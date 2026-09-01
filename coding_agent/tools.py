from dataclasses import dataclass
import difflib
import re
import subprocess
import time
from typing import Any, Callable

from .config import Config
from .changes import ChangeSetManager
from .workspace import Workspace


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., dict[str, Any]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Tool declarations and local execution live here, outside the model client."""

    def __init__(
        self,
        config: Config,
        *,
        change_manager: ChangeSetManager | None = None,
        task_id: str | None = None,
        change_callback: Callable[[str, str], dict[str, Any]] | None = None,
        command_policy: Callable[[str], bool] | None = None,
        allowed_tools: tuple[str, ...] | None = None,
        subagent_runner: Callable[[str, str], dict[str, Any]] | None = None,
        restricted_commands: bool = False,
    ):
        self.workspace = Workspace(config.workspace)
        self.config = config
        self.change_manager = change_manager
        self.task_id = task_id
        self.change_callback = change_callback
        self.command_policy = command_policy or (lambda _command: False)
        self.subagent_runner = subagent_runner
        self.restricted_commands = restricted_commands
        built = self._build_tools()
        if allowed_tools is not None:
            built = [tool for tool in built if tool.name in set(allowed_tools)]
        self.tools = {tool.name: tool for tool in built}

    def _build_tools(self) -> list[Tool]:
        string_path = {"type": "string", "description": "Path relative to workspace"}
        tools = [
            Tool(
                "list_files",
                "List files in the workspace.",
                {"type": "object", "properties": {"path": {"type": "string"}, "max_entries": {"type": "integer", "minimum": 1, "maximum": 500}}, "additionalProperties": False},
                self.list_files,
            ),
            Tool(
                "read_file",
                "Read a UTF-8 text file.",
                {"type": "object", "properties": {"path": string_path}, "required": ["path"], "additionalProperties": False},
                self.read_file,
            ),
            Tool(
                "write_file",
                "Create or overwrite a UTF-8 text file.",
                {"type": "object", "properties": {"path": string_path, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False},
                self.write_file,
            ),
            Tool(
                "run_command",
                "Run a command in the workspace with a timeout.",
                {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False},
                self.run_command,
            ),
        ]
        if self.subagent_runner:
            tools.append(Tool(
                "spawn_subagent",
                "Delegate a bounded task to Explorer, Reviewer, Verifier, or one Implementer specialist.",
                {"type": "object", "properties": {"role": {"type": "string", "enum": ["explorer", "reviewer", "verifier", "implementer"]}, "task": {"type": "string", "description": "Focused specialist assignment"}}, "required": ["role", "task"], "additionalProperties": False},
                self.spawn_subagent,
            ))
        return tools

    def spawn_subagent(self, role: str, task: str) -> dict[str, Any]:
        if not self.subagent_runner:
            return {"ok": False, "error": "subagents are unavailable"}
        result = self.subagent_runner(role, task)
        return {"ok": result.get("status") == "completed", **result}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        is_cancelled: Callable[[], bool] | None = None,
        request_approval: Callable[[str, Callable[[], bool]], str] | None = None,
    ) -> dict[str, Any]:
        tool = self.tools.get(name)
        if not tool:
            return {"ok": False, "error": f"unknown tool: {name}"}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "tool arguments must be an object"}
        allowed = set(tool.parameters.get("properties", {}))
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            return {"ok": False, "error": f"unknown argument(s): {', '.join(unknown)}"}
        required = set(tool.parameters.get("required", []))
        missing = sorted(required - set(arguments))
        if missing:
            return {"ok": False, "error": f"missing required argument(s): {', '.join(missing)}"}
        try:
            if name == "run_command":
                return tool.handler(
                    **arguments,
                    is_cancelled=is_cancelled or (lambda: False),
                    request_approval=request_approval,
                )
            return tool.handler(**arguments)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def list_files(self, path: str = "", max_entries: int = 500) -> dict[str, Any]:
        if not isinstance(path, str) or not isinstance(max_entries, int) or isinstance(max_entries, bool):
            return {"ok": False, "error": "path must be a string and max_entries must be an integer"}
        if not 1 <= max_entries <= 500:
            return {"ok": False, "error": "max_entries must be between 1 and 500"}
        target = self.workspace.resolve(path or ".")
        if not target.is_dir():
            return {"ok": False, "error": "list_files path must be a directory"}
        files = [p.relative_to(self.workspace.root).as_posix() for p in target.rglob("*") if p.is_file() and not ({".git", ".venv", "__pycache__"} & set(p.parts))]
        return {"ok": True, "files": sorted(files)[:max_entries], "truncated": len(files) > max_entries}

    def read_file(self, path: str) -> dict[str, Any]:
        if not isinstance(path, str) or not path.strip():
            return {"ok": False, "error": "path must be a non-empty string"}
        target = self.workspace.resolve(path)
        if not target.exists():
            return {"ok": False, "error": "file does not exist"}
        if not target.is_file():
            return {"ok": False, "error": "path is not a regular file"}
        if target.stat().st_size > self.config.max_file_chars * 4:
            # Discovery agents routinely encounter generated artifacts and
            # dependency bundles. Skipping them is expected, not a failed
            # workspace operation; the model can move on to relevant sources.
            return {
                "ok": True,
                "path": path,
                "skipped": True,
                "skip_reason": f"file exceeds {self.config.max_file_chars} character limit",
                "content": "[Skipped: file is too large to inspect safely.]",
            }
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {
                "ok": True,
                "path": path,
                "skipped": True,
                "skip_reason": "file is not valid UTF-8 text",
                "content": "[Skipped: file is binary or not UTF-8 text.]",
            }
        return {"ok": True, "path": path, "content": content[: self.config.max_file_chars], "truncated": len(content) > self.config.max_file_chars}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        if not isinstance(path, str) or not path.strip():
            return {"ok": False, "error": "path must be a non-empty string"}
        if not isinstance(content, str):
            return {"ok": False, "error": "content must be a string"}
        if len(content) > self.config.max_file_chars:
            return {"ok": False, "error": f"content exceeds {self.config.max_file_chars} character limit"}
        if self.change_callback and self.change_manager and self.task_id:
            return self.change_callback(path, content)
        target = self.workspace.resolve(path)
        if target.exists() and not target.is_file():
            return {"ok": False, "error": "path is not a regular file"}
        existed = target.exists()
        previous = ""
        if existed:
            try:
                previous = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return {"ok": False, "error": "existing file is not valid UTF-8 text"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        preview_limit = min(self.config.max_history_chars, 4000)
        diff_lines = list(
            difflib.unified_diff(
                previous.splitlines(),
                content.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
                n=3,
            )
        )
        diff_text = "\n".join(diff_lines)
        return {
            "ok": True,
            "path": path,
            "bytes": len(content.encode("utf-8")),
            "created": not existed,
            "changed": previous != content,
            "previous_preview": previous[:preview_limit],
            "content_preview": content[:preview_limit],
            "preview_truncated": len(previous) > preview_limit or len(content) > preview_limit,
            "added_lines": sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")),
            "removed_lines": sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---")),
            "unified_diff": diff_text[:preview_limit],
            "diff_truncated": len(diff_text) > preview_limit,
        }

    def run_command(
        self,
        command: str,
        is_cancelled: Callable[[], bool] | None = None,
        request_approval: Callable[[str, Callable[[], bool]], str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            return {"ok": False, "error": "command must be a non-empty string"}
        if self.restricted_commands and not (
            self._is_standard_development_command(command)
            or self._is_standard_readonly_command(command)
        ):
            return {
                "ok": False,
                "error": "验证员仅允许安全的本地检查、编译、测试和可执行文件运行命令",
                "restricted": True,
            }
        is_cancelled = is_cancelled or (lambda: False)
        requires_approval = self._is_dangerous(command) or (
            self.config.permission_mode == "approval"
            and self._may_mutate_workspace(command)
            and not self._is_standard_development_command(command)
        )
        always_allowed = not self._is_dangerous(command) and self.command_policy(command)
        if requires_approval and command not in self.config.approved_commands and not always_allowed:
            if not request_approval:
                return {"ok": False, "error": "command requires local approval", "requires_approval": True}
            decision = request_approval(command, is_cancelled)
            if decision != "approved":
                return {
                    "ok": False,
                    "error": f"command approval {decision}",
                    "requires_approval": True,
                    "approval_status": decision,
                }
        started = time.perf_counter()
        try:
            process = subprocess.Popen(command, cwd=self.workspace.root, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            return {"ok": False, "error": f"command could not be started: {exc}", "duration_ms": round((time.perf_counter() - started) * 1000)}
        stdout, stderr = "", ""
        while True:
            elapsed = time.perf_counter() - started
            if is_cancelled() or elapsed >= self.config.command_timeout:
                process.terminate()
                stdout, stderr = process.communicate()
                reason = "command cancelled" if is_cancelled() else f"command timed out after {self.config.command_timeout}s"
                return {"ok": False, "error": reason, "cancelled": is_cancelled(), "timeout": not is_cancelled(), "output": (stdout + stderr)[: self.config.max_output_chars], "duration_ms": round((time.perf_counter() - started) * 1000)}
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, self.config.command_timeout - elapsed))
                break
            except subprocess.TimeoutExpired:
                continue
        full_output = stdout + stderr
        truncated = len(full_output) > self.config.max_output_chars
        output = full_output[: self.config.max_output_chars]
        return {"ok": process.returncode == 0, "returncode": process.returncode, "output": output, "truncated": truncated, "duration_ms": round((time.perf_counter() - started) * 1000)}

    @staticmethod
    def _is_dangerous(command: str) -> bool:
        normalized = command.lower().replace("\\", "/")
        patterns = [
            r"(^|[;&|])\s*(rm|del|erase|rmdir|rd)\s+(-[a-z]+\s+)*([a-z]:/|/|~)",
            r"(^|[;&|])\s*(format|diskpart|shutdown|reboot)\b",
            r"(^|[;&|])\s*(sudo|runas)\b",
            r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f)",
            r"(^|[;&|])\s*(?:del|erase|rm|rmdir|rd)\b[^\n]*(?:\*|/[sq])",
            r"(curl|wget|invoke-webrequest)\b[^\n]*(\||;|>)[^\n]*",
        ]
        return any(re.search(pattern, normalized) for pattern in patterns)

    @classmethod
    def command_approval_family(cls, command: str) -> tuple[str, str] | None:
        """Return a narrow, workspace-safe category eligible for persistence.

        Persistent approval never covers shell chaining, redirection, deletion,
        elevation, or other dangerous command forms.  The remaining categories
        deliberately include both executable and subcommand where available,
        so allowing ``git add`` does not also allow ``git reset``.
        """
        normalized = command.strip().lower()
        if not normalized or cls._is_dangerous(normalized):
            return None
        if any(token in normalized for token in ("&", "|", ";", ">", "<", "\n", "`", "$", "%")):
            return None
        tokens = re.findall(r"[^\s\"']+", normalized)
        if not tokens:
            return None
        executable = tokens[0].removesuffix(".exe")
        blocked = {"del", "erase", "rm", "rmdir", "rd", "move", "copy", "xcopy", "robocopy", "cmd", "powershell", "pwsh", "python", "py", "node"}
        if executable in blocked:
            return None
        subcommand = tokens[1] if len(tokens) > 1 and not tokens[1].startswith(("-", "/")) else ""
        if executable in {"git", "npm", "pnpm", "yarn", "pip", "pip3", "cargo", "go", "dotnet", "mvn", "gradle", "gradlew"} and not subcommand:
            return None
        family = f"{executable}:{subcommand}" if subcommand else executable
        label = f"{executable} {subcommand}".strip()
        return family, label

    @staticmethod
    def _may_mutate_workspace(command: str) -> bool:
        """Use an allowlist for read-only shell commands in Approval mode.

        Shell syntax is too expressive to prove arbitrary input harmless.  A
        conservative read-only allowlist makes unrecognised commands request
        local approval before they can create build artifacts, install
        dependencies, redirect output, or otherwise change the workspace.
        """
        normalized = command.strip().lower()
        if not normalized or any(token in normalized for token in (">", "|", "&", ";", "\n")):
            return True
        first = normalized.split(maxsplit=1)[0]
        if first == "git":
            return not bool(re.match(r"^git\s+(status|diff|log|show)(\s|$)", normalized))
        return first not in {"dir", "type", "find", "findstr", "where", "fc"}

    @staticmethod
    def _is_standard_development_command(command: str) -> bool:
        """Recognise local build, test, and executable-run commands.

        These commands may produce ordinary build artefacts, which is an
        expected part of an agent completing a programming task. They remain
        subject to the dangerous-command check above. The only accepted shell
        pipeline is ``echo <literal input> | local-program`` for feeding a
        generated executable test data; arbitrary pipelines and output
        redirection still require approval.
        """
        normalized = command.strip().lower()
        # Semicolons inside a quoted Python one-liner are program syntax, not
        # shell chaining.  This form is routinely used for small probes.
        if re.match(r"^(?:python|python3|py)(?:\.exe)?\s+-c\s+['\"].*['\"]\s*$", normalized, flags=re.DOTALL):
            return True
        if not normalized or any(token in normalized for token in (";", ">", "\n")):
            return False
        parts = [part.strip() for part in re.split(r"\s*&&\s*", normalized) if part.strip()]
        if not parts:
            return False
        return all(
            ToolRegistry._is_standard_development_step(part)
            or ToolRegistry._is_standard_development_input_pipeline(part)
            for part in parts
        )

    @staticmethod
    def _is_standard_development_input_pipeline(command: str) -> bool:
        """Accept literal ``echo`` input piped into one local executable only."""
        if command.count("|") != 1:
            return False
        producer, consumer = (part.strip() for part in command.split("|", 1))
        if not re.match(r"^echo\s+[^&;|><`$%]+$", producer):
            return False
        if any(token in consumer for token in ("&", ";", "|", ">", "<", "`", "$", "%")):
            return False
        executable = consumer.split(maxsplit=1)[0].replace("\\", "/")
        return executable.endswith((".exe", ".out")) or executable.startswith("./")

    @staticmethod
    def _is_standard_development_step(command: str) -> bool:
        # Input redirection is useful for local verification, e.g.
        # ``solution.exe < sample.in``.  Output redirection is rejected above.
        if "|" in command:
            return False
        step = command.split("<", 1)[0].strip()
        if not step:
            return False
        first = step.split(maxsplit=1)[0].replace("\\", "/")
        executable = first.removeprefix("./")
        if executable.endswith((".exe", ".out")) or first.startswith("./"):
            return True
        if executable in {"g++", "gcc", "clang", "clang++", "cl", "javac"}:
            return True
        if executable in {"make", "nmake", "msbuild", "pytest", "ctest", "gradle", "gradlew"}:
            return True
        if executable == "cmake":
            return bool(re.match(r"^cmake\s+--build\b", step))
        if executable in {"cargo", "go", "dotnet", "mvn", "npm", "pnpm", "yarn"}:
            return bool(re.match(r"^(cargo\s+(build|check|test|run)|go\s+(build|test|run)|dotnet\s+(build|test|run)|mvn\s+(test|package)|(?:npm|pnpm|yarn)\s+(test|run\s+(build|test)))\b", step))
        if executable in {"python", "python3", "py"}:
            return bool(re.match(r"^(python|python3|py)(?:\.exe)?\s+(?:-c\s+|-m\s+(?:pytest|unittest)\b|[^\s]+\.py\b)", step))
        if executable == "java":
            return True
        return False

    @staticmethod
    def _is_standard_readonly_command(command: str) -> bool:
        """Recognise bounded workspace inspection commands for the verifier.

        Verifiers sometimes need to check the compiler or inspect a generated
        input file before choosing a test.  These commands are read-only and
        deliberately reject shell composition, redirection, and parent/path
        traversal so the verifier cannot turn the exception into a write path.
        """
        normalized = command.strip().lower()
        if not normalized or any(token in normalized for token in (";", "|", "&", ">", "<", "`", "$", "%", "\n")):
            return False
        tokens = re.findall(r"[^\s\"']+", normalized)
        if not tokens:
            return False
        executable = tokens[0].replace("\\", "/").removesuffix(".exe")
        if executable in {"dir", "ls"}:
            # Permit common listing switches and relative workspace paths.
            return all(
                token in {"/a", "/b", "/ad", "/o:n", "-a", "-l", "--all"}
                or (not token.startswith(("/", "-")) and ":" not in token and ".." not in token)
                for token in tokens[1:]
            )
        if executable in {"type", "cat", "head", "tail"}:
            return len(tokens) >= 2 and all(
                not token.startswith(("/", "-")) and ":" not in token and ".." not in token
                for token in tokens[1:]
            )
        if executable == "where":
            return len(tokens) >= 2 and all(re.fullmatch(r"[a-z0-9_.+-]+", token) for token in tokens[1:])
        if executable == "echo":
            return True
        if executable == "git":
            return len(tokens) >= 2 and tokens[1] in {"status", "diff", "log", "show"}
        return False
