import copy
import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Protocol

from .config import Config
from .changes import ChangeSetManager
from .context import CompactionResult, ContextManager
from .llm_client import LLMError, LLMRequestError
from .memory import MemoryMatch, MemoryStore
from .skills import SkillManager
from .tools import ToolRegistry


class ModelClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


class ModelRequestCancelled(LLMError):
    """Raised when cancellation happens while waiting for a model request slot."""


_FALLBACK_AGENT_INSTRUCTIONS = """
你是 LimoCode，一个在本地工作区中执行编程任务的 coding agent。
面向用户的进度说明、工具结果解释、错误说明、子任务报告和最终答复一律使用简体中文。
代码、命令、文件路径、类名、函数名、日志和错误码可以原样保留。
不要输出隐藏的逐步思维链；需要说明思路时只给出简短、可审计的行动摘要。
先检查现状再修改，修改后运行匹配的编译、测试或静态检查，并如实报告退出码。
工具失败时说明原因并在必要时重试，不要把失败伪装成成功。最终答复使用有效 Markdown。
""".strip()


def _load_agent_instructions() -> str:
    """Load the editable language/workflow policy shipped with the package."""
    prompt_path = Path(__file__).with_name("prompts") / "agent_system.md"
    try:
        content = prompt_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return _FALLBACK_AGENT_INSTRUCTIONS
    return content or _FALLBACK_AGENT_INSTRUCTIONS


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
    _PROJECT_MEMORY_HEADER = "## Project Memory"

    def __init__(
        self,
        config: Config,
        model: ModelClient | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        sleeper: Callable[[float], None] | None = None,
        request_limiter: ModelRequestLimiter | None = None,
        command_approval: Callable[[str, Callable[[], bool]], str] | None = None,
        skill_manager: SkillManager | None = None,
        selected_skills: tuple[str, ...] = (),
        memory_store: MemoryStore | None = None,
        context_manager: ContextManager | None = None,
        change_manager: ChangeSetManager | None = None,
        task_id: str | None = None,
        change_callback: Callable[[str, str], dict[str, Any]] | None = None,
        command_policy: Callable[[str], bool] | None = None,
        allowed_tools: tuple[str, ...] | None = None,
        subagent_runner: Callable[[str, str], dict[str, Any]] | None = None,
        restricted_commands: bool = False,
    ):
        self.config = config
        self.registry = ToolRegistry(
            config,
            change_manager=change_manager,
            task_id=task_id,
            change_callback=change_callback,
            command_policy=command_policy,
            allowed_tools=allowed_tools,
            subagent_runner=subagent_runner,
            restricted_commands=restricted_commands,
        )
        self.model = model or DemoModel()
        self.messages: list[dict[str, Any]] = []
        self.execution_log: list[dict[str, Any]] = []
        self.last_status = "idle"
        self._event_callback = event_callback or (lambda _type, _data: None)
        self._is_cancelled = is_cancelled or (lambda: False)
        self._sleeper = sleeper or time.sleep
        self._request_limiter = request_limiter or ModelRequestLimiter(config.model_min_request_interval_ms)
        self._command_approval = command_approval
        self._skill_manager = skill_manager
        self._selected_skills = selected_skills
        self._memory_store = memory_store
        self._context_manager = context_manager or ContextManager(config)
        self._task = ""
        self._instruction_prefix = ""
        self._last_compaction: CompactionResult | None = None

    def run(self, task: str, initial_context: str | None = None) -> str:
        self._task = task
        self._last_compaction = None
        instructions = _load_agent_instructions() + "\n\n" + (
            "使用本地工具完成任务。Shell 是 Windows cmd.exe；文件写入使用 write_file；检查命令使用非交互方式。"
            "复杂调查可以委派 explorer、reviewer、verifier 或一个 implementer，并根据报告继续工作。"
            f"文件修改策略为 {self.config.permission_mode}，不能自行改变该策略。"
        )
        if self._skill_manager:
            active_skills = self._skill_manager.select(task, self._selected_skills)
            if active_skills:
                instructions += "\n\nActive skills:\n" + "\n\n".join(
                    f"## {skill.name}\n{skill.instructions}" for skill in active_skills
                )
                self._emit("skills_loaded", {"skills": [skill.name for skill in active_skills], "automatic": not self._selected_skills})
        if initial_context:
            instructions += "\n\n## Specialist report\n" + initial_context[:8000]
        self._instruction_prefix = instructions
        self.messages = [{"role": "system", "content": instructions}, {"role": "user", "content": task}]
        self._refresh_project_memory(task)
        self.execution_log = []
        self.last_status = "running"
        self._emit("task_started", {"task": task})
        return self._run_loop()

    def continue_task(
        self,
        follow_up: str,
        *,
        config: Config,
        model: ModelClient,
        event_callback: Callable[[str, dict[str, Any]], None],
        is_cancelled: Callable[[], bool],
        request_limiter: ModelRequestLimiter,
        command_approval: Callable[[str, Callable[[], bool]], str] | None,
        parent_task_id: str | None = None,
    ) -> str:
        """Continue a finished task with the existing conversation on a new model."""
        if not self.messages:
            return self.run(follow_up)
        self.config = config
        self.model = model
        self._event_callback = event_callback
        self._is_cancelled = is_cancelled
        self._request_limiter = request_limiter
        self._command_approval = command_approval
        self._context_manager = ContextManager(config)
        # Existing sessions may have been created before the language policy
        # was introduced. Refresh the policy for every continuation without
        # discarding the session's skills or project-specific instructions.
        system_message = self.messages[0] if self.messages and self.messages[0].get("role") == "system" else None
        if system_message is not None:
            system_content = str(system_message.get("content") or "")
            policy = _load_agent_instructions()
            if "面向用户的进度说明" not in system_content:
                system_content = f"{system_content.rstrip()}\n\n{policy}".strip()
                system_message["content"] = system_content
                self._instruction_prefix = self._strip_project_memory(system_content)
        self.messages.append({"role": "user", "content": follow_up})
        self._task = f"{self._task}\n\nFollow-up: {follow_up}"
        self.last_status = "running"
        event_data: dict[str, Any] = {"task": follow_up}
        if parent_task_id:
            event_data["parent_task_id"] = parent_task_id
        self._emit("task_resumed", event_data)
        self._refresh_project_memory(self._task)
        return self._run_loop()

    def restore_session(self, task_context: str, messages: list[dict[str, Any]]) -> None:
        if not task_context or len(messages) < 2:
            raise ValueError("session state is incomplete")
        self._task = task_context
        # A restart can occur after an assistant tool-call message was saved
        # but before its local tool result was persisted. Make the restored
        # OpenAI-compatible transcript valid before sending it back to a
        # provider, and never let a resumed branch mutate its parent snapshot.
        self.messages = self._repair_tool_call_history(messages)
        first_message = self.messages[0] if self.messages else {}
        content = first_message.get("content", "") if isinstance(first_message, dict) else ""
        self._instruction_prefix = self._strip_project_memory(content) if isinstance(content, str) else ""
        self._last_compaction = None
        self.last_status = "completed"

    def session_state(self) -> tuple[str, list[dict[str, Any]]]:
        return self._task, copy.deepcopy(self.messages)

    @staticmethod
    def _repair_tool_call_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return a protocol-valid copy of a persisted tool-call transcript.

        The service stores a snapshot for every public event. If it stops
        between ``tool_started`` and ``tool_finished``, an assistant's tool
        call has no matching tool response and many OpenAI-compatible APIs
        reject the next request. Missing calls get an explicit interrupted
        result; orphaned tool messages are discarded because they cannot be
        attached to an assistant message safely.
        """
        repaired: list[dict[str, Any]] = []
        pending: set[str] = set()

        def close_pending() -> None:
            nonlocal pending
            for call_id in sorted(pending):
                repaired.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": {
                            "ok": False,
                            "error": "tool call interrupted before its result was recorded; re-evaluate the workspace state",
                        },
                    }
                )
            pending = set()

        for message_index, raw_message in enumerate(messages):
            if not isinstance(raw_message, dict):
                continue
            message = copy.deepcopy(raw_message)
            role = message.get("role")
            if role == "tool":
                call_id = message.get("tool_call_id")
                if isinstance(call_id, str) and call_id in pending:
                    repaired.append(message)
                    pending.remove(call_id)
                continue

            close_pending()
            if role == "assistant":
                raw_calls = message.get("tool_calls")
                if raw_calls is not None and not isinstance(raw_calls, list):
                    message.pop("tool_calls", None)
                    raw_calls = []
                if isinstance(raw_calls, list):
                    calls: list[dict[str, Any]] = []
                    for call_index, raw_call in enumerate(raw_calls):
                        if not isinstance(raw_call, dict):
                            continue
                        call = copy.deepcopy(raw_call)
                        call_id = call.get("id")
                        if not isinstance(call_id, str) or not call_id:
                            call_id = f"restored-call-{message_index}-{call_index}"
                            call["id"] = call_id
                        calls.append(call)
                        pending.add(call_id)
                    if calls:
                        message["tool_calls"] = calls
                    else:
                        message.pop("tool_calls", None)
            repaired.append(message)
        close_pending()
        return repaired

    def context_status(self) -> dict[str, Any]:
        """Return inspectable short-term-memory state without exposing full prompts."""
        messages = list(self.messages)
        status = self._context_manager.status(messages)
        status["task_chars"] = len(self._task)
        status["last_compaction"] = (
            {
                "before_tokens": self._last_compaction.before_tokens,
                "after_tokens": self._last_compaction.after_tokens,
            }
            if self._last_compaction and self._last_compaction.compacted
            else None
        )
        return status

    def _refresh_project_memory(self, query: str) -> list[MemoryMatch]:
        """Replace the marked memory section so continuation sees current rules."""
        if not self._memory_store or not self.messages:
            return []
        base = self._instruction_prefix or self._strip_project_memory(str(self.messages[0].get("content", "")))
        self._instruction_prefix = base
        # Reserve room for the header and up to six memory-id line prefixes.
        header_overhead = len(self._PROJECT_MEMORY_HEADER) + 192
        available_chars = max(
            0,
            min(
                max(0, self.config.memory_context_chars),
                max(0, self.config.max_history_chars - len(base) - header_overhead),
            ),
        )
        matches = self._memory_store.retrieve_matches(query, max_chars=available_chars)
        content = base
        if matches:
            content += "\n\n" + self._PROJECT_MEMORY_HEADER + "\n" + "\n".join(
                f"- [memory:{match.item.id}] {match.item.content}" for match in matches
            )
        self.messages[0] = {"role": "system", "content": content}
        storage = self._memory_store.status()
        self._emit(
            "memory_retrieved",
            {
                "memory_ids": [match.item.id for match in matches],
                "matches": [
                    {
                        "id": match.item.id,
                        "source": match.item.source,
                        "score": match.score,
                        "matched_terms": list(match.matched_terms[:6]),
                        "preview": match.item.content.replace("\n", " ")[:160],
                        "truncated": match.truncated,
                    }
                    for match in matches
                ],
                "injected_chars": sum(len(match.item.content) for match in matches),
                "budget_chars": available_chars,
                "storage_available": storage["available"],
                "storage_error": storage["error"],
            },
        )
        return matches

    @classmethod
    def _strip_project_memory(cls, instructions: str) -> str:
        for marker in (f"\n\n{cls._PROJECT_MEMORY_HEADER}\n", "\n\nRelevant project memory:\n"):
            if marker in instructions:
                return instructions.split(marker, 1)[0].rstrip()
        return instructions

    def _run_loop(self) -> str:
        for turn in range(1, self.config.max_turns + 1):
            if self._is_cancelled():
                self.last_status = "cancelled"
                return "Agent task was cancelled."
            self._emit("model_thinking", {"turn": turn, "message": "正在思考"})
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
            if self._is_cancelled():
                self.last_status = "cancelled"
                return "Agent task was cancelled."
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
                if self._memory_store:
                    saved = self._memory_store.extract_from_task(self._task)
                    if saved:
                        self._emit("memory_saved", {"memory_ids": [item.id for item in saved], "source": "automatic"})
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
                        # Keep the structured result available to bounded
                        # specialists (for example, an Implementer needs the
                        # generated changeset id) without changing the model
                        # transcript contract.
                        "result": copy.deepcopy(result),
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
        self.compact_context()
        attempts = max(0, self.config.model_retries) + 1
        for attempt in range(attempts):
            try:
                waited_ms = self._request_limiter.wait(self._is_cancelled)
                if waited_ms:
                    self._emit("model_rate_limited", {"waited_ms": waited_ms})
                stream_complete = getattr(self.model, "complete_stream", None)
                if callable(stream_complete):
                    return stream_complete(
                        self._context_messages(),
                        self.registry.schemas(),
                        lambda delta: (
                            self._emit("assistant_delta", {"delta": delta})
                            if not self._is_cancelled()
                            else None
                        ),
                    )
                return self.model.complete(self._context_messages(), self.registry.schemas())
            except LLMRequestError as exc:
                # A local policy denial (for example Windows WinError 10013)
                # cannot be repaired by waiting and repeating the identical
                # request. Surface it immediately with its actionable detail.
                if not exc.retryable:
                    raise
                if attempt == attempts - 1:
                    raise
                delay = self.config.model_retry_base_delay_ms / 1000 * (2**attempt)
                self._emit("model_retrying", {"attempt": attempt + 1, "delay_ms": round(delay * 1000)})
                self._sleeper(delay)
        raise RuntimeError("unreachable")

    def _context_messages(self) -> list[dict[str, Any]]:
        return [self._truncate_message(message) for message in self.messages]

    def compact_context(self, force: bool = False) -> CompactionResult:
        result = self._context_manager.compact(self.messages, self._task, force=force)
        if result.compacted:
            self.messages = result.messages
            self._last_compaction = result
            self._emit(
                "context_compacted",
                {"before_tokens": result.before_tokens, "after_tokens": result.after_tokens, "manual": force},
            )
        return result

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
