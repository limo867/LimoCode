"""In-process application service for every user interface."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from threading import Event, Lock, Thread
import time
from typing import Any, Callable
import uuid

from .agent import Agent, DemoModel, ModelClient, ModelRequestLimiter
from .changes import (
    APPLIED,
    APPROVED,
    CONFLICT,
    REJECTED,
    WAITING_APPROVAL,
    ChangeSet,
    ChangeSetManager,
)
from .config import Config
from .context import CompactionResult, ContextManager
from .events import AgentEvent
from .llm_client import OpenAICompatibleClient
from .memory import MemoryStore
from .skills import SkillManager
from .storage import TaskStore
from .subagents import SubagentManager
from .tools import ToolRegistry


ModelFactory = Callable[[Config, bool], ModelClient]


@dataclass
class ApprovalRequest:
    id: str
    command: str
    family: str | None = None
    family_label: str | None = None
    resolved: Event = field(default_factory=Event, repr=False)
    approved: bool | None = None


@dataclass
class ChangeApprovalRequest:
    id: str
    changeset_id: str
    resolved: Event = field(default_factory=Event, repr=False)
    approved: bool | None = None


@dataclass
class TaskRecord:
    id: str
    task: str
    status: str = "queued"
    result: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # A conversation is the user-visible unit. Each submitted prompt is still
    # represented by a separate task so its events and file changes remain
    # independently inspectable. Keep new fields after the established task
    # fields so existing positional TaskRecord construction stays compatible.
    conversation_id: str | None = None
    parent_task_id: str | None = None
    events: deque[AgentEvent] = field(default_factory=lambda: deque(maxlen=500))
    cancelled: Event = field(default_factory=Event, repr=False)
    thread: Thread | None = field(default=None, repr=False)
    next_sequence: int = field(default=1, repr=False)
    pending_approval: ApprovalRequest | None = field(default=None, repr=False)
    pending_change_approval: ChangeApprovalRequest | None = field(default=None, repr=False)
    agent: Agent | None = field(default=None, repr=False)

    def transition(self, new_status: str) -> None:
        allowed = {
            "queued": {"running", "failed", "cancelled"},
            "running": {"completed", "failed", "cancelled"},
            "completed": set(),
            "failed": set(),
            "cancelled": set(),
        }
        if new_status not in allowed.get(self.status, set()):
            raise ValueError(f"invalid task status transition: {self.status} -> {new_status}")
        self.status = new_status

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "conversation_id": self.conversation_id,
            "parent_task_id": self.parent_task_id,
            "event_count": self.next_sequence - 1,
            "approval": (
                {"id": self.pending_approval.id, "command": self.pending_approval.command}
                if self.pending_approval
                else None
            ),
            "change_approval": (
                {"id": self.pending_change_approval.id, "changeset_id": self.pending_change_approval.changeset_id}
                if self.pending_change_approval
                else None
            ),
        }


class AgentService:
    """Creates, runs, observes, and cancels local Agent tasks."""

    def __init__(self, config: Config, model_factory: ModelFactory | None = None, skill_manager: SkillManager | None = None):
        if config.permission_mode not in {"approval", "auto"}:
            raise ValueError("permission_mode must be 'approval' or 'auto'")
        self.config = config
        self.model_factory = model_factory or self._default_model_factory
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = Lock()
        self.max_tasks = 100
        self._request_limiter = ModelRequestLimiter(config.model_min_request_interval_ms)
        self.skill_manager = skill_manager or SkillManager(SkillManager.default_roots(config.workspace))
        self.selected_skills: tuple[str, ...] = ()
        self.memory_store = MemoryStore(config.memory_db)
        try:
            self.store = TaskStore(config.history_db) if config.history_db else None
        except Exception:
            # Registry callers may retry with user-owned persistence when a
            # selected project folder is read-only. Do not leak the memory
            # SQLite handle from this failed construction attempt on Windows.
            self.memory_store.close()
            raise
        self._command_approval_rules: dict[str, str] = {}
        if self.store:
            saved_mode = self.store.get_setting("permission_mode")
            if saved_mode in {"approval", "auto"}:
                self.config = self.config.with_overrides(permission_mode=saved_mode)
            self._command_approval_rules = self._load_command_approval_rules()
        self.change_manager = ChangeSetManager(self.config)
        self._changesets: dict[str, ChangeSet] = {}
        self._conversation_titles: dict[str, str] = {}
        self._subagent_counts: dict[str, int] = {}
        self._implementer_counts: dict[str, int] = {}
        if self.store:
            for snapshot in self.store.load_tasks():
                record = TaskRecord(**snapshot)
                record.events.extend(self.store.load_recent_events(record.id, record.events.maxlen or 500))
                record.next_sequence = self.store.last_event_sequence(record.id) + 1
                if record.status in {"queued", "running"}:
                    record.transition("failed")
                    record.error = "task interrupted because the service restarted"
                    self.store.save_task(record.snapshot())
                self._tasks[record.id] = record
            self._backfill_conversations()
            if self.store:
                self._conversation_titles = self.store.load_conversation_titles()
            for snapshot in self.store.load_changesets():
                changeset = ChangeSet.from_snapshot(snapshot)
                # A browser refresh or process restart must never turn a
                # pending proposal into a file write.  It remains inspectable
                # and can only be rejected; the original worker is gone.
                self._changesets[changeset.id] = changeset

    @staticmethod
    def _default_model_factory(config: Config, demo: bool) -> ModelClient:
        return DemoModel() if demo else OpenAICompatibleClient(config)

    def create_task(self, task: str, *, demo: bool | None = None, resume_from: str | None = None) -> TaskRecord:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string")
        if demo is None:
            demo = bool(getattr(self, "demo_default", False))
        parent: TaskRecord | None = None
        persisted_session: tuple[str, list[dict[str, Any]]] | None = None
        if resume_from:
            parent = self.get_task(resume_from)
            persisted_session = self._continuation_session(parent) if parent else None
            if not parent or parent.status not in {"completed", "failed", "cancelled"} or not persisted_session:
                raise ValueError("saved task context is unavailable for continuation")
        if parent:
            # All continuation tasks belong to the selected conversation,
            # including a parent restored from a database written by an older
            # release that did not yet store conversation ids.
            conversation_id = parent.conversation_id or uuid.uuid4().hex
            if not parent.conversation_id:
                parent.conversation_id = conversation_id
                if self.store:
                    self.store.set_task_conversation(parent.id, conversation_id)
        else:
            conversation_id = uuid.uuid4().hex
        record = TaskRecord(
            id=uuid.uuid4().hex,
            task=task.strip(),
            conversation_id=conversation_id,
            parent_task_id=parent.id if parent else None,
        )
        with self._lock:
            if self.store:
                self.store.save_task(record.snapshot())
            self._tasks[record.id] = record
            self._trim_tasks_locked(exclude_id=record.id)
        record.thread = Thread(
            target=self._run,
            args=(record, demo, parent, persisted_session),
            name=f"agent-{record.id[:8]}",
            daemon=True,
        )
        record.thread.start()
        return record

    def conversation_title(self, conversation_id: str, fallback: str = "") -> str:
        title = self._conversation_titles.get(str(conversation_id or ""))
        return title or self._fallback_conversation_title(fallback)

    @staticmethod
    def _fallback_conversation_title(task: str) -> str:
        text = " ".join(str(task or "").split())
        return text if len(text) <= 42 else text[:41].rstrip() + "…"

    def _generate_conversation_title(self, record: TaskRecord, model: ModelClient) -> None:
        if record.parent_task_id or not record.conversation_id or record.conversation_id in self._conversation_titles:
            return
        fallback = self._fallback_conversation_title(record.task)
        title = fallback
        # Keep deterministic/test model implementations single-pass. The
        # production OpenAI-compatible client is the only implementation for
        # which an extra title request is both expected and user-visible.
        if not isinstance(model, OpenAICompatibleClient):
            self._conversation_titles[record.conversation_id] = title
            if self.store:
                self.store.set_conversation_title(record.conversation_id, title, record.created_at)
            return
        try:
            response = model.complete(
                [
                    {"role": "system", "content": "请为下面的编程会话生成一个简短中文标题。只输出标题，不超过18个汉字，不要引号。"},
                    {"role": "user", "content": record.task},
                ],
                [],
            )
            candidate = str((response or {}).get("content") or "").strip().replace("\n", " ")
            if candidate and not (response or {}).get("tool_calls"):
                title = candidate.strip(" \"'“”‘’")[:42] or fallback
        except Exception:
            title = fallback
        self._conversation_titles[record.conversation_id] = title
        if self.store:
            self.store.set_conversation_title(record.conversation_id, title, record.created_at)

    def update_config(self, config: Config) -> None:
        with self._lock:
            if any(record.status in {"queued", "running"} for record in self._tasks.values()):
                raise ValueError("cannot change runtime configuration while a task is active")
            if config.workspace != self.config.workspace:
                raise ValueError("workspace changes require a new service")
            self.config = config
            self._request_limiter = ModelRequestLimiter(config.model_min_request_interval_ms)
            self.change_manager = ChangeSetManager(config)

    def permission_status(self) -> dict[str, Any]:
        return {
            "mode": self.config.permission_mode,
            "modes": ["approval", "auto"],
            "command_approval_rules": self.command_approval_rules(),
        }

    def _load_command_approval_rules(self) -> dict[str, str]:
        if not self.store:
            return {}
        raw = self.store.get_setting("command_approval_rules")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(family): str(label)
            for family, label in value.items()
            if isinstance(family, str) and isinstance(label, str) and family and label
        }

    def _save_command_approval_rules_locked(self) -> None:
        if self.store:
            self.store.set_setting(
                "command_approval_rules",
                json.dumps(self._command_approval_rules, ensure_ascii=True, sort_keys=True),
            )

    def command_approval_rules(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                {"family": family, "label": label}
                for family, label in sorted(self._command_approval_rules.items(), key=lambda item: item[1].casefold())
            ]

    def remove_command_approval_rule(self, family: str) -> bool:
        with self._lock:
            if family not in self._command_approval_rules:
                return False
            del self._command_approval_rules[family]
            self._save_command_approval_rules_locked()
            return True

    def is_command_family_approved(self, command: str) -> bool:
        family = ToolRegistry.command_approval_family(command)
        if not family:
            return False
        with self._lock:
            return family[0] in self._command_approval_rules

    def set_permission_mode(self, mode: str) -> dict[str, Any]:
        normalized = str(mode or "").strip().lower()
        if normalized not in {"approval", "auto"}:
            raise ValueError("mode must be 'approval' or 'auto'")
        with self._lock:
            if any(record.status in {"queued", "running"} for record in self._tasks.values()):
                raise ValueError("cannot change permission mode while a task is active")
            self.config = self.config.with_overrides(permission_mode=normalized)
            self.change_manager = ChangeSetManager(self.config)
            if self.store:
                self.store.set_setting("permission_mode", normalized)
        return self.permission_status()

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            records = sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)
            return [record.snapshot() for record in records[offset : offset + limit if limit is not None else None]]

    def list_conversations(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        resumable_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return user-visible conversations, each grouping its task turns.

        ``tasks`` retains individual task history for transcript and activity
        views. The summary ids let history/resume pickers display one row per
        conversation and continue from its newest resumable task.
        """
        with self._lock:
            records = list(self._tasks.values())
        grouped: dict[str, list[TaskRecord]] = {}
        for record in records:
            # Runtime-created records always have a conversation id. The
            # fallback keeps manually-created records usable until a service
            # restart persists and backfills them.
            conversation_id = record.conversation_id or record.id
            grouped.setdefault(conversation_id, []).append(record)

        conversations: list[dict[str, Any]] = []
        for conversation_id, members in grouped.items():
            ordered = self._ordered_conversation_tasks(members)
            members_by_id = {item.id: item for item in ordered}
            root = self._conversation_root(ordered, members_by_id)
            latest = ordered[-1]
            continuation = next(
                (item for item in reversed(ordered) if self.get_continuable_task(item.id)),
                None,
            )
            if resumable_only and continuation is None:
                continue
            root_snapshot = root.snapshot()
            latest_snapshot = latest.snapshot()
            continuation_snapshot = continuation.snapshot() if continuation else None
            conversations.append(
                {
                    "id": conversation_id,
                    "conversation_id": conversation_id,
                    "title": self.conversation_title(conversation_id, root.task),
                    "root_task_id": root.id,
                    "latest_task_id": latest.id,
                    "continuation_task_id": continuation.id if continuation else None,
                    "latest_resumable_task_id": continuation.id if continuation else None,
                    "task_count": len(ordered),
                    "created_at": root.created_at,
                    "updated_at": latest.created_at,
                    "root_task": root_snapshot,
                    "latest_task": latest_snapshot,
                    "continuation_task": continuation_snapshot,
                    "latest_resumable_task": continuation_snapshot,
                    "tasks": [item.snapshot() for item in ordered],
                }
            )
        conversations.sort(key=lambda item: (str(item["updated_at"]), str(item["id"])), reverse=True)
        return conversations[offset : offset + limit if limit is not None else None]

    def conversation_tasks(self, conversation_id: str) -> list[TaskRecord]:
        """Return every task turn in one conversation, oldest first."""
        with self._lock:
            records = [
                record
                for record in self._tasks.values()
                if (record.conversation_id or record.id) == conversation_id
            ]
        return self._ordered_conversation_tasks(records)

    def delete_conversation(self, conversation_id: str) -> bool:
        """Permanently remove one idle user-visible conversation and its state."""
        normalized_id = str(conversation_id or "").strip()
        if not normalized_id:
            return False
        with self._lock:
            records = [
                record
                for record in self._tasks.values()
                if (record.conversation_id or record.id) == normalized_id
            ]
            if not records:
                return False
            if any(record.status in {"queued", "running"} for record in records):
                raise ValueError("cannot delete a conversation while a task is active")
            for record in records:
                self._tasks.pop(record.id, None)
                if self.store:
                    self.store.delete_task(record.id)
            self._conversation_titles.pop(normalized_id, None)
            if self.store:
                self.store.delete_conversation_title(normalized_id)
        return True

    @staticmethod
    def _ordered_conversation_tasks(records: list[TaskRecord]) -> list[TaskRecord]:
        """Order conversation tasks so a parent always precedes its children.

        Normal task creation produces distinct timestamps, but imported and
        legacy SQLite records can share one. Sorting only by timestamp can
        then choose a parent as the latest resume/compaction anchor. A stable
        parent-first traversal keeps the user-visible conversation order and
        recovery behavior correct in both cases.
        """
        members_by_id = {record.id: record for record in records}
        children: dict[str, list[TaskRecord]] = {}
        roots: list[TaskRecord] = []
        indegree: dict[str, int] = {record.id: 0 for record in records}
        for record in records:
            if record.parent_task_id and record.parent_task_id in members_by_id:
                children.setdefault(record.parent_task_id, []).append(record)
                indegree[record.id] = 1
            else:
                roots.append(record)

        key = lambda item: (item.created_at, item.id)
        for child_records in children.values():
            child_records.sort(key=key)
        ready = sorted(roots, key=key)
        ordered: list[TaskRecord] = []
        seen: set[str] = set()

        # Kahn's traversal keeps normal timestamps chronological while still
        # guaranteeing that a same-timestamp continuation comes after its
        # parent. This matters for selecting the newest resume anchor.
        while ready:
            record = ready.pop(0)
            if record.id in seen:
                continue
            seen.add(record.id)
            ordered.append(record)
            for child in children.get(record.id, []):
                indegree[child.id] -= 1
                if indegree[child.id] == 0:
                    ready.append(child)
            ready.sort(key=key)
        # Preserve malformed/cyclic historical records without looping.
        ordered.extend(record for record in sorted(records, key=key) if record.id not in seen)
        return ordered

    @staticmethod
    def _conversation_root(
        members: list[TaskRecord],
        members_by_id: dict[str, TaskRecord],
    ) -> TaskRecord:
        """Choose the root task for a grouped conversation defensively."""
        roots = [
            item
            for item in members
            if not item.parent_task_id or item.parent_task_id not in members_by_id
        ]
        candidates = roots or members
        return min(candidates, key=lambda item: (item.created_at, item.id))

    def _backfill_conversations(self) -> None:
        """Assign durable ids to old task history without splitting continuations.

        Versions predating ``conversation_id`` may have direct parent ids, or
        no parent data at all. For the latter, the old persisted task context
        has a deterministic ``parent context + Follow-up`` form; infer that
        edge first, then let every descendant inherit its root's id.
        """
        with self._lock:
            records_by_id = dict(self._tasks)
        if not records_by_id:
            return

        contexts: dict[str, str | None] = {}
        for record in sorted(records_by_id.values(), key=lambda item: (item.created_at, item.id)):
            if record.parent_task_id:
                continue
            parent = self._infer_legacy_parent(record, records_by_id, contexts)
            if not parent:
                continue
            record.parent_task_id = parent.id
            if self.store:
                self.store.set_task_parent(record.id, parent.id)

        resolved: dict[str, str] = {}

        def resolve(record: TaskRecord, ancestors: set[str]) -> str:
            cached = resolved.get(record.id)
            if cached:
                return cached
            parent = records_by_id.get(record.parent_task_id) if record.parent_task_id else None
            if parent and parent.id not in ancestors:
                conversation_id = resolve(parent, ancestors | {record.id})
            else:
                # Broken parent links and cycles were never a valid hierarchy.
                # Keep them readable by assigning one stable local group rather
                # than following an unbounded reference.
                conversation_id = record.conversation_id or uuid.uuid4().hex
            resolved[record.id] = conversation_id
            return conversation_id

        for record in records_by_id.values():
            conversation_id = resolve(record, set())
            if record.conversation_id == conversation_id:
                continue
            record.conversation_id = conversation_id
            if self.store:
                self.store.set_task_conversation(record.id, conversation_id)

    def get_continuable_task(self, task_id: str) -> TaskRecord | None:
        """Return a finished task when its conversation can be resumed."""
        record = self.get_task(task_id)
        if not record or record.status not in {"completed", "failed", "cancelled"}:
            return None
        return record if self._continuation_session(record) else None

    def latest_continuable_task(self) -> TaskRecord | None:
        """Find the newest finished task with live or persisted session state."""
        with self._lock:
            finished = sorted(
                (record for record in self._tasks.values() if record.status in {"completed", "failed", "cancelled"}),
                key=lambda item: item.created_at,
                reverse=True,
            )
        for record in finished:
            if self._continuation_session(record):
                return record
        return None

    def conversation_lineage(self, task_id: str) -> list[TaskRecord]:
        """Return one parent path from a conversation root through ``task_id``.

        New callers that need the complete user-visible conversation should
        use :meth:`conversation_tasks`; this method remains for callers that
        explicitly need a single task's ancestry.
        """
        with self._lock:
            records_by_id = dict(self._tasks)
        records: list[TaskRecord] = []
        seen: set[str] = set()
        contexts: dict[str, str | None] = {}
        current = records_by_id.get(task_id)
        while current and current.id not in seen:
            records.append(current)
            seen.add(current.id)
            parent = records_by_id.get(current.parent_task_id) if current.parent_task_id else None
            if parent is None and not current.parent_task_id:
                parent = self._infer_legacy_parent(current, records_by_id, contexts)
                if parent:
                    current.parent_task_id = parent.id
                    if self.store:
                        self.store.set_task_parent(current.id, parent.id)
                    conversation_id = parent.conversation_id or current.conversation_id or uuid.uuid4().hex
                    if current.conversation_id != conversation_id:
                        current.conversation_id = conversation_id
                        if self.store:
                            self.store.set_task_conversation(current.id, conversation_id)
            current = parent
        return list(reversed(records))

    def _infer_legacy_parent(
        self,
        record: TaskRecord,
        records_by_id: dict[str, TaskRecord],
        contexts: dict[str, str | None],
    ) -> TaskRecord | None:
        """Find one unambiguous old-format parent using saved task contexts."""
        task_context = self._task_context_for(record, contexts)
        if not task_context:
            return None
        matches: list[TaskRecord] = []
        for candidate in records_by_id.values():
            # Older versions can write consecutive snapshots with identical
            # timestamps. Their exact context relationship remains sufficient
            # evidence, so only reject candidates that are definitely newer.
            if candidate.id == record.id or candidate.created_at > record.created_at:
                continue
            parent_context = self._task_context_for(candidate, contexts)
            if parent_context and task_context == f"{parent_context}\n\nFollow-up: {record.task}":
                matches.append(candidate)
        # A copied/retried task can leave more than one matching context. In
        # that case there is no safe way to tell which branch the old task
        # belonged to, so avoid merging unrelated user-visible conversations.
        if len(matches) != 1:
            return None
        return matches[0]

    def _task_context_for(self, record: TaskRecord, contexts: dict[str, str | None]) -> str | None:
        if record.id in contexts:
            return contexts[record.id]
        if record.agent is not None:
            context, _messages = record.agent.session_state()
        elif self.store:
            session = self.store.load_session(record.id)
            context = session[0] if session else None
        else:
            context = None
        contexts[record.id] = str(context) if context else None
        return contexts[record.id]

    def events(self, task_id: str, after: int = 0, *, limit: int | None = None) -> list[AgentEvent]:
        record = self.get_task(task_id)
        if not record:
            return []
        if self.store:
            return self.store.load_events(task_id, after, limit)
        events = [event for event in record.events if event.sequence > after]
        return events[:limit] if limit is not None else events

    def cancel_task(self, task_id: str) -> bool:
        record = self.get_task(task_id)
        if not record or record.status in {"completed", "failed", "cancelled"}:
            return False
        record.cancelled.set()
        self._emit(record, "task_cancelling", {})
        return True

    def approve_command(
        self,
        task_id: str,
        approval_id: str,
        approved: bool,
        *,
        scope: str = "once",
    ) -> bool:
        record = self.get_task(task_id)
        if not record or not isinstance(approved, bool):
            return False
        if scope not in {"once", "always"}:
            return False
        with self._lock:
            request = record.pending_approval
            if not request or request.id != approval_id or request.resolved.is_set():
                return False
            if scope == "always":
                if not approved or not request.family or not request.family_label:
                    return False
                self._command_approval_rules[request.family] = request.family_label
                self._save_command_approval_rules_locked()
            request.approved = approved
            request.resolved.set()
            return True

    def approve_changeset(self, task_id: str, approval_id: str, approved: bool) -> bool:
        record = self.get_task(task_id)
        if not record or not isinstance(approved, bool):
            return False
        with self._lock:
            request = record.pending_change_approval
            if not request or request.id != approval_id or request.resolved.is_set():
                return False
            request.approved = approved
            request.resolved.set()
            return True

    def list_changesets(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._changesets.values())
        if task_id:
            values = [item for item in values if item.task_id == task_id]
        return [item.snapshot(preview_limit=min(self.config.max_history_chars, 4000)) for item in sorted(values, key=lambda item: item.updated_at, reverse=True)]

    def get_changeset(self, changeset_id: str) -> dict[str, Any] | None:
        with self._lock:
            changeset = self._changesets.get(changeset_id)
        return changeset.snapshot(preview_limit=min(self.config.max_history_chars, 4000)) if changeset else None

    def undo_changeset(self, changeset_id: str) -> dict[str, Any] | None:
        with self._lock:
            changeset = self._changesets.get(changeset_id)
            if not changeset:
                return None
            result = self.change_manager.undo(changeset)
            self._save_changeset_locked(result)
            record = self._tasks.get(result.task_id)
        if record:
            self._emit(record, "changeset_undone" if result.status == "undone" else "changeset_conflict", result.snapshot())
        return result.snapshot(preview_limit=min(self.config.max_history_chars, 4000))

    def _save_changeset_locked(self, changeset: ChangeSet) -> None:
        self._changesets[changeset.id] = changeset
        if self.store:
            self.store.save_changeset(changeset.snapshot(include_content=True))

    def _propose_file_change(self, record: TaskRecord, path: str, content: str) -> dict[str, Any]:
        changeset = self.change_manager.prepare_write(record.id, path, content)
        with self._lock:
            self._save_changeset_locked(changeset)
        self._emit(record, "changeset_proposed", changeset.snapshot())
        if self.config.permission_mode == "approval":
            request = ChangeApprovalRequest(id=uuid.uuid4().hex, changeset_id=changeset.id)
            with self._lock:
                record.pending_change_approval = request
                changeset.status = WAITING_APPROVAL
                changeset.updated_at = datetime.now(timezone.utc).isoformat()
                self._save_changeset_locked(changeset)
            self._emit(record, "changeset_approval_requested", {"approval_id": request.id, **changeset.snapshot()})
            deadline = time.monotonic() + max(1, self.config.command_approval_timeout)
            while time.monotonic() < deadline:
                if record.cancelled.is_set():
                    break
                if request.resolved.wait(0.1):
                    break
            rejected_result = None
            with self._lock:
                if record.pending_change_approval is request:
                    record.pending_change_approval = None
                if request.approved is not True:
                    changeset.status = REJECTED
                    changeset.error = "change approval rejected or timed out"
                    changeset.updated_at = datetime.now(timezone.utc).isoformat()
                    self._save_changeset_locked(changeset)
                    decision = "cancelled" if record.cancelled.is_set() else "rejected"
                    rejected_result = changeset.snapshot()
                    rejected_result.update({"ok": False, "error": f"change approval {decision}", "requires_approval": True, "changeset_id": changeset.id})
            if rejected_result is not None:
                self._emit(record, "changeset_rejected", rejected_result)
                return rejected_result
            changeset.status = APPROVED
        result = self.change_manager.apply(changeset)
        with self._lock:
            self._save_changeset_locked(result)
        payload = result.snapshot()
        payload.update({"ok": result.status == APPLIED, "path": path, "changed": result.status == APPLIED, "changeset_id": result.id})
        if result.status != APPLIED:
            payload["error"] = result.error or "change could not be applied"
        self._emit(record, "changeset_applied" if result.status == APPLIED else "changeset_failed", payload)
        return payload

    def set_selected_skills(self, names: tuple[str, ...]) -> None:
        for name in names:
            self.skill_manager.load(name)
        self.selected_skills = names

    def reload_skills(self) -> None:
        self.skill_manager.reload()
        available = {skill.name for skill in self.skill_manager.metadata()}
        self.selected_skills = tuple(name for name in self.selected_skills if name in available)

    def list_memories(self, limit: int = 50):
        return self.memory_store.list(limit)

    def add_memory(self, content: str):
        return self.memory_store.add(content)

    def search_memories(self, query: str, limit: int = 8):
        return self.memory_store.search(query, limit)

    def delete_memory(self, item_id: int) -> bool:
        return self.memory_store.delete(item_id)

    def memory_status(self, task_id: str | None = None) -> dict[str, Any]:
        """Report durable-memory health and the selected task's short-term context."""
        long_term = dict(self.memory_store.status())
        long_term["context_char_budget"] = max(0, self.config.memory_context_chars)
        long_term["max_retrieved_items"] = 6
        status: dict[str, Any] = {
            "long_term": long_term,
            "short_term": None,
            "task_found": task_id is None,
        }
        if not task_id:
            return status
        record = self.get_task(task_id)
        if not record:
            return status
        status["task_found"] = True
        persisted = False
        if record.agent:
            context = record.agent.context_status()
            source = "live"
            persisted = self.store is not None
        else:
            session = self.store.load_session(record.id) if self.store else None
            if not session:
                status["short_term"] = {
                    "task_id": record.id,
                    "task_status": record.status,
                    "available": False,
                    "resumable": False,
                }
                return status
            _task_context, messages = session
            context = ContextManager(self.config).status(messages)
            source = "persisted"
            persisted = True
        status["short_term"] = {
            "task_id": record.id,
            "task_status": record.status,
            "available": True,
            "persisted": persisted,
            "resumable": record.status in {"completed", "failed", "cancelled"} and (record.agent is not None or persisted),
            "source": source,
            **context,
        }
        return status

    def compact_conversation(self, conversation_id: str) -> CompactionResult | None:
        """Compact the latest resumable context snapshot of one conversation.

        A conversation is made of several task records, but its newest
        resumable task owns the accumulated model transcript used for the next
        turn.  That task is only a persistence anchor: this method never
        removes or rewrites the individual task history shown to the user.
        It also works after a restart by restoring the saved session snapshot
        before compacting and writing the result back to SQLite.
        """
        normalized_id = str(conversation_id or "").strip()
        if not normalized_id:
            return None
        anchor = self._conversation_compaction_anchor(normalized_id)
        if not anchor:
            return None

        if anchor.agent is not None:
            agent = anchor.agent
        else:
            session = self._continuation_session(anchor)
            if not session:
                return None

            def emit(event_type: str, data: dict[str, Any]) -> None:
                payload = dict(data)
                payload["conversation_id"] = normalized_id
                self._emit(anchor, event_type, payload)

            agent = Agent(
                self.config,
                event_callback=emit,
                is_cancelled=lambda: False,
                request_limiter=self._request_limiter,
                memory_store=self.memory_store,
            )
            agent.restore_session(*session)
            # Preserve the restored context in memory as well. A later
            # continuation still builds its own isolated Agent from this
            # snapshot, so branches cannot mutate one another.
            with self._lock:
                anchor.agent = agent

        result = agent.compact_context(force=True)
        if self.store:
            task_context, messages = agent.session_state()
            self.store.save_session(
                anchor.id,
                task_context,
                messages,
                datetime.now(timezone.utc).isoformat(),
            )
        return result

    def compact_task(self, task_id: str) -> CompactionResult | None:
        """Backward-compatible task entry point for older callers.

        Manual compaction is conversation-scoped. A task id merely identifies
        which conversation should be compacted.
        """
        record = self.get_task(task_id)
        if not record:
            return None
        return self.compact_conversation(record.conversation_id or record.id)

    def _conversation_compaction_anchor(self, conversation_id: str) -> TaskRecord | None:
        """Return the newest saved context that can continue this conversation."""
        records = self.conversation_tasks(conversation_id)
        for record in reversed(records):
            if record.status not in {"completed", "failed", "cancelled"}:
                continue
            if self._continuation_session(record):
                return record
        return None

    def _run(
        self,
        record: TaskRecord,
        demo: bool,
        parent: TaskRecord | None = None,
        persisted_session: tuple[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        pending_assistant_content = ""

        def emit(event_type: str, data: dict[str, Any]) -> None:
            nonlocal pending_assistant_content
            # The final reply is an artifact of a successful verification and
            # review, not merely the first model completion. Stream deltas for
            # responsiveness; the terminal assistant_message is emitted only
            # after the safety gates and represents the accepted final text.
            if event_type == "assistant_delta":
                pending_assistant_content += str(data.get("delta") or "")
                self._emit(record, event_type, data)
                return
            if event_type == "assistant_message":
                pending_assistant_content = str(data.get("content") or pending_assistant_content)
                return
            self._emit(record, event_type, data)

        try:
            with self._lock:
                record.transition("running")
            with self._lock:
                if self.store:
                    self.store.save_task(record.snapshot())
                self._trim_tasks_locked()
            model = self.model_factory(self.config, demo)
            approval = lambda command, cancelled: self._request_command_approval(record, command, cancelled)

            def new_agent() -> Agent:
                return Agent(
                    self.config,
                    model=model,
                    event_callback=emit,
                    is_cancelled=record.cancelled.is_set,
                    request_limiter=self._request_limiter,
                    command_approval=approval,
                    skill_manager=self.skill_manager,
                    selected_skills=self.selected_skills,
                    memory_store=self.memory_store,
                    change_manager=self.change_manager,
                    task_id=record.id,
                    change_callback=lambda path, content: self._propose_file_change(record, path, content),
                    command_policy=self.is_command_family_approved,
                    subagent_runner=lambda role, task: self._run_subagent(record, model, role, task),
                )

            # A continuation must start from the selected task's snapshot.
            # Reusing parent.agent lets separate /resume branches mutate each
            # other's messages, so every child receives an isolated Agent.
            agent = new_agent()
            if parent:
                session = persisted_session or self._continuation_session(parent)
                if not session:
                    raise ValueError("saved task context is unavailable")
                agent.restore_session(*session)
            record.agent = agent
            if parent:
                record.result = agent.continue_task(
                    record.task,
                    config=self.config,
                    model=model,
                    event_callback=emit,
                    is_cancelled=record.cancelled.is_set,
                    request_limiter=self._request_limiter,
                    command_approval=approval,
                    parent_task_id=parent.id,
                )
                if self._has_code_changes(agent) and not record.cancelled.is_set():
                    verification = self._run_verifier(record, model, agent)
                    self._emit(record, "verification_completed", verification)
            else:
                explorer_context = None
                if self._should_auto_explore(record.task) and not record.cancelled.is_set():
                    explorer_context = self._run_parallel_explorers(record, model)
                record.result = agent.run(record.task, initial_context=explorer_context)
                verification = None
                if self._has_code_changes(agent) and not record.cancelled.is_set():
                    verification = self._run_verifier(record, model, agent)
                    self._emit(record, "verification_completed", verification)
                # Production runs receive one read-only final review. This is
                # a bounded safety gate: a rejected review gets one correction
                # pass with concrete findings, rather than silently accepting
                # a plausible but incorrect answer.
                if (
                    isinstance(model, OpenAICompatibleClient)
                    and not record.cancelled.is_set()
                    and agent.last_status == "completed"
                ):
                    review_prompt = (
                        "审查这次编码任务是否真正符合用户意图。只读检查工作区当前状态、相关文件和测试结果，"
                        "不要修改文件。必须严格按以下格式返回：第一行只能是 VERDICT: PASS 或 VERDICT: REJECT；"
                        "随后用中文列出依据。若有任何需求遗漏、错误实现、未验证或答非所问，必须 REJECT，"
                        "并给出可执行的修正建议。\n\n"
                        f"用户原始请求：{record.task}\n\n"
                        f"主 Agent 最终回复：{record.result}\n\n"
                        f"主 Agent 已执行操作摘要：{json.dumps(agent.execution_log[-20:], ensure_ascii=False)}\n\n"
                        f"Verifier 验证报告：{json.dumps(verification or {}, ensure_ascii=False)}"
                    )
                    review = self._run_subagent(record, model, "reviewer", review_prompt, reserved=True)
                    verdict = self._review_verdict(review)
                    self._emit(record, "review_completed", {
                        "verdict": verdict,
                        "summary": review.get("summary", ""),
                        "status": review.get("status"),
                    })
                    if verdict == "reject" and not record.cancelled.is_set():
                        feedback = str(review.get("summary") or review.get("error") or "审查未通过，请重新检查并修正实现。")[:6000]
                        self._emit(record, "review_rejected", {"feedback": feedback})
                        record.result = agent.continue_task(
                            "Reviewer 审查未通过，请根据以下反馈修正实现，并重新运行必要的验证。\n\n" + feedback,
                            config=self.config,
                            model=model,
                            event_callback=emit,
                            is_cancelled=record.cancelled.is_set,
                            request_limiter=self._request_limiter,
                            command_approval=approval,
                            parent_task_id=record.id,
                        )
                        if agent.last_status == "completed" and not record.cancelled.is_set():
                            final_review = self._run_subagent(
                                record,
                                model,
                                "reviewer",
                                review_prompt + f"\n\n主 Agent 根据首次审查修正后的回复：{record.result}",
                                reserved=True,
                            )
                            final_verdict = self._review_verdict(final_review)
                            self._emit(record, "review_completed", {
                                "verdict": final_verdict,
                                "summary": final_review.get("summary", ""),
                                "status": final_review.get("status"),
                                "final": True,
                            })
                            if final_verdict == "reject":
                                record.result = "Reviewer 在修正后仍未通过本次任务，未将结果作为成功交付。\n\n" + str(
                                    final_review.get("summary") or final_review.get("error") or "请检查审查结论。"
                                )
                                agent.last_status = "failed"
            self._generate_conversation_title(record, model)
            if record.cancelled.is_set() or agent.last_status == "cancelled":
                with self._lock:
                    record.transition("cancelled")
                self._emit(record, "task_cancelled", {"result": record.result})
            elif agent.last_status == "failed":
                with self._lock:
                    record.transition("failed")
                record.error = record.result
                self._emit(record, "task_error", {"error": record.error})
            else:
                with self._lock:
                    record.transition("completed")
                self._emit(record, "assistant_message", {"content": record.result or pending_assistant_content})
                self._emit(record, "task_finished", {"result": record.result, "status": agent.last_status})
            with self._lock:
                if self.store:
                    self.store.save_task(record.snapshot())
                self._trim_tasks_locked()
        except Exception as exc:
            with self._lock:
                if record.status in {"queued", "running"}:
                    record.transition("failed")
            record.error = str(exc)
            self._emit(record, "task_error", {"error": record.error})
            if self.store:
                self.store.save_task(record.snapshot())

    @staticmethod
    def _has_code_changes(agent: Agent) -> bool:
        """Return true only when the agent actually changed a source file."""
        code_extensions = {
            ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".java", ".py", ".js", ".jsx",
            ".ts", ".tsx", ".go", ".rs", ".rb", ".php", ".cs", ".swift", ".kt", ".kts",
            ".css", ".html", ".vue", ".sql", ".sh", ".ps1",
        }
        for entry in agent.execution_log:
            result = entry.get("result") or {}
            if entry.get("tool") == "write_file":
                path = str(result.get("path") or entry.get("arguments", {}).get("path") or "")
                changed = bool(result.get("ok") and result.get("changed"))
            elif entry.get("tool") == "spawn_subagent":
                paths = result.get("files") or []
                path = next((str(item) for item in paths if str(item).lower().endswith(tuple(code_extensions))), "")
                changed = bool(result.get("ok") and result.get("changeset_ids"))
            else:
                continue
            if changed and any(path.lower().endswith(ext) for ext in code_extensions):
                return True
        return False

    def _run_verifier(self, parent: TaskRecord, model: ModelClient, agent: Agent) -> dict[str, Any]:
        paths: set[str] = set()
        for entry in agent.execution_log:
            result = entry.get("result") or {}
            if entry.get("tool") == "write_file" and result.get("changed"):
                path = result.get("path") or entry.get("arguments", {}).get("path")
                if path:
                    paths.add(str(path))
            elif entry.get("tool") == "spawn_subagent" and result.get("changeset_ids"):
                paths.update(str(path) for path in (result.get("files") or []) if path)
        paths = sorted(paths)
        prompt = (
            "请验证主 Agent 刚刚修改的代码。只运行安全的本地检查、编译、测试或可执行程序命令；"
            "可使用 dir/type/where 或只读 git status、git diff、git log、git show 检查环境。"
            "不要修改、删除、下载或安装文件，不要使用 &, |, >, <, ; 等 shell 链接或重定向。"
            "优先读取变更文件和项目说明，选择与语言匹配的验证命令。"
            "请用中文报告实际命令、退出码、关键输出和是否通过。\n\n"
            f"变更文件：{', '.join(paths)}"
        )
        result = self._run_subagent(parent, model, "verifier", prompt, reserved=True, max_turns=8)
        return {"id": result.get("id"), "role": "verifier", "status": result.get("status"), "summary": result.get("summary", ""), "error": result.get("error"), "files": paths}

    def _run_parallel_explorers(self, parent: TaskRecord, model: ModelClient) -> str | None:
        """Run independent read-only discovery passes concurrently.

        The reports investigate different questions and are merged only after
        both complete. They never write or run commands, so they are safe to
        execute against the same workspace in parallel.
        """
        assignments = (
            (
                "实现分析",
                "请分析此用户请求涉及的实现。定位相关源码、入口、依赖、当前行为与约束。"
                "不要修改文件，使用中文报告文件路径和建议。\n\n"
                f"用户请求：{parent.task}",
            ),
            (
                "验证分析",
                "请分析此用户请求的验证路径。定位现有测试、构建配置、可安全执行的编译或测试命令，"
                "并指出可能的边界风险。不要修改文件，使用中文报告。\n\n"
                f"用户请求：{parent.task}",
            ),
        )
        # Keep deterministic/offline model runs lightweight; production API
        # runs use both independent read-only passes concurrently.
        if not isinstance(model, OpenAICompatibleClient):
            assignments = assignments[:1]
        with ThreadPoolExecutor(max_workers=len(assignments), thread_name_prefix="explorer") as pool:
            futures = [
                pool.submit(self._run_subagent, parent, model, "explorer", assignment, reserved=True, max_turns=3)
                for _label, assignment in assignments
            ]
            reports = [future.result() for future in futures]
        completed = [
            f"## {label}\n{str(report.get('summary') or '').strip()}"
            for (label, _assignment), report in zip(assignments, reports)
            if report.get("status") == "completed" and report.get("summary")
        ]
        return "\n\n".join(completed) or None

    @staticmethod
    def _review_verdict(review: dict[str, Any]) -> str:
        """Parse the reviewer's constrained first-line verdict defensively."""
        if review.get("status") != "completed":
            # A missing review is not evidence of correctness. Fail closed so
            # a transient specialist error cannot silently approve a result.
            return "reject"
        text = str(review.get("summary") or "").strip().upper()
        first_line = text.splitlines()[0] if text else ""
        return "reject" if "VERDICT: REJECT" in first_line or first_line.startswith("REJECT") else "pass"

    @staticmethod
    def _should_auto_explore(task: str) -> bool:
        """Use one read-only specialist for requests that likely need workspace context.

        This is deliberately conservative: it provides visible delegation for
        programming work without spending an additional model request on
        greetings, explanations, or ordinary conversation.
        """
        normalized = " ".join(str(task or "").lower().split())
        if len(normalized) < 4:
            return False
        chinese_signals = (
            "修改", "实现", "修复", "排查", "调试", "重构", "编译", "运行",
            "新建", "创建", "编写", "写一个", "测试", "代码", "文件", "项目",
        )
        if any(signal in normalized for signal in chinese_signals):
            return True
        english_signals = (
            "implement", "modify", "edit ", "fix ", "debug", "refactor",
            "build ", "compile", "create ", "write ", "add ", "test ",
        )
        return any(signal in normalized for signal in english_signals)

    def _run_subagent(
        self,
        parent: TaskRecord,
        model: ModelClient,
        role: str,
        task: str,
        *,
        reserved: bool = False,
        max_turns: int = 16,
    ) -> dict[str, Any]:
        """Execute a bounded specialist under the parent task.

        One Implementer is permitted per parent task. Its write callback is
        intentionally the parent's change proposal function, so all diffs,
        approvals, persistence, and undo semantics stay unchanged.
        """
        normalized_role = str(role or "").strip().lower()
        with self._lock:
            count = self._subagent_counts.get(parent.id, 0)
            if count >= 2 and not reserved:
                return {
                    "role": role,
                    "task": task,
                    "status": "failed",
                    "summary": "Subagent limit reached for this task.",
                    "error": "maximum of 2 subagents per task",
                }
            implementer_count = self._implementer_counts.get(parent.id, 0)
            if normalized_role == "implementer" and implementer_count >= 1:
                return {
                    "role": normalized_role,
                    "task": task,
                    "status": "failed",
                    "summary": "An Implementer has already been used for this task.",
                    "error": "maximum of 1 implementer per task (maximum of 2 subagents for normal delegation)",
                }
            # System-owned discovery, verification, and review stages have
            # dedicated budgets. They must not prevent the parent model from
            # using its two normal specialist delegations.
            if not reserved:
                self._subagent_counts[parent.id] = count + 1
            if normalized_role == "implementer":
                self._implementer_counts[parent.id] = implementer_count + 1
        subagent_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        snapshot = {
            "id": subagent_id,
            "parent_task_id": parent.id,
            "role": normalized_role,
            "task": task,
            "status": "running",
            "summary": "",
            "files": [],
            "changeset_ids": [],
            "operations": 0,
            "error": None,
            "duration_ms": 0,
            "created_at": created_at,
            "updated_at": created_at,
        }
        if self.store:
            self.store.save_subagent(snapshot)
        self._emit(parent, "subagent_started", {"id": subagent_id, "role": normalized_role, "task": task, "index": count + 1})

        def forward_child_event(event_type: str, data: dict[str, Any]) -> None:
            # Child events have their own private Agent instance. Re-publish
            # a bounded progress stream on the parent task so UI cards can
            # show the work in real time without exposing child transcripts.
            if event_type not in {"model_thinking", "tool_started", "tool_finished", "task_error"}:
                return
            self._emit(parent, "subagent_progress", {
                "id": subagent_id,
                "role": normalized_role,
                "event": event_type,
                "tool": data.get("tool"),
                "arguments": data.get("arguments") or {},
                "result": data.get("result") or {},
                "error": data.get("error"),
            })

        result = SubagentManager(self.config, model, self._request_limiter, max_turns=max_turns).run(
            normalized_role,
            task,
            is_cancelled=parent.cancelled.is_set,
            change_manager=self.change_manager,
            task_id=parent.id,
            change_callback=lambda path, content: self._propose_file_change(parent, path, content),
            command_approval=lambda command, cancelled: self._request_command_approval(parent, command, cancelled),
            command_policy=self.is_command_family_approved,
            event_callback=forward_child_event,
        )
        payload = result.as_dict()
        payload["id"] = subagent_id
        payload["duration_ms"] = round((time.monotonic() - started) * 1000)
        snapshot.update(payload)
        snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
        if self.store:
            self.store.save_subagent(snapshot)
        self._emit(parent, "subagent_finished", payload)
        return payload

    def subagents(self, task_id: str) -> list[dict[str, Any]]:
        """Return persisted first-phase specialists for one parent task."""
        if not self.get_task(task_id):
            return []
        return self.store.load_subagents(task_id) if self.store else []

    def _emit(self, record: TaskRecord, event_type: str, data: dict[str, Any]) -> None:
        with self._lock:
            sequence = record.next_sequence
            record.next_sequence += 1
            event = AgentEvent(type=event_type, task_id=record.id, data=data, sequence=sequence)
            record.events.append(event)
            if self.store:
                self.store.save_event(event)
                if record.agent:
                    task_context, messages = record.agent.session_state()
                    self.store.save_session(record.id, task_context, messages, event.timestamp)

    def _request_command_approval(
        self,
        record: TaskRecord,
        command: str,
        is_cancelled: Callable[[], bool],
    ) -> str:
        family = ToolRegistry.command_approval_family(command)
        request = ApprovalRequest(
            id=uuid.uuid4().hex,
            command=command,
            family=family[0] if family else None,
            family_label=family[1] if family else None,
        )
        with self._lock:
            record.pending_approval = request
        self._emit(record, "command_approval_requested", {
            "approval_id": request.id,
            "command": command,
            "family": request.family,
            "family_label": request.family_label,
            "allow_always": request.family is not None,
        })
        deadline = time.monotonic() + max(1, self.config.command_approval_timeout)
        decision = "timed_out"
        while time.monotonic() < deadline:
            if is_cancelled():
                decision = "cancelled"
                break
            if request.resolved.wait(0.1):
                decision = "approved" if request.approved else "rejected"
                break
        with self._lock:
            if record.pending_approval is request:
                record.pending_approval = None
        self._emit(record, "command_approval_resolved", {
            "approval_id": request.id,
            "command": command,
            "decision": decision,
            "family": request.family,
        })
        return decision

    def _trim_tasks_locked(self, *, exclude_id: str | None = None) -> None:
        """Keep bounded completed history without discarding active work."""
        excess = len(self._tasks) - self.max_tasks
        if excess <= 0:
            return
        finished = [
            record
            for record in self._tasks.values()
            if record.status in {"completed", "failed", "cancelled"} and record.id != exclude_id
        ]
        for record in sorted(finished, key=lambda item: item.created_at)[:excess]:
            self._tasks.pop(record.id, None)
            if self.store:
                self.store.delete_task(record.id)

    def _continuation_session(self, record: TaskRecord | None) -> tuple[str, list[dict[str, Any]]] | None:
        """Return an isolated snapshot that Agent.restore_session can accept."""
        if not record:
            return None
        if record.agent is not None:
            task_context, messages = record.agent.session_state()
        elif self.store:
            session = self.store.load_session(record.id)
            if not session:
                return None
            task_context, messages = session
        else:
            return None
        if not task_context or len(messages) < 2:
            return None
        return str(task_context), deepcopy(messages)
