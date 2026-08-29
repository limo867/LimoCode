from dataclasses import dataclass
import re
import subprocess
import time
from typing import Any, Callable

from .config import Config
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

    def __init__(self, config: Config):
        self.workspace = Workspace(config.workspace)
        self.config = config
        self.tools = {tool.name: tool for tool in self._build_tools()}

    def _build_tools(self) -> list[Tool]:
        string_path = {"type": "string", "description": "Path relative to workspace"}
        return [
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

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    def execute(self, name: str, arguments: dict[str, Any], is_cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
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
                return tool.handler(**arguments, is_cancelled=is_cancelled or (lambda: False))
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
            return {"ok": False, "error": f"file exceeds {self.config.max_file_chars} character limit"}
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "file is not valid UTF-8 text"}
        return {"ok": True, "path": path, "content": content[: self.config.max_file_chars], "truncated": len(content) > self.config.max_file_chars}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        if not isinstance(path, str) or not path.strip():
            return {"ok": False, "error": "path must be a non-empty string"}
        if not isinstance(content, str):
            return {"ok": False, "error": "content must be a string"}
        if len(content) > self.config.max_file_chars:
            return {"ok": False, "error": f"content exceeds {self.config.max_file_chars} character limit"}
        target = self.workspace.resolve(path)
        if target.exists() and not target.is_file():
            return {"ok": False, "error": "path is not a regular file"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}

    def run_command(self, command: str, is_cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            return {"ok": False, "error": "command must be a non-empty string"}
        if self._is_dangerous(command):
            return {"ok": False, "error": "command rejected by safety policy"}
        started = time.perf_counter()
        is_cancelled = is_cancelled or (lambda: False)
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
            r"(curl|wget|invoke-webrequest)\b[^\n]*(\||;|>)[^\n]*",
        ]
        return any(re.search(pattern, normalized) for pattern in patterns)
