import json
from typing import Any, Callable, Protocol

from .config import Config
from .llm_client import LLMError, LLMRequestError
from .tools import ToolRegistry


class ModelClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


class DemoModel:
    """Offline placeholder for testing the CLI without a model API key."""

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        return {"role": "assistant", "content": "离线演示模式已启动。真实模型模式需要配置 LLM_API_KEY。"}


class Agent:
    def __init__(
        self,
        config: Config,
        model: ModelClient | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ):
        self.config = config
        self.registry = ToolRegistry(config)
        self.model = model or DemoModel()
        self.messages: list[dict[str, Any]] = []
        self.execution_log: list[dict[str, Any]] = []
        self.last_status = "idle"
        self._event_callback = event_callback or (lambda _type, _data: None)
        self._is_cancelled = is_cancelled or (lambda: False)

    def run(self, task: str) -> str:
        self.messages = [
            {"role": "system", "content": "You are a coding agent. Use local tools to complete the user's task."},
            {"role": "user", "content": task},
        ]
        self.execution_log = []
        self.last_status = "running"
        self._emit("task_started", {"task": task})
        for turn in range(1, self.config.max_turns + 1):
            if self._is_cancelled():
                self.last_status = "cancelled"
                return "Agent task was cancelled."
            self._emit("model_thinking", {"turn": turn})
            try:
                response = self._complete_with_retry()
            except LLMError as exc:
                self.last_status = "failed"
                result = self._failure_summary("model request", str(exc))
                self._emit("task_error", {"stage": "model request", "error": str(exc)})
                return result
            assistant_message = {
                "role": "assistant",
                "content": response.get("content", ""),
            }
            calls = response.get("tool_calls") or []
            if calls:
                assistant_message["tool_calls"] = calls
            self.messages.append(assistant_message)
            if not calls:
                self.last_status = "completed"
                self._emit("assistant_message", {"content": assistant_message["content"]})
                return assistant_message["content"]
            for index, call in enumerate(calls, start=1):
                if self._is_cancelled():
                    self.last_status = "cancelled"
                    return "Agent task was cancelled."
                call_id = call.get("id", f"turn-{turn}-call-{index}") if isinstance(call, dict) else f"turn-{turn}-call-{index}"
                name = call.get("name", "") if isinstance(call, dict) else ""
                arguments, error = self._parse_arguments(call.get("arguments") if isinstance(call, dict) else None)
                self._emit("tool_started", {"turn": turn, "tool": name or "<invalid>", "arguments": self._argument_summary(arguments)})
                result = {"ok": False, "error": error} if error else self.registry.execute(name, arguments)
                self.execution_log.append(
                    {
                        "turn": turn,
                        "tool": name or "<invalid>",
                        "arguments": self._argument_summary(arguments),
                        "ok": result.get("ok", False),
                        "error": result.get("error"),
                    }
                )
                self.messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                self._emit("tool_finished", {"turn": turn, "tool": name or "<invalid>", "result": result})
        count = len(self.execution_log)
        self.last_status = "limit_reached"
        return f"Agent stopped after reaching the maximum turn limit ({self.config.max_turns}); executed {count} tool call(s)."

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        self._event_callback(event_type, data)

    def _complete_with_retry(self) -> dict[str, Any]:
        attempts = max(0, self.config.model_retries) + 1
        for attempt in range(attempts):
            try:
                return self.model.complete(self._context_messages(), self.registry.schemas())
            except LLMRequestError:
                if attempt == attempts - 1:
                    raise
        raise RuntimeError("unreachable")

    def _context_messages(self) -> list[dict[str, Any]]:
        """Keep instructions and the newest interactions within bounded context."""
        if len(self.messages) <= 2:
            return list(self.messages)
        head = self.messages[:2]
        recent = self.messages[2:]
        limit = max(1, self.config.max_history_messages)
        omitted = len(recent) > limit
        recent = recent[-limit:]
        if omitted:
            head = [
                head[0],
                {
                    "role": "user",
                    "content": f"{head[1]['content']}\n\nEarlier tool interactions were omitted to keep the context bounded.",
                },
            ]
        return [self._truncate_message(message) for message in head + recent]

    def _truncate_message(self, message: dict[str, Any]) -> dict[str, Any]:
        result = dict(message)
        content = result.get("content")
        if isinstance(content, str) and len(content) > self.config.max_history_chars:
            result["content"] = content[: self.config.max_history_chars] + "\n[truncated]"
        elif isinstance(content, dict):
            result["content"] = self._truncate_value(content)
        return result

    def _truncate_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return value[: self.config.max_history_chars] + ("\n[truncated]" if len(value) > self.config.max_history_chars else "")
        if isinstance(value, dict):
            return {key: self._truncate_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._truncate_value(item) for item in value]
        return value

    def _failure_summary(self, stage: str, reason: str) -> str:
        tool_summary = ", ".join(entry["tool"] for entry in self.execution_log[-5:]) or "no tools executed"
        return f"Agent failed during {stage}: {reason}. Recent operations: {tool_summary}."

    @staticmethod
    def _parse_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None]:
        if isinstance(raw_arguments, dict):
            return raw_arguments, None
        if raw_arguments in (None, ""):
            return {}, "tool arguments are missing"
        if not isinstance(raw_arguments, str):
            return {}, "tool arguments must be a JSON object"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}, "tool arguments are not valid JSON"
        if not isinstance(arguments, dict):
            return {}, "tool arguments must be a JSON object"
        return arguments, None

    @staticmethod
    def _argument_summary(arguments: dict[str, Any]) -> dict[str, str]:
        return {key: str(value)[:160] for key, value in arguments.items()}
