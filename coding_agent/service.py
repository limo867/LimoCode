"""In-process application service for every user interface."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable
import uuid

from .agent import Agent, DemoModel, ModelClient
from .config import Config
from .events import AgentEvent
from .llm_client import OpenAICompatibleClient


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

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
        }


class AgentService:
    """Creates, runs, observes, and cancels local Agent tasks."""

    def __init__(self, config: Config, model_factory: ModelFactory | None = None):
        self.config = config
        self.model_factory = model_factory or self._default_model_factory
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = Lock()

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
        record.thread = Thread(target=self._run, args=(record, demo), name=f"agent-{record.id[:8]}", daemon=True)
        record.thread.start()
        return record

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def events(self, task_id: str, after: int = 0) -> list[AgentEvent]:
        record = self.get_task(task_id)
        if not record:
            return []
        return list(record.events)[after:]

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
            record.status = "running"
            model = self.model_factory(self.config, demo)
            agent = Agent(self.config, model=model, event_callback=emit, is_cancelled=record.cancelled.is_set)
            record.result = agent.run(record.task)
            if record.cancelled.is_set() or agent.last_status == "cancelled":
                record.status = "cancelled"
                self._emit(record, "task_cancelled", {"result": record.result})
            elif agent.last_status == "failed":
                record.status = "failed"
                record.error = record.result
                self._emit(record, "task_error", {"error": record.error})
            else:
                record.status = "completed"
                self._emit(record, "task_finished", {"result": record.result, "status": agent.last_status})
        except Exception as exc:
            record.status = "failed"
            record.error = str(exc)
            self._emit(record, "task_error", {"error": record.error})

    def _emit(self, record: TaskRecord, event_type: str, data: dict[str, Any]) -> None:
        record.events.append(AgentEvent(type=event_type, task_id=record.id, data=data))
