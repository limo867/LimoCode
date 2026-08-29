"""In-process application service for every user interface."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable
import uuid

from .agent import Agent, DemoModel, ModelClient, ModelRequestLimiter
from .config import Config
from .events import AgentEvent
from .llm_client import OpenAICompatibleClient
from .storage import TaskStore


ModelFactory = Callable[[Config, bool], ModelClient]


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
            "event_count": len(self.events),
        }


class AgentService:
    """Creates, runs, observes, and cancels local Agent tasks."""

    def __init__(self, config: Config, model_factory: ModelFactory | None = None):
        self.config = config
        self.model_factory = model_factory or self._default_model_factory
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = Lock()
        self.max_tasks = 100
        self._request_limiter = ModelRequestLimiter(config.model_min_request_interval_ms)
        self.store = TaskStore(config.history_db) if config.history_db else None
        if self.store:
            for snapshot in self.store.load_tasks():
                record = TaskRecord(**snapshot)
                record.events.extend(self.store.load_events(record.id))
                record.next_sequence = (record.events[-1].sequence + 1) if record.events else 1
                if record.status in {"queued", "running"}:
                    record.transition("failed")
                    record.error = "task interrupted because the service restarted"
                    self.store.save_task(record.snapshot())
                self._tasks[record.id] = record

    @staticmethod
    def _default_model_factory(config: Config, demo: bool) -> ModelClient:
        return DemoModel() if demo else OpenAICompatibleClient(config)

    def create_task(self, task: str, *, demo: bool | None = None) -> TaskRecord:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string")
        if demo is None:
            demo = bool(getattr(self, "demo_default", False))
        record = TaskRecord(id=uuid.uuid4().hex, task=task.strip())
        with self._lock:
            self._tasks[record.id] = record
            if self.store:
                self.store.save_task(record.snapshot())
            self._trim_tasks_locked(exclude_id=record.id)
        record.thread = Thread(target=self._run, args=(record, demo), name=f"agent-{record.id[:8]}", daemon=True)
        record.thread.start()
        return record

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
        events = [event for event in record.events if event.sequence > after]
        return events[:limit] if limit is not None else events

    def cancel_task(self, task_id: str) -> bool:
        record = self.get_task(task_id)
        if not record or record.status in {"completed", "failed", "cancelled"}:
            return False
        record.cancelled.set()
        self._emit(record, "task_cancelling", {})
        return True

    def _run(self, record: TaskRecord, demo: bool) -> None:
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
            agent = Agent(
                self.config,
                model=model,
                event_callback=emit,
                is_cancelled=record.cancelled.is_set,
                request_limiter=self._request_limiter,
            )
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
