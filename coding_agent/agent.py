import json
import time
from threading import Lock
from typing import Any, Callable, Protocol

from .config import Config
from .llm_client import LLMError, LLMRequestError
from .tools import ToolRegistry


class ModelClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


class ModelRequestCancelled(LLMError):
    """Raised when cancellation happens while waiting for a model request slot."""


class ModelRequestLimiter:
    """Thread-safe minimum-interval gate shared by model clients in one process."""

    def __init__(self, minimum_interval_ms: int = 0):
        self.minimum_interval = max(0, minimum_interval_ms) / 1000
        self._next_request_at = 0.0
        self._lock = Lock()

    def wait(self, is_cancelled: Callable[[], bool]) -> int:
        started = time.monotonic()
        while True:
            if is_cancelled():
                raise ModelRequestCancelled("model request was cancelled while waiting for rate limit")
            with self._lock:
                now = time.monotonic()
                delay = self._next_request_at - now
                if delay <= 0:
                    self._next_request_at = now + self.minimum_interval
                    return round((now - started) * 1000)
            time.sleep(min(delay, 0.1))


class DemoModel:
    """Offline placeholder for testing the CLI without a model API key."""

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        return {"role": "assistant", "content": "离线演示模式已启动。真实模型模式需要配置 LLM_API_KEY。"}


class DemoModel:
    """Deterministic local model used to demonstrate the complete tool loop offline."""

    @staticmethod
    def _tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [message for message in messages if message.get("role") == "tool"]

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        tool_messages = self._tool_messages(messages)
        if not tool_messages:
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "demo-list", "name": "list_files", "arguments": "{}"}]}
        if len(tool_messages) == 1:
            files = tool_messages[-1].get("content", {}).get("files", [])
            path = next((item for item in files if item.lower().endswith((".md", ".txt", ".py"))), "README.md")
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "demo-read", "name": "read_file", "arguments": json.dumps({"path": path})}]}
        if len(tool_messages) == 2:
            source = tool_messages[-1].get("content", {})
            content = source.get("content", "") if isinstance(source, dict) else str(source)
            report = "Coding Agent demo report\n\nRead source characters: " + str(len(content)) + "\n"
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "demo-write", "name": "write_file", "arguments": json.dumps({"path": ".coding-agent-demo/result.txt", "content": report})}]}
        if len(tool_messages) == 3:
            command = "python -c \"from pathlib import Path; print(Path('.coding-agent-demo/result.txt').read_text())\""
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "demo-run", "name": "run_command", "arguments": json.dumps({"command": command})}]}
        return {"role": "assistant", "content": "离线演示已完成：列出文件、读取文本、写入 .coding-agent-demo/result.txt，并执行命令验证了写入结果。"}


class Agent:
    def __init__(
        self,
        config: Config,
        model: ModelClient | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        sleeper: Callable[[float], None] | None = None,
        request_limiter: ModelRequestLimiter | None = None,
        command_approval: Callable[[str, Callable[[], bool]], str] | None = None,
    ):
        self.config = config
        self.registry = ToolRegistry(config)
        self.model = model or DemoModel()
        self.messages: list[dict[str, Any]] = []
        self.execution_log: list[dict[str, Any]] = []
        self.last_status = "idle"
        self._event_callback = event_callback or (lambda _type, _data: None)
        self._is_cancelled = is_cancelled or (lambda: False)
        self._sleeper = sleeper or time.sleep
        self._request_limiter = request_limiter or ModelRequestLimiter(config.model_min_request_interval_ms)
        self._command_approval = command_approval

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
            except ModelRequestCancelled:
                self.last_status = "cancelled"
                return "Agent task was cancelled."
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
                result = (
                    {"ok": False, "error": error}
                    if error
                    else self.registry.execute(
                        name,
                        arguments,
                        is_cancelled=self._is_cancelled,
                        request_approval=self._command_approval,
                    )
                )
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
                waited_ms = self._request_limiter.wait(self._is_cancelled)
                if waited_ms:
                    self._emit("model_rate_limited", {"waited_ms": waited_ms})
                return self.model.complete(self._context_messages(), self.registry.schemas())
            except LLMRequestError:
                if attempt == attempts - 1:
                    raise
                delay = self.config.model_retry_base_delay_ms / 1000 * (2**attempt)
                self._emit("model_retrying", {"attempt": attempt + 1, "delay_ms": round(delay * 1000)})
                self._sleeper(delay)
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
