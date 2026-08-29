"""A terminal-first local Coding Agent interface with no third-party packages."""

import argparse
import os
from pathlib import Path
import shlex
import sys
import textwrap
import time
from typing import Any

from .config import Config
from .events import AgentEvent
from .models import ModelManager
from .service import AgentService, TaskRecord


HELP = """Commands:
  /help                         Show this help
  /config                       Show runtime configuration
  /config <name> <value>        Set model, workspace, api-timeout, max-turns,
                                command-timeout, approval-timeout, request-gap, or demo
  /model [name]                 Show or switch the model
  /models                       List configured models
  /skills                       List available skills
  /skill <name|auto|reload>     Select a skill, return to automatic selection, or reload
  /memory                       List project memory
  /memory add <content>         Add durable project memory
  /memory search <query>        Search relevant project memory
  /memory delete <id>           Delete a memory item
  /compact                      Compact the most recent task context
  /continue <instruction>       Continue the most recent completed task
  /history                      Show recent tasks
  /open <task-id-prefix>        Replay a stored task event stream
  /clear                        Clear the terminal
  /quit                         Exit the TUI

Any other input is sent to the coding agent as a task. Ctrl+C cancels the active task.
"""


def parse_command(line: str) -> tuple[str, list[str]]:
    """Parse slash commands without treating ordinary task text as a command."""
    text = line.strip()
    if not text.startswith("/"):
        return "task", [text]
    parts = shlex.split(text[1:])
    return (parts[0].lower(), parts[1:]) if parts else ("", [])


def event_summary(event: AgentEvent) -> str:
    """Turn a public event into a compact terminal transcript line."""
    data = event.data
    if event.type == "task_started":
        return "task started"
    if event.type == "model_thinking":
        return f"thinking (turn {data.get('turn', '?')})"
    if event.type == "skills_loaded":
        return f"skills loaded: {', '.join(data.get('skills', []))}"
    if event.type == "memory_retrieved":
        return f"retrieved {len(data.get('memory_ids', []))} project memories"
    if event.type == "memory_saved":
        return f"saved {len(data.get('memory_ids', []))} project memories"
    if event.type == "context_compacted":
        return f"context compacted: {data.get('before_tokens', '?')} -> {data.get('after_tokens', '?')} tokens"
    if event.type == "task_resumed":
        return "task context resumed"
    if event.type == "model_retrying":
        return f"retrying model request in {data.get('delay_ms', '?')} ms"
    if event.type == "model_rate_limited":
        return f"waiting for model rate limit ({data.get('waited_ms', '?')} ms)"
    if event.type == "tool_started":
        arguments = data.get("arguments") or {}
        return f"tool {data.get('tool', '<unknown>')} {arguments}"
    if event.type == "tool_finished":
        tool = str(data.get("tool", "<unknown>"))
        result = data.get("result") or {}
        if not isinstance(result, dict):
            return f"tool {tool} returned an invalid result"
        if not result.get("ok"):
            return f"tool {tool} failed: {result.get('error', 'unknown error')}"
        if tool == "run_command":
            return f"tool run_command finished (exit {result.get('returncode', '?')}, {result.get('duration_ms', '?')} ms)"
        if tool == "write_file":
            state = "changed" if result.get("changed") else "unchanged"
            return f"tool write_file {result.get('path', '?')} ({state})"
        if tool == "read_file":
            return f"tool read_file {result.get('path', '?')}"
        if tool == "list_files":
            return f"tool list_files ({len(result.get('files', []))} files)"
        return f"tool {tool} finished"
    if event.type == "command_approval_requested":
        return f"approval required: {data.get('command', '')}"
    if event.type == "command_approval_resolved":
        return f"approval {data.get('decision', 'resolved')}: {data.get('command', '')}"
    if event.type == "assistant_message":
        return "assistant finished"
    if event.type == "task_finished":
        return "task completed"
    if event.type == "task_cancelling":
        return "cancelling task"
    if event.type == "task_cancelled":
        return "task cancelled"
    if event.type == "task_error":
        return f"task error: {data.get('error', 'unknown error')}"
    return event.type.replace("_", " ")


def apply_config_change(config: Config, demo: bool, name: str, value: str) -> tuple[Config, bool]:
    """Apply one TUI configuration command with a small, explicit option surface."""
    key = name.lower().replace("_", "-")
    if key == "demo":
        if value.lower() not in {"on", "off"}:
            raise ValueError("demo must be 'on' or 'off'")
        return config, value.lower() == "on"
    if key == "workspace":
        workspace = Path(value).expanduser().resolve()
        # Re-evaluate the default history location relative to the new workspace.
        refreshed = Config.from_env(str(workspace))
        return refreshed.with_overrides(
            model=config.model,
            model_timeout=config.model_timeout,
            max_turns=config.max_turns,
            command_timeout=config.command_timeout,
            command_approval_timeout=config.command_approval_timeout,
            model_min_request_interval_ms=config.model_min_request_interval_ms,
        ), demo
    integer_fields = {
        "api-timeout": ("model_timeout", 1),
        "max-turns": ("max_turns", 1),
        "command-timeout": ("command_timeout", 1),
        "approval-timeout": ("command_approval_timeout", 1),
        "request-gap": ("model_min_request_interval_ms", 0),
    }
    if key == "model":
        if not value.strip():
            raise ValueError("model must not be empty")
        return config.with_overrides(model=value.strip()), demo
    if key not in integer_fields:
        raise ValueError("unknown setting")
    field, minimum = integer_fields[key]
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{key} must be at least {minimum}")
    return config.with_overrides(**{field: parsed}), demo


class TerminalApp:
    """Line-oriented TUI using AgentService as the only task backend."""

    def __init__(self, config: Config, demo: bool = False):
        self.config = config
        self.demo = demo
        self.service = AgentService(config)
        self.models = ModelManager(config)
        self._selected_skills: tuple[str, ...] = ()
        self._last_task_id: str | None = None
        self._use_colour = sys.stdout.isatty() and not os.getenv("NO_COLOR")

    def run(self) -> None:
        self._banner()
        while True:
            try:
                line = input(self._prompt())
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print("\nUse /quit to exit.")
                continue
            if not line.strip():
                continue
            try:
                if not self._dispatch(line):
                    return
            except (ValueError, OSError) as exc:
                self._write(f"error: {exc}", "red")

    def _dispatch(self, line: str) -> bool:
        command, arguments = parse_command(line)
        if command == "task":
            self._run_task(arguments[0])
            return True
        if command in {"quit", "exit"}:
            return False
        if command == "help":
            print(HELP)
            return True
        if command == "config":
            self._config(arguments)
            return True
        if command == "model":
            self._model(arguments)
            return True
        if command == "models":
            self._models()
            return True
        if command == "skills":
            self._skills()
            return True
        if command == "skill":
            self._skill(arguments)
            return True
        if command == "memory":
            self._memory(arguments)
            return True
        if command == "compact":
            self._compact(arguments)
            return True
        if command == "continue":
            self._continue(arguments)
            return True
        if command == "history":
            self._history()
            return True
        if command == "open":
            self._open(arguments)
            return True
        if command == "clear":
            self._clear()
            self._banner()
            return True
        self._write(f"unknown command: /{command}. Type /help.", "red")
        return True

    def _run_task(self, task: str, resume_from: str | None = None) -> None:
        record = self.service.create_task(task, demo=self.demo, resume_from=resume_from)
        self._last_task_id = record.id
        self._write(f"\n[{record.id[:8]}] {task}", "bold")
        self._stream(record)

    def _stream(self, record: TaskRecord) -> None:
        sequence = 0
        interrupted = False
        while True:
            try:
                events = self.service.events(record.id, sequence)
                for event in events:
                    sequence = event.sequence
                    self._write(f"  {event_summary(event)}")
                    if event.type == "command_approval_requested":
                        self._resolve_approval(record, event)
                if record.status in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.08)
            except KeyboardInterrupt:
                if not interrupted:
                    interrupted = self.service.cancel_task(record.id)
                    self._write("  cancellation requested", "yellow")
                else:
                    self._write("  waiting for task cleanup", "yellow")
        for event in self.service.events(record.id, sequence):
            self._write(f"  {event_summary(event)}")
        result = record.result or record.error or "No final result."
        self._write("\nfinal", "bold")
        self._write_block(result)

    def _resolve_approval(self, record: TaskRecord, event: AgentEvent) -> None:
        approval_id = event.data.get("approval_id")
        command = event.data.get("command")
        if not isinstance(approval_id, str) or not isinstance(command, str):
            return
        while True:
            try:
                decision = input("  approve this command? [y/N] ").strip().lower()
            except KeyboardInterrupt:
                decision = "n"
                print()
            if decision in {"", "n", "no"}:
                self.service.approve_command(record.id, approval_id, False)
                return
            if decision in {"y", "yes"}:
                self.service.approve_command(record.id, approval_id, True)
                return
            self._write("  enter y or n", "yellow")

    def _config(self, arguments: list[str]) -> None:
        if not arguments:
            self._write("configuration", "bold")
            print(f"  workspace        {self.config.workspace}")
            print(f"  model            {self.config.model}")
            print(f"  api-timeout      {self.config.model_timeout}s")
            print(f"  max-turns        {self.config.max_turns}")
            print(f"  command-timeout  {self.config.command_timeout}s")
            print(f"  approval-timeout {self.config.command_approval_timeout}s")
            print(f"  request-gap      {self.config.model_min_request_interval_ms}ms")
            print(f"  demo             {'on' if self.demo else 'off'}")
            return
        if len(arguments) < 2:
            raise ValueError("usage: /config <name> <value>")
        if arguments[0].lower().replace("_", "-") == "model":
            self._model(arguments[1:])
            return
        self.config, self.demo = apply_config_change(self.config, self.demo, arguments[0], " ".join(arguments[1:]))
        self._rebuild_service()
        self._write(f"updated {arguments[0]}", "green")

    def _model(self, arguments: list[str]) -> None:
        if not arguments:
            current = self.models.current()
            self._write("current model", "bold")
            print(f"  provider  {current.provider}\n  model     {current.name}\n  context   {current.context_window} tokens")
            return
        self.config = self.models.switch(" ".join(arguments))
        self.service.update_config(self.config)
        self.models = ModelManager(self.config)
        self._write(f"model switched to {self.config.model}", "green")

    def _models(self) -> None:
        self._write("available models", "bold")
        for model in self.models.available():
            marker = "*" if model.name == self.config.model else " "
            print(f" {marker} {model.name}  ({model.provider}, {model.context_window} tokens)")

    def _skills(self) -> None:
        skills = self.service.skill_manager.metadata()
        if not skills:
            self._write("no skills found")
            return
        self._write("available skills", "bold")
        for skill in skills:
            marker = "*" if skill.name in self._selected_skills else " "
            print(f" {marker} {skill.name:<14} {skill.description}")

    def _skill(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            raise ValueError("usage: /skill <name|auto|reload>")
        name = arguments[0]
        if name == "reload":
            self.service.reload_skills()
            self._write("skills reloaded", "green")
            return
        if name == "auto":
            self._selected_skills = ()
            self.service.set_selected_skills(())
            self._write("automatic skill selection enabled", "green")
            return
        self.service.set_selected_skills((name,))
        self._selected_skills = (name,)
        self._write(f"skill selected: {name}", "green")

    def _rebuild_service(self) -> None:
        self.service = AgentService(self.config)
        self.models = ModelManager(self.config)
        self.service.set_selected_skills(self._selected_skills)

    def _memory(self, arguments: list[str]) -> None:
        if not arguments:
            items = self.service.list_memories()
            self._show_memories(items)
            return
        action = arguments[0].lower()
        if action == "add" and len(arguments) > 1:
            item = self.service.add_memory(" ".join(arguments[1:]))
            self._write(f"memory saved: {item.id}", "green")
            return
        if action == "search" and len(arguments) > 1:
            self._show_memories(self.service.search_memories(" ".join(arguments[1:])))
            return
        if action == "delete" and len(arguments) == 2:
            try:
                item_id = int(arguments[1])
            except ValueError as exc:
                raise ValueError("memory id must be an integer") from exc
            if not self.service.delete_memory(item_id):
                raise ValueError("memory does not exist")
            self._write(f"memory deleted: {item_id}", "green")
            return
        raise ValueError("usage: /memory [add <content>|search <query>|delete <id>]")

    def _show_memories(self, items: list[Any]) -> None:
        if not items:
            self._write("no project memory")
            return
        self._write("project memory", "bold")
        for item in items:
            print(f"  {item.id:<4} {textwrap.shorten(item.content, width=76, placeholder='...')}")

    def _compact(self, arguments: list[str]) -> None:
        if arguments:
            raise ValueError("usage: /compact")
        if not self._last_task_id:
            raise ValueError("no task context is available")
        result = self.service.compact_task(self._last_task_id)
        if not result:
            raise ValueError("task context is unavailable")
        if result.compacted:
            self._write(f"context compacted: {result.before_tokens} -> {result.after_tokens} tokens", "green")
        else:
            self._write("context is already compact", "green")

    def _continue(self, arguments: list[str]) -> None:
        if not arguments:
            raise ValueError("usage: /continue <instruction>")
        if not self._last_task_id:
            raise ValueError("no completed task context is available")
        self._run_task(" ".join(arguments), resume_from=self._last_task_id)

    def _history(self) -> None:
        tasks = self.service.list_tasks(limit=12)
        if not tasks:
            self._write("no task history")
            return
        self._write("history", "bold")
        for item in tasks:
            text = textwrap.shorten(str(item["task"]), width=60, placeholder="...")
            print(f"  {item['id'][:8]}  {item['status']:<10}  {text}")

    def _open(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            raise ValueError("usage: /open <task-id-prefix>")
        prefix = arguments[0]
        matches = [item for item in self.service.list_tasks() if item["id"].startswith(prefix)]
        if len(matches) != 1:
            raise ValueError("task prefix must match exactly one task")
        record = self.service.get_task(matches[0]["id"])
        self._last_task_id = record.id
        self._write(f"\n[{record.id[:8]}] {record.task}", "bold")
        for event in self.service.events(record.id):
            self._write(f"  {event_summary(event)}")
        if record.result or record.error:
            self._write("\nfinal", "bold")
            self._write_block(record.result or record.error or "")

    def _banner(self) -> None:
        self._write("LOCAL CODEX", "bold")
        print(f"workspace {self.config.workspace}  model {self.config.model}  {'demo' if self.demo else 'live'}")
        print("Type a task, or /help for commands.\n")

    def _prompt(self) -> str:
        return self._colour("local-codex > ", "blue")

    def _write_block(self, text: str) -> None:
        width = max(40, min(100, os.get_terminal_size().columns if sys.stdout.isatty() else 88))
        for line in str(text).splitlines() or [""]:
            for wrapped in textwrap.wrap(line, width=width, replace_whitespace=False) or [""]:
                print(f"  {wrapped}")

    def _clear(self) -> None:
        print("\033[2J\033[H" if self._use_colour else "\n" * 3, end="")

    def _write(self, text: str, colour: str | None = None) -> None:
        print(self._colour(text, colour))

    def _colour(self, text: str, colour: str | None) -> str:
        if not self._use_colour or not colour:
            return text
        codes = {"bold": "1", "blue": "36", "green": "32", "yellow": "33", "red": "31"}
        code = codes.get(colour)
        return f"\033[{code}m{text}\033[0m" if code else text


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal-first local Coding Agent")
    parser.add_argument("--workspace")
    parser.add_argument("--demo", action="store_true", help="Start in deterministic offline demo mode")
    args = parser.parse_args()
    TerminalApp(Config.from_env(args.workspace), demo=args.demo).run()


if __name__ == "__main__":
    main()
