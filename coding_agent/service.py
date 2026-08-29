"""In-process application service for every user interface."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, Lock, Thread
import time
from typing import Any, Callable
import uuid

from .agent import Agent, DemoModel, ModelClient, ModelRequestLimiter
from .config import Config
from .context import CompactionResult
from .events import AgentEvent
from .llm_client import OpenAICompatibleClient
from .memory import MemoryStore
from .skills import SkillManager
from .storage import TaskStore


ModelFactory = Callable[[Config, bool], ModelClient]


@dataclass
class ApprovalRequest:
    id: str
    command: str
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
    events: deque[AgentEvent] = field(default_factory=lambda: deque(maxlen=500))
    cancelled: Event = field(default_factory=Event, repr=False)
    thread: Thread | None = field(default=None, repr=False)
    next_sequence: int = field(default=1, repr=False)
    pending_approval: ApprovalRequest | None = field(default=None, repr=False)
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
            "event_count": self.next_sequence - 1,
            "approval": (
                {"id": self.pending_approval.id, "command": self.pending_approval.command}
                if self.pending_approval
                else None
            ),
        }


class AgentService:
    """Creates, runs, observes, and cancels local Agent tasks."""

    def __init__(self, config: Config, model_factory: ModelFactory | None = None, skill_manager: SkillManager | None = None):
        self.config = config
        self.model_factory = model_factory or self._default_model_factory
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = Lock()
        self.max_tasks = 100
        self._request_limiter = ModelRequestLimiter(config.model_min_request_interval_ms)
        self.skill_manager = skill_manager or SkillManager(SkillManager.default_roots(config.workspace))
        self.selected_skills: tuple[str, ...] = ()
        self.memory_store = MemoryStore(config.memory_db)
        self.store = TaskStore(config.history_db) if config.history_db else None
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

    @staticmethod
    def _default_model_factory(config: Config, demo: bool) -> ModelClient:
        return DemoModel() if demo else OpenAICompatibleClient(config)

    def create_task(self, task: str, *, demo: bool | None = None, resume_from: str | None = None) -> TaskRecord:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string")
        if demo is None:
            demo = bool(getattr(self, "demo_default", False))
        parent: TaskRecord | None = None
        if resume_from:
            parent = self.get_task(resume_from)
            persisted_session = self.store.load_session(resume_from) if self.store else None
            if not parent or parent.status != "completed" or (not parent.agent and not persisted_session):
                raise ValueError("completed task context is unavailable for continuation")
        record = TaskRecord(id=uuid.uuid4().hex, task=task.strip())
        with self._lock:
            self._tasks[record.id] = record
            if self.store:
                self.store.save_task(record.snapshot())
            self._trim_tasks_locked(exclude_id=record.id)
        record.thread = Thread(target=self._run, args=(record, demo, parent), name=f"agent-{record.id[:8]}", daemon=True)
        record.thread.start()
        return record

    def update_config(self, config: Config) -> None:
        with self._lock:
            if any(record.status in {"queued", "running"} for record in self._tasks.values()):
                raise ValueError("cannot change runtime configuration while a task is active")
            if config.workspace != self.config.workspace:
                raise ValueError("workspace changes require a new service")
            self.config = config
            self._request_limiter = ModelRequestLimiter(config.model_min_request_interval_ms)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            records = sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)
            return [record.snapshot() for record in records[offset : offset + limit if limit is not None else None]]

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

    def approve_command(self, task_id: str, approval_id: str, approved: bool) -> bool:
        record = self.get_task(task_id)
        if not record or not isinstance(approved, bool):
            return False
        with self._lock:
            request = record.pending_approval
            if not request or request.id != approval_id or request.resolved.is_set():
                return False
            request.approved = approved
            request.resolved.set()
            return True

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

    def compact_task(self, task_id: str) -> CompactionResult | None:
        record = self.get_task(task_id)
        return record.agent.compact_context(force=True) if record and record.agent else None

    def _run(self, record: TaskRecord, demo: bool, parent: TaskRecord | None = None) -> None:
        def emit(event_type: str, data: dict[str, Any]) -> None:
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
                )

            if parent and parent.agent:
                agent = parent.agent
            else:
                agent = new_agent()
                if parent:
                    session = self.store.load_session(parent.id) if self.store else None
                    if not session:
                        raise ValueError("persisted task context is unavailable")
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
                )
            else:
                record.result = agent.run(record.task)
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
        request = ApprovalRequest(id=uuid.uuid4().hex, command=command)
        with self._lock:
            record.pending_approval = request
        self._emit(record, "command_approval_requested", {"approval_id": request.id, "command": command})
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
        self._emit(record, "command_approval_resolved", {"approval_id": request.id, "command": command, "decision": decision})
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
