"""Small, bounded read-only subagent runner used by the parent Agent."""

from dataclasses import dataclass
from typing import Any, Callable

from .agent import Agent, ModelClient, ModelRequestLimiter
from .changes import ChangeSetManager
from .config import Config


@dataclass(frozen=True)
class SubagentResult:
    role: str
    task: str
    status: str
    summary: str
    files: tuple[str, ...] = ()
    changeset_ids: tuple[str, ...] = ()
    operations: int = 0
    error: str | None = None
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "task": self.task,
            "status": self.status,
            "summary": self.summary,
            "files": list(self.files),
            "changeset_ids": list(self.changeset_ids),
            "operations": self.operations,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


class SubagentManager:
    """Run first-phase specialists without allowing recursive delegation."""

    ROLES = {
        "explorer": "检查工作区，定位相关文件、架构和当前行为。",
        "reviewer": "审查当前实现是否符合用户请求，识别遗漏、风险和不一致之处。",
        "verifier": "运行安全的本地编译或测试命令，并报告实际观察到的结果。",
        "implementer": "在限定范围内实现一项经过验证的修改。",
    }

    def __init__(self, config: Config, model: ModelClient, request_limiter: ModelRequestLimiter, max_turns: int = 16):
        self.config = config
        self.model = model
        self.request_limiter = request_limiter
        # Specialists are intentionally bounded independently from the parent,
        # but twelve rounds is too easy to exhaust during multi-file checks.
        self.max_turns = max(1, min(max_turns, 24))

    def run(
        self,
        role: str,
        task: str,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        change_manager: ChangeSetManager | None = None,
        task_id: str | None = None,
        change_callback: Callable[[str, str], dict[str, Any]] | None = None,
        command_approval: Callable[[str, Callable[[], bool]], str] | None = None,
        command_policy: Callable[[str], bool] | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> SubagentResult:
        normalized_role = str(role or "").strip().lower()
        prompt = str(task or "").strip()
        if normalized_role not in self.ROLES:
            return SubagentResult(normalized_role or "unknown", prompt, "failed", "不支持的子智能体角色。", error="角色必须是 explorer、reviewer、verifier 或 implementer")
        if not prompt:
            return SubagentResult(normalized_role, prompt, "failed", "子智能体任务为空。", error="任务不能为空")
        is_implementer = normalized_role == "implementer"
        tools = (
            ("list_files", "read_file", "write_file", "run_command")
            if is_implementer
            else ("list_files", "read_file", "run_command")
            if normalized_role == "verifier"
            else ("list_files", "read_file")
        )
        child = Agent(
            self.config.with_overrides(max_turns=self.max_turns),
            model=self.model,
            event_callback=event_callback,
            is_cancelled=is_cancelled,
            request_limiter=self.request_limiter,
            # No recursive spawning. Only Implementer receives write_file;
            # its callback routes through the parent's ChangeSet lifecycle.
            allowed_tools=tools,
            restricted_commands=normalized_role == "verifier",
            change_manager=change_manager if is_implementer else None,
            task_id=task_id if is_implementer else None,
            change_callback=change_callback if is_implementer else None,
            command_approval=command_approval if is_implementer else None,
            command_policy=command_policy if is_implementer else None,
        )
        instructions = (
            f"你是 {normalized_role} 专项智能体。{self.ROLES[normalized_role]}"
            + (
                "只能运行标准的本地编译、测试或可执行程序命令；绝不能创建、修改、删除、重定向、下载或安装任何内容。请用中文简洁报告。"
                if normalized_role == "verifier"
                else "你必须严格只读。请用中文输出简洁的结构化报告，包含发现、文件路径和建议。"
                if not is_implementer
                else "只能用 write_file 完成限定范围内的实现。每次文件变更都会进入父任务的审批流程。请使用安全的本地命令验证，并用中文报告变更文件和验证结果。"
            )
        )
        try:
            summary = child.run(f"{instructions}\n\nAssignment:\n{prompt}")
            files = tuple(sorted({
                str(entry.get("arguments", {}).get("path"))
                for entry in child.execution_log
                if entry.get("tool") in {"read_file", "write_file"} and entry.get("arguments", {}).get("path")
            }))
            changeset_ids = tuple(sorted({
                str(entry.get("result", {}).get("changeset_id"))
                for entry in child.execution_log
                if entry.get("tool") == "write_file" and entry.get("result", {}).get("changeset_id")
            }))
            status = "completed" if child.last_status == "completed" else child.last_status
            return SubagentResult(normalized_role, prompt, status, str(summary or "未返回审查结论。")[:8000], files[:30], changeset_ids, len(child.execution_log))
        except Exception as exc:
            return SubagentResult(normalized_role, prompt, "failed", "子智能体未能完成报告。", error=str(exc)[:1000])
