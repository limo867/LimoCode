"""A terminal-first local Coding Agent interface."""

import argparse
from dataclasses import dataclass, field
import html
import os
from pathlib import Path
import re
import shlex
import sys
import textwrap
import threading
import time
from typing import Any
import unicodedata

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.history import DummyHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import HSplit, Layout
    from prompt_toolkit.layout.containers import ConditionalContainer, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.scrollable_pane import ScrollablePane
    from prompt_toolkit.mouse_events import MouseButton, MouseEventType
    from prompt_toolkit.output import ColorDepth
    from prompt_toolkit.styles import Style
    from prompt_toolkit.utils import get_cwidth
    from prompt_toolkit.widgets import TextArea
    _PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:  # pragma: no cover - optional fallback for minimal installs
    _PROMPT_TOOLKIT_AVAILABLE = False
    ColorDepth = None
    get_cwidth = None

from .config import Config
from .events import AgentEvent
from .models import ModelManager
from .service import AgentService, TaskRecord
from .trust import WorkspaceTrustStore


if _PROMPT_TOOLKIT_AVAILABLE:
    class CodexScrollablePane(ScrollablePane):
        """ScrollablePane with explicit wheel and draggable-scrollbar support."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._codex_virtual_height = 0
            self._codex_view_height = 0
            self._codex_scrollbar_x = -1
            self._codex_top = 0
            self._scrollbar_dragging = False
            self.follow_output = True

        def _scroll_by(self, amount: int) -> None:
            maximum = max(0, self._codex_virtual_height - self._codex_view_height)
            self.vertical_scroll = max(0, min(maximum, self.vertical_scroll + amount))
            self.follow_output = self.vertical_scroll >= maximum
            # Mouse handlers do not necessarily trigger an application redraw
            # themselves. Request one immediately so trackpad/wheel scrolling
            # feels direct rather than waiting for the polling interval.
            from prompt_toolkit.application import get_app
            get_app().invalidate()

        def scroll_to_bottom(self) -> None:
            self.follow_output = True
            maximum = max(0, self._codex_virtual_height - self._codex_view_height)
            self.vertical_scroll = maximum

        def _scroll_to_pointer(self, row: int) -> None:
            maximum = max(0, self._codex_virtual_height - self._codex_view_height)
            if not maximum or self._codex_view_height <= 1:
                return
            relative = max(0, min(self._codex_view_height - 1, row - self._codex_top))
            self.vertical_scroll = round(maximum * relative / (self._codex_view_height - 1))
            self.follow_output = self.vertical_scroll >= maximum
            from prompt_toolkit.application import get_app
            get_app().invalidate()

        def _handle_global_scrollbar_drag(self, event: Any) -> bool:
            """Keep a scrollbar drag owned by the transcript outside its row range.

            The root layout reroutes pointer events while a drag is active. This
            matters when the pointer leaves the transcript and enters the
            composer: without it Prompt Toolkit gives the editor the mouse move
            and can move its cursor instead of finishing the scroll gesture.
            """
            if not self._scrollbar_dragging:
                return False
            if event.event_type in {MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_MOVE}:
                self._scroll_to_pointer(event.position.y)
                return True
            if event.event_type == MouseEventType.MOUSE_UP:
                self._scrollbar_dragging = False
                return True
            return False

        def write_to_screen(self, screen, mouse_handlers, write_position, parent_style, erase_bg, z_index):
            virtual_width = write_position.width - (1 if self.show_scrollbar() else 0)
            self._codex_virtual_height = max(
                self.content.preferred_height(virtual_width, self.max_available_height).preferred,
                write_position.height,
            )
            self._codex_view_height = write_position.height
            self._codex_scrollbar_x = write_position.xpos + write_position.width - 1
            self._codex_top = write_position.ypos
            if self.follow_output:
                self.vertical_scroll = max(0, self._codex_virtual_height - self._codex_view_height)
            super().write_to_screen(screen, mouse_handlers, write_position, parent_style, erase_bg, z_index)

            # ScrollablePane draws a scrollbar but leaves its mouse area inert.
            # Wrap every visible mouse cell so wheel events always reach the pane;
            # the right-most column additionally supports click/drag jumps.
            for row in range(write_position.ypos, write_position.ypos + write_position.height):
                handlers = mouse_handlers.mouse_handlers[row]
                for column in range(write_position.xpos, write_position.xpos + write_position.width):
                    previous = handlers.get(column)

                    def handle(event, previous=previous, column=column):
                        if event.event_type == MouseEventType.SCROLL_UP:
                            self._scroll_by(-3)
                            return None
                        if event.event_type == MouseEventType.SCROLL_DOWN:
                            self._scroll_by(3)
                            return None
                        # Keep ownership once a scrollbar drag begins. Mouse
                        # move events can arrive one column away from the
                        # track; without this, Prompt Toolkit may pass the
                        # event through to the composer and move its cursor.
                        if event.event_type == MouseEventType.MOUSE_DOWN:
                            self._scrollbar_dragging = column == self._codex_scrollbar_x
                        if self._scrollbar_dragging and event.event_type in {
                            MouseEventType.MOUSE_DOWN,
                            MouseEventType.MOUSE_MOVE,
                        }:
                            self._scroll_to_pointer(event.position.y)
                            return None
                        if self._scrollbar_dragging and event.event_type == MouseEventType.MOUSE_UP:
                            self._scrollbar_dragging = False
                            return None
                        return previous(event) if previous else NotImplemented

                    handlers[column] = handle


    class CodexMouseRoutingHSplit(HSplit):
        """Route pointer-wheel events from the whole UI to the transcript.

        Prompt Toolkit dispatches a coordinate-bearing wheel event to the
        control under the pointer.  The composer, slash picker, and footer sit
        outside the transcript pane, so their default handlers would otherwise
        consume the gesture or leave it unhandled.  Codex treats the terminal
        transcript as the one scroll target; preserve that behavior here while
        leaving ordinary clicks and scrollbar drag events with their original
        controls.
        """

        def __init__(self, *args, transcript: CodexScrollablePane, **kwargs):
            super().__init__(*args, **kwargs)
            self._codex_transcript = transcript

        def write_to_screen(self, screen, mouse_handlers, write_position, parent_style, erase_bg, z_index):
            super().write_to_screen(screen, mouse_handlers, write_position, parent_style, erase_bg, z_index)

            # Child controls have registered their pointer handlers at this
            # point. Wrap the complete root surface, including empty rows, so
            # a wheel gesture always scrolls transcript history rather than a
            # focused input control.
            for row in range(write_position.ypos, write_position.ypos + write_position.height):
                handlers = mouse_handlers.mouse_handlers[row]
                for column in range(write_position.xpos, write_position.xpos + write_position.width):
                    previous = handlers.get(column)

                    def handle(event, previous=previous):
                        if event.event_type == MouseEventType.SCROLL_UP:
                            self._codex_transcript._scroll_by(-3)
                            return None
                        if event.event_type == MouseEventType.SCROLL_DOWN:
                            self._codex_transcript._scroll_by(3)
                            return None
                        if self._codex_transcript._handle_global_scrollbar_drag(event):
                            return None
                        return previous(event) if previous else NotImplemented

                    handlers[column] = handle
else:  # pragma: no cover - keeps module names defined for minimal installs
    CodexScrollablePane = None
    CodexMouseRoutingHSplit = None


HELP = """Commands:
  /help                         Show this help
  /config                       Show runtime configuration
  /config <name> <value>        Set model, workspace, timeouts, request-gap,
                                permission-mode, or demo
  /mode [approval|auto]         Show or switch file-change permission mode
  /changes [task-id-prefix]     List tracked Agent file changes and Diff status
  /undo <changeset-id-prefix>   Undo one applied Agent ChangeSet
  /model [name]                 Show or switch the model
  /models                       List configured models
  /skills                       List available skills
  /skill <name|auto|reload>     Select a skill, return to automatic selection, or reload
  /memory                       List project memory
  /memory status                Show durable and task-context memory status
  /memory add <content>         Add durable project memory
  /memory search <query>        Search relevant project memory
  /memory delete <id>           Delete a memory item
  /compact                      Compact the active conversation context
  /new                          Start a new conversation
  /continue <instruction>       Send a follow-up in the active resumable conversation
  /history                      List saved conversations
  /resume [conversation-id]     Choose or activate a saved conversation
  /open <task-id-prefix>        Inspect one stored task without changing the conversation
  /details [task-id-prefix]     Show task details and bounded tool output
  /inspect [task-id-prefix]     Inspect task operations and their outputs
  /activity [task-id-prefix]    Expand or collapse task activity
  /prompt [task-id-prefix]      Show the full user prompt for a task
  /clear                        Clear the terminal
  /quit                         Exit the TUI

After resuming a conversation, ordinary input continues it. Use /new before ordinary
input to start without prior task context. Ctrl+C cancels the active task.
"""


MAX_VISIBLE_OPERATIONS = 8
MAX_COMMAND_MENU_ROWS = 8
MAX_DETAIL_LINES = 18
MAX_DETAIL_CHARS = 2400
LONG_PROMPT_CHARS = 180
SCREEN_HISTORY_LIMIT = 300
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})


PROMPT_TOOLKIT_STYLE_RULES = {
    # Keep Codex's black terminal canvas, but make information hierarchy
    # visible even in hosts that quantize true color to a 256-color palette.
    "": "bg:#000000 #e5e7eb",
    "header": "bold #f8fafc",
    "workspace": "#94a3b8",
    "workspace.path": "#b8c7d9",
    "trust": "bold #f6c46e",
    "user": "#eaf4ff",
    "user.marker": "bold #7dd3fc",
    "assistant": "#e5e7eb",
    "assistant.marker": "bold #8ab4f8",
    "assistant.bullet": "#79a9d6",
    "heading": "bold #f8fafc",
    "code": "#c7d8f1",
    "code.block": "#bfd0e6",
    "table.border": "#526174",
    "table.header": "bold #dbeafe",
    "table.cell": "#e2e8f0",
    "emphasis": "bold #f8fafc",
    "link": "underline #7dd3fc",
    "activity": "#8d9bae",
    "activity.label": "bold #d3dbe7",
    "success": "bold #82d8a0",
    "warning": "bold #f4c66b",
    "error": "bold #f48b86",
    "approval": "bold #c9a8ff",
    "separator": "#4c5b6e",
    "completion": "#9cabc0",
    "diff.header": "#9aa9bd",
    # GitHub-style line highlighting adapted to the black terminal canvas:
    # additions/deletions use saturated foreground colours and bold markers.
    "diff.add": "bold #b9efc5",
    "diff.add.number": "bold #79e39a",
    "diff.remove": "bold #ffc0ba",
    "diff.remove.number": "bold #ff8580",
    "diff.context": "#b1bdcb",
    "diff.context.number": "#718096",
    "thinking": "#8ab4f8 italic",
    "command-working": "#f4c66b italic",
    "mode": "#7dd3fc",
    "input": "#f8fafc",
    "input.prompt": "bold #7dd3fc",
    "footer": "#7e8ca0",
    "command.info": "#a5b4c6",
    # Codex command popups are plain terminal rows: command names are bold,
    # descriptions are quiet, and only the active row is cyan and bold.
    "command-menu": "#e5e7eb",
    "command-menu.selected": "bold #7dd3fc",
    "command-menu.name": "bold #e5e7eb",
    "command-menu.choice": "#d6deea",
    "command-menu.meta": "#8d9bae",
    "command-menu.hint": "#7e8ca0 italic",
    "header.border": "#66758a",
    "header.title": "bold #f8fafc",
    "header.label": "#94a3b8",
}


COLOR_MODES = frozenset({"auto", "always", "never"})


@dataclass(frozen=True)
class CommandMenuOption:
    """One keyboard-selectable choice shown below the Prompt Toolkit composer."""

    kind: str
    value: str
    label: str
    description: str


def _context_update_from_event(event: AgentEvent) -> str | None:
    """Summarize context events without exposing memory or transcript content."""
    data = event.data
    if event.type == "memory_retrieved":
        memory_ids = data.get("memory_ids")
        count = len(memory_ids) if isinstance(memory_ids, list) else 0
        storage_error = data.get("storage_error")
        if not count:
            return f"Project memory unavailable: {_short_value(storage_error, 140)}" if storage_error else None
        noun = "memory" if count == 1 else "memories"
        return f"Loaded {count} project {noun}"
    if event.type == "memory_saved":
        memory_ids = data.get("memory_ids")
        count = len(memory_ids) if isinstance(memory_ids, list) else 0
        noun = "memory" if count == 1 else "memories"
        return f"Saved {count} durable project {noun}"
    if event.type == "context_compacted":
        before = data.get("before_tokens")
        after = data.get("after_tokens")
        if isinstance(before, int) and isinstance(after, int):
            return f"Compacted conversation context: {before:,} -> {after:,} tokens"
        return "Compacted conversation context"
    if event.type == "task_resumed":
        return "Resumed saved conversation context"
    return None


SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("model", "Choose the model for tasks, or switch the active model."),
    ("models", "List configured models."),
    ("config", "View or update local agent settings."),
    ("mode", "Show or switch file-change permission mode."),
    ("changes", "List tracked file changes and their status."),
    ("undo", "Undo an applied file changeset."),
    ("skills", "List skills available in this workspace."),
    ("skill", "Choose a skill or return to automatic selection."),
    ("memory", "View, manage, or inspect project memory."),
    ("compact", "Compact the active conversation context."),
    ("new", "Start a new conversation and clear the active context."),
    ("continue", "Send a follow-up in the active resumable conversation."),
    ("history", "List saved conversations."),
    ("resume", "Choose or activate a saved conversation."),
    ("open", "Inspect one stored task without changing the conversation."),
    ("details", "Show task details and bounded tool output."),
    ("inspect", "Inspect task operations and their outputs."),
    ("activity", "Expand or collapse task activity."),
    ("prompt", "Show the full prompt for a task."),
    ("help", "Show all available commands."),
    ("clear", "Clear the local transcript without deleting saved data."),
    ("quit", "Exit LimoCode."),
)


MODEL_DESCRIPTIONS = {
    "sol": "Frontier coding model for complex agent work.",
    "terra": "Balanced model for everyday coding work.",
    "luna": "Fast model for lower-latency work.",
    "gpt-5.5": "Frontier model for complex coding and research.",
    "gpt-5.2": "Model optimized for longer-running professional work.",
}


@dataclass
class ToolOperation:
    """A presentation model for one tool call, independent from runtime events."""

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    turn: int | None = None

    @property
    def ok(self) -> bool | None:
        return self.result.get("ok") if isinstance(self.result, dict) else None


@dataclass
class TaskView:
    """TUI-only state derived from durable AgentEvent values."""

    task_id: str
    task: str
    started_at: float = field(default_factory=time.monotonic)
    operations: list[ToolOperation] = field(default_factory=list)
    turns: int = 0
    context_updates: list[str] = field(default_factory=list)
    subagents: list[dict[str, Any]] = field(default_factory=list)
    review_verdict: str | None = None
    review_summary: str = ""
    status: str = "running"
    error: str | None = None
    finished_at: float | None = None
    cancelling: bool = False
    _pending: list[ToolOperation] = field(default_factory=list, repr=False)

    def finish(self, status: str | None = None) -> None:
        """Freeze elapsed time once the task reaches a terminal status."""
        terminal_status = status or self.status
        if terminal_status not in TERMINAL_TASK_STATUSES:
            return
        self.status = terminal_status
        if self.finished_at is None:
            self.finished_at = time.monotonic()

    def apply(self, event: AgentEvent) -> ToolOperation | None:
        """Update UI state and return a completed operation when one is available."""
        if event.type == "model_thinking":
            turn = event.data.get("turn")
            if isinstance(turn, int):
                self.turns = max(self.turns, turn)
        elif event.type == "task_cancelling":
            self.cancelling = True
        elif event.type == "subagent_started":
            self.subagents.append({**dict(event.data), "status": "running"})
        elif event.type == "subagent_finished":
            identifier = event.data.get("id")
            existing = next((item for item in self.subagents if identifier and item.get("id") == identifier), None)
            if existing is None:
                self.subagents.append(dict(event.data))
            else:
                existing.update(dict(event.data))
        elif event.type == "review_completed":
            self.review_verdict = str(event.data.get("verdict") or "pass")
            self.review_summary = str(event.data.get("summary") or "")
        elif event.type == "review_rejected":
            self.review_verdict = "reject"
            self.review_summary = str(event.data.get("feedback") or "")
        elif event.type == "tool_started":
            arguments = event.data.get("arguments") or {}
            operation = ToolOperation(
                tool=str(event.data.get("tool", "<unknown>")),
                arguments=dict(arguments) if isinstance(arguments, dict) else {},
                turn=event.data.get("turn") if isinstance(event.data.get("turn"), int) else None,
            )
            self.operations.append(operation)
            self._pending.append(operation)
        elif event.type == "tool_finished":
            tool = str(event.data.get("tool", "<unknown>"))
            operation = next((item for item in self._pending if item.tool == tool and item.result is None), None)
            if operation is None:
                operation = ToolOperation(tool=tool)
                self.operations.append(operation)
            result = event.data.get("result")
            operation.result = dict(result) if isinstance(result, dict) else {"ok": False, "error": "invalid tool result"}
            if operation in self._pending:
                self._pending.remove(operation)
            return operation
        elif event.type == "task_finished":
            self.cancelling = False
            self.finish("completed")
        elif event.type == "task_cancelled":
            self.cancelling = False
            self.finish("cancelled")
        elif event.type == "task_error":
            self.cancelling = False
            self.error = str(event.data.get("error", "unknown error"))
            self.finish("failed")
        else:
            context_update = _context_update_from_event(event)
            if context_update:
                self.context_updates.append(context_update)
        return None

    @property
    def duration_seconds(self) -> float:
        # Records restored from history, and a narrow service/event race, can
        # expose a terminal status before its terminal event reaches the UI.
        # Never let a completed conversation continue counting in either case.
        self.finish()
        return max(0.0, (self.finished_at or time.monotonic()) - self.started_at)


@dataclass
class ContextCompactionView:
    """Transient state for an active-conversation compaction request."""

    conversation_id: str
    started_at: float = field(default_factory=time.monotonic)
    completed: threading.Event = field(default_factory=threading.Event, repr=False)
    result: Any | None = None
    error: str | None = None
    finished_at: float | None = None
    published: bool = False

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.finished_at or time.monotonic()) - self.started_at)


def format_duration(seconds: float) -> str:
    """Format task duration in the compact style used by the transcript."""
    total = max(0.0, seconds)
    if total < 60:
        return f"{total:.1f}s"
    minutes, remaining = divmod(int(round(total)), 60)
    if minutes < 60:
        return f"{minutes}m {remaining}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def task_summary(task: str, width: int = 54) -> str:
    """Make a one-line task title without echoing a complete user prompt."""
    normalized = " ".join(task.split())
    file_match = re.search(r"(?<!\w)([\w./\\-]+\.[A-Za-z0-9]{1,12})(?!\w)", normalized)
    target = file_match.group(1) if file_match else ""
    lower = normalized.lower()
    if target and ("create" in lower or "new" in lower or "新建" in normalized or "创建" in normalized):
        return f"Create {target}"
    if target and ("fix" in lower or "修复" in normalized):
        return f"Fix {target}"
    if target and ("test" in lower or "测试" in normalized):
        return f"Test {target}"
    return textwrap.shorten(normalized, width=width, placeholder="...") or "Untitled task"


def _short_value(value: Any, width: int = 72) -> str:
    return textwrap.shorten(" ".join(str(value).split()), width=width, placeholder="...")


def operation_summary(operation: ToolOperation) -> str:
    """Render the useful result of a tool call without exposing its bulky payload."""
    result = operation.result or {}
    path = str(result.get("path") or operation.arguments.get("path") or "")
    if operation.tool == "write_file":
        if not result.get("ok"):
            return f"Could not write {path or 'file'}"
        if not result.get("changed"):
            return f"Checked {path or 'file'} (unchanged)"
        added = int(result.get("added_lines") or 0)
        removed = int(result.get("removed_lines") or 0)
        change_count = f" (+{added} -{removed})" if added or removed else ""
        created = result.get("created") if "created" in result else not result.get("previous_preview")
        verb = "Created" if created else "Edited"
        return f"{verb} {path or 'file'}{change_count}"
    if operation.tool == "read_file":
        if result.get("skipped"):
            return f"Skipped {path or 'file'} ({result.get('skip_reason', 'not text')})"
        return f"Read {path or 'file'}" if result.get("ok") else f"Could not read {path or 'file'}"
    if operation.tool == "list_files":
        count = len(result.get("files", [])) if isinstance(result.get("files"), list) else 0
        return f"Listed {count} files" if result.get("ok") else "Could not list files"
    if operation.tool == "run_command":
        command = _short_value(operation.arguments.get("command", "command"), 76)
        return f"$ {command}"
    if operation.tool == "spawn_subagent":
        role = str(result.get("role") or operation.arguments.get("role") or "specialist")
        return f"{role.title()} {result.get('status', 'completed')}"
    return f"{operation.tool} {'completed' if result.get('ok') else 'failed'}"


def operation_error(operation: ToolOperation) -> str:
    result = operation.result or {}
    if operation.tool == "run_command":
        error = str(result.get("error") or "").strip()
        if error:
            return f"Command failed: {_short_value(error, 150)}"
        code = result.get("returncode")
        output_lines = [line.strip() for line in str(result.get("output") or "").splitlines() if line.strip()]
        if output_lines:
            return f"Command failed (exit {code if code is not None else '?'}): {_short_value(output_lines[0], 130)}"
        return f"Command failed (exit {code if code is not None else '?'})"
    error = _short_value(result.get("error", "unknown error"), 150)
    return f"{operation.tool} failed: {error}"


def _bounded_lines(value: Any, limit: int = MAX_DETAIL_LINES) -> list[str]:
    lines = str(value or "").splitlines()
    if len(lines) <= limit:
        return lines
    head = max(1, limit - 3)
    return lines[:head] + [f"... {len(lines) - head} more lines hidden ..."] + lines[-2:]


def present_model_error(error: str) -> str:
    """Return an actionable terminal-safe explanation for common provider failures."""
    normalized = error.lower()
    if "http 402" in normalized or "insufficient balance" in normalized:
        return "Model provider rejected the request: account balance is insufficient. Add credit or use a funded API key/model."
    if "http 401" in normalized or "invalid api key" in normalized or "incorrect api key" in normalized:
        return "Model provider rejected the API key. Check LLM_API_KEY and the configured provider."
    if "http 403" in normalized:
        return "Model provider denied access. Check the API key permissions and selected model."
    if "http 429" in normalized or "rate limit" in normalized or "quota" in normalized:
        return "Model provider rate limit or quota was reached. Wait and retry, or check the account quota."
    if "timed out" in normalized or "timeout" in normalized:
        return "Model request timed out. Check the provider connection or increase api-timeout."
    first_line = error.splitlines()[0].strip()
    return textwrap.shorten(first_line or "model request failed", width=180, placeholder="...")


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
        return "[started]"
    if event.type == "model_thinking":
        return f"[thinking] turn {data.get('turn', '?')}"
    if event.type == "skills_loaded":
        return f"[context] skills: {', '.join(data.get('skills', []))}"
    if event.type == "memory_retrieved":
        return f"[context] {len(data.get('memory_ids', []))} project memories"
    if event.type == "memory_saved":
        return f"[memory] saved {len(data.get('memory_ids', []))} items"
    if event.type == "context_compacted":
        return f"[context] compacted {data.get('before_tokens', '?')} -> {data.get('after_tokens', '?')} tokens"
    if event.type == "task_resumed":
        return "[resumed] task context"
    if event.type == "subagent_started":
        return f"[agent] {data.get('role', 'specialist')} started: {textwrap.shorten(str(data.get('task', '')), width=120, placeholder='...')}"
    if event.type == "subagent_finished":
        role = data.get("role", "specialist")
        status = data.get("status", "completed")
        return f"[agent] {role} {status}: {textwrap.shorten(str(data.get('summary', '')), width=160, placeholder='...')}"
    if event.type == "model_retrying":
        return f"[retry] model request in {data.get('delay_ms', '?')} ms"
    if event.type == "model_rate_limited":
        return f"[waiting] model rate limit ({data.get('waited_ms', '?')} ms)"
    if event.type == "tool_started":
        arguments = data.get("arguments") or {}
        return f"[tool] {data.get('tool', '<unknown>')} {arguments}"
    if event.type == "tool_finished":
        tool = str(data.get("tool", "<unknown>"))
        result = data.get("result") or {}
        if not isinstance(result, dict):
            return f"[error] {tool} returned an invalid result"
        if not result.get("ok"):
            return f"[error] {tool}: {textwrap.shorten(str(result.get('error', 'unknown error')), width=160, placeholder='...')}"
        if tool == "run_command":
            return f"[tool] run_command exit {result.get('returncode', '?')} ({result.get('duration_ms', '?')} ms)"
        if tool == "write_file":
            state = "changed" if result.get("changed") else "unchanged"
            return f"[tool] write_file {result.get('path', '?')} {state}"
        if tool == "read_file":
            return f"[tool] read_file {result.get('path', '?')}"
        if tool == "list_files":
            return f"[tool] list_files {len(result.get('files', []))} files"
        return f"[tool] {tool} complete"
    if event.type == "command_approval_requested":
        return f"[approval] {data.get('command', '')}"
    if event.type == "command_approval_resolved":
        return f"[approval] {data.get('decision', 'resolved')}: {data.get('command', '')}"
    if event.type == "assistant_message":
        return "[completed] response received"
    if event.type == "task_finished":
        return "[completed]"
    if event.type == "task_cancelling":
        return "[cancelling]"
    if event.type == "task_cancelled":
        return "[cancelled]"
    if event.type == "task_error":
        return f"[error] {present_model_error(str(data.get('error', 'unknown error')))}"
    return f"[{event.type.replace('_', ' ')}]"


def _format_status_number(value: Any) -> str:
    """Format a status counter without making a malformed service response fatal."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "?"


def format_memory_status(status: Any) -> str:
    """Render long- and short-term memory state for the local TUI."""
    if not isinstance(status, dict):
        return "Memory status is unavailable."

    lines = ["Memory status"]
    long_term = status.get("long_term")
    if not isinstance(long_term, dict):
        lines.append("  Long-term: unavailable")
    else:
        available = bool(long_term.get("available", long_term.get("enabled", False)))
        count = long_term.get("count")
        if count is None:
            description = "available" if available else "unavailable"
        else:
            item_count = _format_status_number(count)
            noun = "item" if item_count == "1" else "items"
            description = f"{'available' if available else 'unavailable'} | {item_count} saved {noun}"
        lines.append(f"  Long-term: {description}")
        path = long_term.get("path")
        if path:
            lines.append(f"    Store: {path}")
        budget = long_term.get("context_char_budget")
        if budget is not None:
            lines.append(f"    Retrieval budget: {_format_status_number(budget)} chars")
        error = long_term.get("error")
        if error:
            lines.append(f"    Error: {_short_value(error, 180)}")

    short_term = status.get("short_term")
    if not isinstance(short_term, dict) or short_term.get("available") is False:
        lines.append("  Short-term: no task context available")
        return "\n".join(lines)

    task_id = short_term.get("task_id")
    task_status = short_term.get("task_status") or short_term.get("status") or "available"
    task_suffix = f" task {str(task_id)[:8]}" if task_id else ""
    lines.append(f"  Short-term: {task_status}{task_suffix}")

    context_parts: list[str] = []
    message_count = short_term.get("message_count")
    if message_count is not None:
        context_parts.append(f"{_format_status_number(message_count)} messages")
    estimated_tokens = short_term.get("estimated_tokens")
    max_tokens = short_term.get("max_tokens")
    if estimated_tokens is not None and max_tokens is not None:
        context_parts.append(f"{_format_status_number(estimated_tokens)} / {_format_status_number(max_tokens)} tokens")
    elif estimated_tokens is not None:
        context_parts.append(f"{_format_status_number(estimated_tokens)} tokens")
    threshold_tokens = short_term.get("threshold_tokens")
    if threshold_tokens is not None:
        context_parts.append(f"compact at {_format_status_number(threshold_tokens)}")
    if context_parts:
        lines.append("    Context: " + " | ".join(context_parts))

    summary_key = "has_compacted_summary" if "has_compacted_summary" in short_term else "has_summary"
    if summary_key in short_term:
        summary = "compacted summary present" if short_term.get(summary_key) else "full recent context"
        lines.append(f"    Summary: {summary}")

    session_parts: list[str] = []
    if "persisted" in short_term:
        session_parts.append("persisted" if short_term.get("persisted") else "not persisted")
    if "resumable" in short_term:
        session_parts.append("resume available" if short_term.get("resumable") else "not resumable")
    if session_parts:
        lines.append("    Session: " + " | ".join(session_parts))

    last_compaction = short_term.get("last_compaction")
    if isinstance(last_compaction, dict):
        before = last_compaction.get("before_tokens")
        after = last_compaction.get("after_tokens")
        if before is not None and after is not None:
            lines.append(
                f"    Last compaction: {_format_status_number(before)} -> {_format_status_number(after)} tokens"
            )
        else:
            lines.append("    Last compaction: recorded")
    return "\n".join(lines)


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
            permission_mode=config.permission_mode,
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
    if key == "permission-mode":
        mode = value.strip().lower()
        if mode not in {"approval", "auto"}:
            raise ValueError("permission-mode must be approval or auto")
        return config.with_overrides(permission_mode=mode), demo
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

    def __init__(
        self,
        config: Config,
        demo: bool = False,
        *,
        color: str = "auto",
        trust_store: WorkspaceTrustStore | None = None,
    ):
        if color not in COLOR_MODES:
            raise ValueError(f"color must be one of: {', '.join(sorted(COLOR_MODES))}")
        # Offline demo mode is deterministic and non-interactive; keep its
        # sample write loop runnable without waiting for a human approval.
        self.config = config.with_overrides(permission_mode="auto") if demo and config.permission_mode == "approval" else config
        self.demo = demo
        self.color = color
        self.service = AgentService(self.config)
        self.models = ModelManager(self.config)
        self.trust_store = trust_store or WorkspaceTrustStore()
        self._selected_skills: tuple[str, ...] = ()
        self._last_task_id: str | None = None
        # This is intentionally separate from _last_task_id. The latter is
        # used for activity/details, while this one controls whether ordinary
        # input is sent as the next turn of a selected conversation.
        self._active_conversation_task_id: str | None = None
        # Some hosts inject NO_COLOR=1 for child processes, which used to
        # flatten the entire interactive UI to one foreground color. Fullscreen
        # LimoCode has an explicit --color switch; use it as the source of
        # truth and reserve --color never as the intentional opt-out.
        self._use_colour = sys.stdout.isatty() and self.color != "never"
        self._screen_lines: list[str] = []
        self._screen_input = ""
        self._screen_scroll = 0
        self._screen_active: tuple[TaskRecord, TaskView, int] | None = None
        self._screen_last_view: TaskView | None = None
        self._screen_stream = ""
        self._screen_activity_expanded = False
        self._screen_focus_activity = False
        self._screen_selected_operation = -1
        self._screen_expanded_operations: set[int] = set()
        self._screen_status = "Ready · /help · /model · /skills"
        self._screen_last_ctrl_c = 0.0
        self._screen_last_frame: tuple[Any, ...] | None = None
        self._screen_error_seen = False
        # Active-conversation compaction runs off the UI thread so the transcript
        # keeps repainting while a large history is summarized.  The worker
        # only writes this small view; the UI thread publishes its result.
        self._compact_work: ContextCompactionView | None = None
        self._compact_work_lock = threading.Lock()
        # Entries are committed transcript blocks rather than a reconstruction
        # of the latest task state, so their displayed order remains stable.
        self._pt_history: list[tuple[str, Any]] = []
        self._pt_running = False
        self._pt_app: Any = None
        self._pt_body: Any = None
        self._pt_composer: Any = None
        self._pt_command_menu_window: Any = None
        self._pt_mode_window: Any = None
        self._pt_footer_window: Any = None
        self._pt_approval: tuple[str, str, bool, str] | None = None
        self._pt_change_approval: tuple[str, str, str] | None = None
        self._pt_stop = False
        self._pt_workspace_trusted: bool | None = None
        self._refresh_workspace_trust()
        self._pt_expanded_activities: set[str] = set()
        self._pt_expanded_operations: set[tuple[str, int]] = set()
        self._pt_expanded_diffs: set[tuple[str, int]] = set()
        self._pt_expanded_outputs: set[tuple[str, int]] = set()
        self._pt_expanded_prompts: set[str] = set()
        self._pt_menu_options: list[CommandMenuOption] = []
        self._pt_menu_index = 0
        self._pt_menu_scroll_top = 0
        self._pt_menu_title = ""

    def run(self) -> None:
        """Run the interactive UI when connected to a real terminal."""
        if _PROMPT_TOOLKIT_AVAILABLE and sys.stdin.isatty() and sys.stdout.isatty():
            self._run_prompt_toolkit()
        else:
            self._run_line_interface()

    def _pt_color_depth(self):
        """Return a stable color depth independent of injected NO_COLOR vars."""
        if ColorDepth is None:
            return None
        if self.color == "never":
            return ColorDepth.DEPTH_1_BIT
        if self.color == "always":
            return ColorDepth.TRUE_COLOR
        # A 256-color baseline retains semantic contrast on legacy PowerShell
        # hosts while preserving the black, restrained Codex-like canvas.
        return ColorDepth.DEPTH_8_BIT

    def _run_prompt_toolkit(self) -> None:
        """Run the Codex-style full-screen interface with reliable Windows input."""
        style = Style.from_dict(PROMPT_TOOLKIT_STYLE_RULES)
        self._pt_composer = TextArea(
            multiline=False,
            height=1,
            prompt=[("class:input.prompt", "› ")],
            style="class:input",
            # The transcript owns vertical navigation. In particular, some
            # Windows touchpads are decoded as Up/Down key events; retaining
            # Prompt Toolkit's default in-memory history would replace the
            # draft with a previous command instead of scrolling the view.
            history=DummyHistory(),
            scrollbar=False,
            wrap_lines=True,
            focus_on_click=True,
            accept_handler=self._pt_accept_composer,
        )
        self._pt_composer.buffer.on_text_changed += self._pt_on_composer_text_changed
        body_control = FormattedTextControl(self._pt_fragments, focusable=False)
        transcript = Window(body_control, wrap_lines=True, dont_extend_height=False)
        self._pt_command_menu_window = Window(
            FormattedTextControl(self._pt_command_menu_fragments, focusable=False),
            wrap_lines=True,
            dont_extend_height=True,
            style="class:command-menu",
        )
        command_menu = ConditionalContainer(
            self._pt_command_menu_window,
            filter=Condition(lambda: bool(self._pt_menu_options)),
        )
        # Match Codex's pane hierarchy: the transcript owns scrolling while
        # the composer stays in the bottom pane. Keeping these controls out of
        # the scrollable content prevents wheel/drag events from reaching the
        # focused editor and changing input history.
        self._pt_body = CodexScrollablePane(
            transcript,
            show_scrollbar=True,
            display_arrows=False,
            keep_cursor_visible=False,
            keep_focused_window_visible=False,
        )
        keys = KeyBindings()

        menu_visible = Condition(lambda: bool(self._pt_menu_options))

        @keys.add("up", filter=menu_visible, eager=True)
        def _menu_previous(event):
            self._pt_move_menu(-1)
            event.app.invalidate()

        @keys.add("down", filter=menu_visible, eager=True)
        def _menu_next(event):
            self._pt_move_menu(1)
            event.app.invalidate()

        @keys.add("c-p", filter=menu_visible, eager=True)
        def _menu_previous_ctrl(event):
            self._pt_move_menu(-1)
            event.app.invalidate()

        @keys.add("c-n", filter=menu_visible, eager=True)
        def _menu_next_ctrl(event):
            self._pt_move_menu(1)
            event.app.invalidate()

        @keys.add("tab", filter=menu_visible, eager=True)
        def _menu_complete(event):
            self._pt_complete_menu_option()
            event.app.invalidate()

        @keys.add("up", filter=Condition(lambda: not self._pt_menu_options), eager=True)
        def _scroll_up_from_key(event):
            self._pt_body._scroll_by(-3)
            event.app.invalidate()

        @keys.add("down", filter=Condition(lambda: not self._pt_menu_options), eager=True)
        def _scroll_down_from_key(event):
            self._pt_body._scroll_by(3)
            event.app.invalidate()

        @keys.add("enter", filter=menu_visible, eager=True)
        def _menu_select(event):
            self._pt_select_menu_option()
            event.app.invalidate()

        @keys.add("escape", eager=True)
        def _escape(event):
            if self._pt_menu_options:
                # Codex dismisses the popup while retaining the user's draft;
                # the next edit is what deliberately opens matching choices.
                self._pt_clear_command_menu()
                event.app.invalidate()
                return
            if self._screen_active and self._screen_active[0].status in {"queued", "running"}:
                self.service.cancel_task(self._screen_active[0].id)
            else:
                self._pt_composer.text = ""

        @keys.add("c-c")
        def _ctrl_c(event):
            if self._screen_active and self._screen_active[0].status in {"queued", "running"}:
                self.service.cancel_task(self._screen_active[0].id)
            else:
                event.app.exit()

        @keys.add("y", filter=Condition(lambda: self._pt_approval is not None))
        def _approve(event):
            if self._pt_approval:
                command = self._pt_approval[1]
                self.service.approve_command(self._screen_active[0].id, self._pt_approval[0], True, scope="once")
                self._pt_history.append(("approval_resolved", (True, command)))
                self._pt_approval = None

        @keys.add("a", filter=Condition(lambda: self._pt_approval is not None and self._pt_approval[2]))
        def _approve_command_family(event):
            if self._pt_approval:
                approval_id, command, _allow_always, family_label = self._pt_approval
                self.service.approve_command(self._screen_active[0].id, approval_id, True, scope="always")
                self._pt_history.append(("approval_resolved", (True, f"{command} (always allow {family_label})")))
                self._pt_approval = None

        @keys.add("y", filter=Condition(lambda: self._pt_change_approval is not None))
        def _approve_change(event):
            if self._pt_change_approval and self._screen_active:
                approval_id, changeset_id, summary = self._pt_change_approval
                self.service.approve_changeset(self._screen_active[0].id, approval_id, True)
                self._pt_history.append(("approval_resolved", (True, summary)))
                self._pt_change_approval = None

        @keys.add("n", filter=Condition(lambda: self._pt_approval is not None))
        def _reject(event):
            if self._pt_approval:
                command = self._pt_approval[1]
                self.service.approve_command(self._screen_active[0].id, self._pt_approval[0], False, scope="once")
                self._pt_history.append(("approval_resolved", (False, command)))
                self._pt_approval = None

        @keys.add("n", filter=Condition(lambda: self._pt_change_approval is not None))
        def _reject_change(event):
            if self._pt_change_approval and self._screen_active:
                approval_id, changeset_id, summary = self._pt_change_approval
                self.service.approve_changeset(self._screen_active[0].id, approval_id, False)
                self._pt_history.append(("approval_resolved", (False, summary)))
                self._pt_change_approval = None

        @keys.add("c-l")
        def _clear(event):
            self._pt_history.clear()
            self._pt_expanded_activities.clear()
            self._pt_expanded_operations.clear()
            self._pt_expanded_diffs.clear()
            self._pt_expanded_outputs.clear()
            self._pt_expanded_prompts.clear()
            self._screen_lines.clear()
            event.app.invalidate()

        @keys.add("c-t")
        def _toggle_activity(event):
            view = self._pt_latest_activity_view()
            if view and (view.operations or view.context_updates):
                self._pt_toggle_activity(view)
                event.app.invalidate()

        @keys.add("pageup", eager=True)
        def _page_up(event):
            self._pt_body._scroll_by(-max(3, self._pt_body._codex_view_height // 2))
            event.app.invalidate()

        @keys.add("pagedown", eager=True)
        def _page_down(event):
            self._pt_body._scroll_by(max(3, self._pt_body._codex_view_height // 2))
            event.app.invalidate()

        # Some Windows terminals report a wheel gesture without a pointer
        # position. Prompt Toolkit's default binding turns these into Up/Down
        # keys, which makes the focused composer consume the gesture. Handle
        # them eagerly at the application level so they always scroll history.
        @keys.add(Keys.ScrollUp, eager=True)
        def _scroll_up_without_pointer(event):
            self._pt_body._scroll_by(-3)
            event.app.invalidate()

        @keys.add(Keys.ScrollDown, eager=True)
        def _scroll_down_without_pointer(event):
            self._pt_body._scroll_by(3)
            event.app.invalidate()

        @keys.add("home", filter=Condition(lambda: self._pt_composer.text == ""))
        def _scroll_top(event):
            self._pt_body._scroll_by(-self._pt_body._codex_virtual_height)
            event.app.invalidate()

        @keys.add("end", filter=Condition(lambda: self._pt_composer.text == ""))
        def _scroll_bottom(event):
            self._pt_body.scroll_to_bottom()
            event.app.invalidate()

        @keys.add("y", filter=Condition(lambda: self._pt_workspace_trusted is None))
        def _trust_workspace(event):
            saved = self._trust_current_workspace()
            self._screen_status = "Workspace trusted and saved" if saved else (
                self.trust_store.last_error or "Unable to save workspace trust"
            )
            event.app.invalidate()

        @keys.add("n", filter=Condition(lambda: self._pt_workspace_trusted is None))
        def _decline_workspace(event):
            self._screen_status = "Workspace not trusted; no tasks were run"
            event.app.exit()

        self._pt_mode_window = Window(FormattedTextControl(self._pt_mode_text), height=1)
        self._pt_footer_window = Window(FormattedTextControl(self._pt_hint_text), height=1)
        footer = ConditionalContainer(
            self._pt_footer_window,
            # The popup takes the footer's slot, as in Codex's bottom pane.
            filter=Condition(lambda: not bool(self._pt_menu_options)),
        )
        root = CodexMouseRoutingHSplit([
            self._pt_body,
            self._pt_composer,
            self._pt_mode_window,
            command_menu,
            footer,
        ], transcript=self._pt_body)
        self._pt_app = Application(
            layout=Layout(root, focused_element=self._pt_composer),
            key_bindings=keys,
            style=style,
            full_screen=True,
            color_depth=self._pt_color_depth(),
            mouse_support=True,
            refresh_interval=0.12,
        )
        self._pt_stop = False
        import threading
        poller = threading.Thread(target=self._pt_poll_loop, daemon=True)
        poller.start()
        try:
            self._pt_app.run()
        finally:
            self._pt_stop = True
            # The refresh thread may be between loop iterations while the
            # application event loop is being torn down.  Let it observe the
            # stop flag before dropping the application reference.
            poller.join(timeout=0.5)
            self._pt_app = None

    def _pt_poll_loop(self) -> None:
        while not self._pt_stop:
            if self._screen_active:
                self._pt_pump_task()
            self._poll_compaction()
            app = self._pt_app
            if app and app.is_running:
                try:
                    app.invalidate()
                except RuntimeError:
                    # Prompt Toolkit can close its asyncio loop between its
                    # internal lifecycle check and the cross-thread redraw
                    # request.  The UI is already shutting down in that case.
                    return
            time.sleep(0.08)

    def _compact_work_snapshot(self) -> ContextCompactionView | None:
        """Return the current compaction state without exposing worker races."""
        with self._compact_work_lock:
            return self._compact_work

    def _compact_work_running(self) -> bool:
        work = self._compact_work_snapshot()
        return bool(work and not work.completed.is_set())

    def _start_compaction(self, conversation_id: str) -> bool:
        """Start conversation-context compaction without freezing the UI."""
        with self._compact_work_lock:
            current = self._compact_work
            if current and not current.completed.is_set():
                return False
            work = ContextCompactionView(conversation_id)
            self._compact_work = work

        def worker() -> None:
            try:
                work.result = self.service.compact_conversation(conversation_id)
            except Exception as exc:  # surface failures in the transcript
                work.error = str(exc) or exc.__class__.__name__
            finally:
                work.finished_at = time.monotonic()
                work.completed.set()

        thread = threading.Thread(
            target=worker,
            name="limocode-context-compaction",
            daemon=True,
        )
        thread.start()
        self._screen_status = "Working · compacting conversation context"
        return True

    def _poll_compaction(self) -> None:
        """Publish a completed compaction exactly once from the UI loop."""
        work = self._compact_work_snapshot()
        if not work or not work.completed.is_set():
            return
        with self._compact_work_lock:
            if work.published:
                return
            work.published = True

        elapsed = format_duration(work.duration_seconds)
        if work.error:
            text = f"Conversation compaction failed: {_short_value(work.error, 180)} · {elapsed}"
        elif work.result is None:
            text = f"Conversation context is unavailable · {elapsed}"
        else:
            result = work.result
            if result.compacted:
                text = (
                    f"Conversation compacted: {result.before_tokens:,} -> "
                    f"{result.after_tokens:,} tokens · {elapsed}"
                )
            else:
                text = f"Conversation context already compact · {elapsed}"
        self._screen_status = text
        self._append_screen_text(text)
        with self._compact_work_lock:
            if self._compact_work is work:
                self._compact_work = None

    def _pt_submit(self, text: str) -> None:
        try:
            command, arguments = parse_command(text)
        except ValueError as exc:
            self._pt_history.append(("error", f"Invalid command: {exc}"))
            return
        if command in {"quit", "exit"}:
            if self._pt_app:
                self._pt_app.exit()
            return
        if command == "task":
            # Trust is required before code can run, but informational slash
            # commands must remain usable so a user can inspect/configure the
            # agent before making that decision.
            if self._pt_workspace_trusted is not True:
                self._screen_status = "Trust this workspace before running a task (y / n)"
                return
            if len(text) > LONG_PROMPT_CHARS:
                prompt_key = f"prompt-{time.monotonic_ns()}"
                self._pt_history.append(("user_prompt", (prompt_key, text)))
            else:
                self._pt_history.append(("user", text))
            resume_from = self._active_conversation_id()
            if resume_from:
                self._start_screen_task(text, resume_from=resume_from)
            else:
                self._start_screen_task(text)
            if self._pt_body:
                self._pt_body.scroll_to_bottom()
            return
        # Reuse command semantics; _append_screen_text mirrors command output
        # into the prompt-toolkit transcript while retaining line-mode support.
        if command == "activity" and not arguments:
            view = self._pt_latest_activity_view()
            if view:
                self._pt_toggle_activity(view)
                return
        try:
            self._submit_screen_line(text)
        except (ValueError, OSError) as exc:
            self._pt_history.append(("error", str(exc)))

    def _pt_accept_composer(self, buffer) -> bool:
        text = buffer.text.strip()
        if text:
            self._pt_submit(text)
        # False asks Prompt Toolkit to clear the accepted input itself.
        return False

    def _pt_on_composer_text_changed(self, buffer) -> None:
        """Keep the small command picker synchronized with the composer."""
        self._pt_update_command_menu(buffer.text)
        if self._pt_app:
            self._pt_app.invalidate()

    def _pt_update_command_menu(self, text: str) -> None:
        title, options = self._pt_menu_options_for_text(text)
        previous = tuple((option.kind, option.value) for option in self._pt_menu_options)
        current = tuple((option.kind, option.value) for option in options)
        self._pt_menu_options = options
        self._pt_menu_title = title
        if current != previous:
            self._pt_menu_index = 0
            self._pt_menu_scroll_top = 0
        elif options:
            self._pt_menu_index = min(self._pt_menu_index, len(options) - 1)
            self._pt_ensure_menu_selection_visible()
        else:
            self._pt_menu_index = 0
            self._pt_menu_scroll_top = 0

    def _pt_clear_command_menu(self) -> None:
        self._pt_menu_options = []
        self._pt_menu_index = 0
        self._pt_menu_scroll_top = 0
        self._pt_menu_title = ""

    def _pt_menu_options_for_text(self, text: str) -> tuple[str, list[CommandMenuOption]]:
        """Return contextual slash-command choices without parsing incomplete input."""
        source = str(text).lstrip()
        if not source.startswith("/") or "\n" in source:
            return "", []
        body = source[1:]
        command, separator, remainder = body.partition(" ")
        command = command.lower()
        argument = remainder.lstrip()

        if not separator:
            if command == "model":
                return "Select model", self._pt_model_menu_options(argument)
            if command == "skill":
                return "Select skill", self._pt_skill_menu_options(argument)
            if command == "memory":
                return "Project memory", self._pt_memory_menu_options(argument)
            if command == "config":
                return "Configure LimoCode", self._pt_config_menu_options(argument)
            if command == "mode":
                return "Permission mode", self._pt_mode_menu_options(argument)
            if command == "changes":
                return "Select task", self._pt_task_menu_options(command, argument)
            if command == "undo":
                return "Select changeset", self._pt_changeset_menu_options(argument)
            if command in {"history", "resume"}:
                return "Resume conversation", self._pt_resumable_conversation_menu_options(argument)
            matches = [
                CommandMenuOption("command", name, f"/{name}", description)
                for name, description in SLASH_COMMANDS
                if name.startswith(command)
                # Keep the long-standing /mo completion focused on model(s);
                # /mode remains available from / and by typing /mode directly.
                and not (command == "mo" and name == "mode")
            ]
            return "Commands", matches

        if command == "model":
            return "Select model", self._pt_model_menu_options(argument)
        if command == "skill":
            return "Select skill", self._pt_skill_menu_options(argument)
        if command == "memory":
            return "Project memory", self._pt_memory_menu_options(argument)
        if command == "mode":
            return "Permission mode", self._pt_mode_menu_options(argument)
        if command == "config":
            config_command, config_separator, config_value = argument.partition(" ")
            if not config_separator:
                if config_command.lower() == "model":
                    return "Select model", self._pt_model_menu_options("", config=True)
                if config_command.lower() == "permission-mode":
                    return "Permission mode", self._pt_mode_menu_options("", config=True)
                return "Configure LimoCode", self._pt_config_menu_options(config_command)
            if config_command.lower() == "model":
                return "Select model", self._pt_model_menu_options(config_value.strip(), config=True)
            if config_command.lower() == "permission-mode":
                return "Permission mode", self._pt_mode_menu_options(config_value.strip(), config=True)
        if command in {"history", "resume"}:
            return "Resume conversation", self._pt_resumable_conversation_menu_options(argument)
        if command == "changes":
            return "Select task", self._pt_task_menu_options(command, argument)
        if command == "undo":
            return "Select changeset", self._pt_changeset_menu_options(argument)
        if command in {"open", "details", "detail", "inspect", "activity", "prompt"}:
            return "Select task", self._pt_task_menu_options(command, argument)
        return "", []

    def _pt_mode_menu_options(self, query: str, *, config: bool = False) -> list[CommandMenuOption]:
        """Offer the two supported permission modes as executable choices."""
        match = query.strip().lower()
        current = str(getattr(self.config, "permission_mode", "approval") or "approval").lower()
        prefix = "/config permission-mode " if config else "/mode "
        options = [
            CommandMenuOption(
                "execute",
                prefix + "approval",
                "approval" + (" (current)" if current == "approval" else ""),
                "Ask before applying file changes.",
            ),
            CommandMenuOption(
                "execute",
                prefix + "auto",
                "auto" + (" (current)" if current == "auto" else ""),
                "Apply file changes automatically.",
            ),
        ]
        return [option for option in options if not match or option.value.rsplit(" ", 1)[-1].startswith(match)]

    def _pt_changeset_menu_options(self, query: str) -> list[CommandMenuOption]:
        """Offer tracked changesets for the destructive /undo command."""
        match = query.strip().lower()
        options: list[CommandMenuOption] = []
        for item in self.service.list_changesets():
            changeset_id = str(item.get("id") or "")
            if not changeset_id:
                continue
            files = item.get("files") or []
            paths = ", ".join(
                str(file.get("path", "file"))
                for file in files
                if isinstance(file, dict)
            ) or "workspace"
            searchable = f"{changeset_id} {paths} {item.get('status', '')}".lower()
            if match and match not in searchable:
                continue
            options.append(CommandMenuOption(
                "execute",
                f"/undo {changeset_id[:8]}",
                f"{changeset_id[:8]}  {paths}",
                str(item.get("status", "tracked")),
            ))
        return options

    def _pt_model_menu_options(self, query: str, *, config: bool = False) -> list[CommandMenuOption]:
        match = query.lower()
        options: list[CommandMenuOption] = []
        for item in self.models.available():
            if match and match not in item.name.lower():
                continue
            current = " (current)" if item.name == self.config.model else ""
            kind = "config_model" if config else "model"
            options.append(CommandMenuOption(
                kind,
                item.name,
                item.name + current,
                self._pt_model_description(item.name, item.context_window),
            ))
        return options

    @staticmethod
    def _pt_model_description(name: str, context_window: int) -> str:
        lowered = name.lower()
        for marker, description in MODEL_DESCRIPTIONS.items():
            if marker in lowered:
                return description
        return f"Configured model · {context_window:,} token context."

    def _pt_skill_menu_options(self, query: str) -> list[CommandMenuOption]:
        match = query.lower()
        options = [
            CommandMenuOption("execute", "/skill auto", "auto", "Choose skills automatically for each task."),
            CommandMenuOption("execute", "/skill reload", "reload", "Reload skills from this workspace."),
        ]
        for item in self.service.skill_manager.metadata():
            if match and match not in item.name.lower():
                continue
            options.append(CommandMenuOption("execute", f"/skill {item.name}", item.name, item.description))
        return [option for option in options if not match or match in option.label.lower()]

    @staticmethod
    def _pt_memory_menu_options(query: str) -> list[CommandMenuOption]:
        options = [
            CommandMenuOption("execute", "/memory", "show", "List project memory."),
            CommandMenuOption("execute", "/memory status", "status", "Show durable and task-context memory state."),
            CommandMenuOption("insert", "/memory add ", "add", "Save a durable project rule."),
            CommandMenuOption("insert", "/memory search ", "search", "Search project memory."),
            CommandMenuOption("insert", "/memory delete ", "delete", "Delete an item by id."),
        ]
        return [option for option in options if not query or option.label.startswith(query.lower())]

    @staticmethod
    def _pt_config_menu_options(query: str) -> list[CommandMenuOption]:
        settings = (
            ("model", "Choose the model for tasks, or switch the active model."),
            ("workspace", "Set the workspace for a new session."),
            ("api-timeout", "Set model request timeout in seconds."),
            ("max-turns", "Set the maximum agent tool rounds."),
            ("command-timeout", "Set local command timeout in seconds."),
            ("approval-timeout", "Set command approval timeout in seconds."),
            ("request-gap", "Set the delay between model requests."),
            ("permission-mode", "Choose whether file changes require approval or apply automatically."),
            ("demo", "Turn offline demo mode on or off."),
        )
        return [
            CommandMenuOption("insert", f"/config {name} ", name, description)
            for name, description in settings
            if not query or name.startswith(query.lower())
        ]

    def _pt_task_menu_options(self, command: str, query: str) -> list[CommandMenuOption]:
        normalized_command = "details" if command == "detail" else command
        options: list[CommandMenuOption] = []
        for item in self.service.list_tasks(limit=12):
            task_id = str(item["id"])
            summary = task_summary(str(item["task"]), 42)
            if query and query.lower() not in task_id.lower() and query.lower() not in summary.lower():
                continue
            options.append(CommandMenuOption(
                "execute",
                f"/{normalized_command} {task_id[:8]}",
                f"{task_id[:8]}  {summary}",
                str(item["status"]),
            ))
        return options

    def _pt_resumable_conversation_menu_options(self, query: str) -> list[CommandMenuOption]:
        """Offer one selectable row per saved conversation.

        A conversation can contain multiple task records. The menu deliberately
        targets its stable conversation id rather than a task id, so selecting
        an older session always restores the newest resumable task in that
        conversation.
        """
        match = query.lower()
        options: list[CommandMenuOption] = []
        for item in self._list_conversations(limit=12, resumable_only=True):
            conversation_id = str(item.get("id") or item.get("conversation_id") or "")
            root_task = self._conversation_task_text(item, "root_task")
            latest_task = self._conversation_task_text(item, "latest_task", fallback=root_task)
            title = self._conversation_title(item, fallback=root_task)
            searchable = " ".join((conversation_id, title, root_task, latest_task)).lower()
            if match and match not in searchable:
                continue
            continuation_task_id = item.get("continuation_task_id")
            if not isinstance(continuation_task_id, str) or not continuation_task_id:
                continue
            is_active = continuation_task_id == self._active_conversation_task_id
            task_count = int(item.get("task_count", 1) or 1)
            latest_snapshot = item.get("latest_task")
            latest_status = str(
                item.get("latest_status")
                or (latest_snapshot.get("status") if isinstance(latest_snapshot, dict) else "saved")
            ).capitalize()
            summary = task_summary(title, 46)
            description = (
                "Active conversation"
                if is_active
                else f"{task_count} task{'s' if task_count != 1 else ''} · {latest_status}"
            )
            options.append(CommandMenuOption(
                "execute",
                f"/resume {conversation_id[:8]}",
                f"{conversation_id[:8]}  {summary}",
                description,
            ))
        return options

    def _pt_move_menu(self, amount: int) -> None:
        option_count = len(self._pt_menu_options)
        if not option_count:
            self._pt_menu_index = 0
            self._pt_menu_scroll_top = 0
            return
        # Codex's CommandPopup uses a wrapping ScrollState rather than a
        # clamped list. This matters for long slash menus: arrowing beyond the
        # eighth entry must move the visible window, and both endpoints remain
        # reachable without changing the composer text.
        self._pt_menu_index = (self._pt_menu_index + amount) % option_count
        self._pt_ensure_menu_selection_visible()

    def _pt_ensure_menu_selection_visible(self) -> None:
        """Keep the selected popup item inside Codex's eight-item viewport."""
        option_count = len(self._pt_menu_options)
        if not option_count:
            self._pt_menu_index = 0
            self._pt_menu_scroll_top = 0
            return
        self._pt_menu_index = max(0, min(self._pt_menu_index, option_count - 1))
        visible_rows = min(MAX_COMMAND_MENU_ROWS, option_count)
        maximum_top = max(0, option_count - visible_rows)
        self._pt_menu_scroll_top = max(0, min(self._pt_menu_scroll_top, maximum_top))
        if self._pt_menu_index < self._pt_menu_scroll_top:
            self._pt_menu_scroll_top = self._pt_menu_index
        elif self._pt_menu_index >= self._pt_menu_scroll_top + visible_rows:
            self._pt_menu_scroll_top = self._pt_menu_index - visible_rows + 1

    def _pt_select_menu_option(self, index: int | None = None) -> None:
        if not self._pt_menu_options:
            return
        if index is not None:
            self._pt_menu_index = max(0, min(index, len(self._pt_menu_options) - 1))
            self._pt_ensure_menu_selection_visible()
        option = self._pt_menu_options[self._pt_menu_index]
        if option.kind == "command":
            if option.value == "continue":
                # Unlike the read-only commands, /continue needs an explicit
                # follow-up instruction. Previously this branch rewrote the
                # identical text and left the menu open, so Enter looked like
                # it had been ignored.
                self._pt_composer.text = "/continue "
                self._pt_composer.buffer.cursor_position = len(self._pt_composer.text)
                self._pt_clear_command_menu()
                message = "Continue selected. Add a follow-up instruction after /continue, then press Enter."
                self._screen_status = message
                self._pt_history.append(("command", message))
                if self._pt_app:
                    self._pt_app.invalidate()
                return
            if option.value in {"model", "skill", "memory", "config", "mode", "changes", "undo", "history", "resume", "open", "details", "inspect", "activity", "prompt"}:
                self._pt_composer.text = f"/{option.value}"
                if option.value != "model":
                    self._pt_composer.buffer.cursor_position = len(self._pt_composer.text)
                return
            self._pt_submit(f"/{option.value}")
        elif option.kind == "insert":
            self._pt_composer.text = option.value
            self._pt_composer.buffer.cursor_position = len(option.value)
            return
        elif option.kind in {"model", "config_model"}:
            self._pt_select_model_from_menu(option.value)
        else:
            self._pt_submit(option.value)
        self._pt_composer.text = ""
        self._pt_clear_command_menu()
        if self._pt_app:
            self._pt_app.invalidate()

    def _pt_complete_menu_option(self) -> None:
        """Complete the highlighted popup item without executing it."""
        if not self._pt_menu_options:
            return
        self._pt_ensure_menu_selection_visible()
        option = self._pt_menu_options[self._pt_menu_index]
        if option.kind == "command":
            value = option.value
            # Keep a trailing space so the contextual picker can immediately
            # offer arguments (for example, ``/model `` or ``/resume ``).
            self._pt_composer.text = f"/{value} "
        elif option.kind == "insert":
            self._pt_composer.text = option.value
        elif option.kind in {"model", "config_model"}:
            prefix = "/config model " if option.kind == "config_model" else "/model "
            self._pt_composer.text = prefix + option.value
        else:
            self._pt_composer.text = option.value
        self._pt_composer.buffer.cursor_position = len(self._pt_composer.text)

    def _pt_select_model_from_menu(self, name: str) -> None:
        self._screen_switch_model(name)
        if self.config.model == name:
            self._append_screen_text(f"Model switched to {name}")
        else:
            self._append_screen_text(f"Model selection failed: {self._screen_status}")

    def _pt_command_menu_fragments(self):
        if not self._pt_menu_options:
            return []
        self._pt_ensure_menu_selection_visible()
        visible_options = self._pt_menu_options[
            self._pt_menu_scroll_top:self._pt_menu_scroll_top + MAX_COMMAND_MENU_ROWS
        ]
        display_labels = [
            f"{index + 1}. {option.label}" if self._pt_menu_title == "Select model" else option.label
            for index, option in enumerate(self._pt_menu_options)
        ]
        name_width = max(self._display_width(label) for label in display_labels)
        content_width = max(24, self._terminal_width() - 4)
        description_width = max(12, content_width - 4 - name_width)
        # Codex has no frame, title, selection marker, or opaque menu panel.
        # Its popup is a compact two-column list below the composer.
        fragments: list[tuple[str, str] | tuple[str, str, Any]] = []
        for index in range(self._pt_menu_scroll_top, self._pt_menu_scroll_top + len(visible_options)):
            option = self._pt_menu_options[index]
            label = display_labels[index]
            selected = index == self._pt_menu_index
            handler = self._pt_menu_mouse_handler(index)
            row_style = "class:command-menu.selected" if selected else "class:command-menu.choice"
            name_style = row_style if selected else "class:command-menu.name"
            description_style = row_style if selected else "class:command-menu.meta"
            name_padding = " " * max(0, name_width - self._display_width(label))
            description_lines = self._wrap_display_text(option.description, description_width)
            fragments.append((name_style, "  " + label + name_padding + "  ", handler))
            fragments.append((description_style, description_lines[0] + "\n", handler))
            continuation = " " * (4 + name_width)
            for description_line in description_lines[1:]:
                fragments.append((description_style, continuation + description_line + "\n", handler))
        return fragments

    def _pt_menu_mouse_handler(self, index: int):
        def handler(event):
            if event.event_type == MouseEventType.MOUSE_UP:
                self._pt_select_menu_option(index)
            return None

        return handler

    def _pt_pump_task(self) -> None:
        record, view, sequence = self._screen_active
        task_error: str | None = None
        task_cancelled = False
        for event in self.service.events(record.id, sequence):
            sequence = event.sequence
            view.apply(event)
            if event.type == "assistant_delta":
                self._screen_stream += str(event.data.get("delta", ""))
            elif event.type == "model_retrying":
                # A retry replaces an interrupted streaming response. Do not
                # leave its partial prose above the eventual retry result.
                self._screen_stream = ""
            elif event.type == "tool_started":
                # Some providers emit a short assistant preface before a tool
                # call. It is not the final response, so remove it from the
                # visible answer area once the structured operation begins.
                self._screen_stream = ""
            elif event.type == "command_approval_requested":
                approval_id = event.data.get("approval_id")
                command = event.data.get("command")
                if isinstance(approval_id, str) and isinstance(command, str):
                    allow_always = event.data.get("allow_always") is True
                    family_label = str(event.data.get("family_label") or "this command type")
                    self._pt_approval = (approval_id, command, allow_always, family_label)
                    self._pt_history.append(("approval", {"command": command, "allow_always": allow_always, "family_label": family_label}))
            elif event.type == "changeset_approval_requested":
                approval_id = event.data.get("approval_id")
                changeset_id = event.data.get("id") or event.data.get("changeset_id")
                files = event.data.get("files") or []
                summary = ", ".join(str(item.get("path", "file")) for item in files if isinstance(item, dict)) or "workspace changes"
                if isinstance(approval_id, str) and isinstance(changeset_id, str):
                    self._pt_change_approval = (approval_id, changeset_id, summary)
                    self._pt_history.append(("change_approval", event.data))
            elif event.type == "task_error" and not self._screen_error_seen:
                self._screen_error_seen = True
                task_error = present_model_error(str(event.data.get("error", "unknown error")))
            elif event.type == "task_cancelled":
                task_cancelled = True
        self._screen_active = (record, view, sequence)
        if record.status in TERMINAL_TASK_STATUSES:
            # AgentService transitions the record before it emits the terminal
            # event. Freeze the view here too when this refresh observes that
            # small window, otherwise the archived completion keeps counting.
            view.finish(record.status)
            self._active_conversation_task_id = (
                record.id if self.service.get_continuable_task(record.id) else None
            )
            if record.status == "completed":
                self._pt_history.append(("assistant", record.result or self._screen_stream or "Completed."))
            elif task_error or record.error:
                self._pt_history.append(("error", task_error or present_model_error(record.error or "task failed")))
            elif task_cancelled or record.status == "cancelled":
                self._pt_history.append(("system", "Task cancelled."))
            if view.operations or view.context_updates:
                self._pt_history.append(("activity", view))
            self._pt_history.append(("completion", view))
            self._screen_stream = ""
            self._screen_status = "Ready"
            self._screen_active = None

    def _pt_fragments(self):
        # A worker may have finished between poll ticks (especially in tests
        # and for short contexts); publish its result before building the
        # transcript so the completion line is never one frame late.
        self._poll_compaction()
        fragments: list[tuple[str, str]] = self._pt_workspace_card_fragments()
        for kind, payload in self._pt_history:
            if kind == "user":
                fragments.extend(self._pt_prefixed_text_fragments(
                    str(payload),
                    style="class:user",
                    initial_prefix="\n  › ",
                    continuation_prefix="    ",
                ))
                fragments.append(("class:user", "\n"))
            elif kind == "user_prompt" and isinstance(payload, tuple) and len(payload) == 2:
                prompt_key, prompt = str(payload[0]), str(payload[1])
                prompt_handler = self._pt_prompt_mouse_handler(prompt_key)
                if prompt_key in self._pt_expanded_prompts:
                    fragments.extend(self._pt_prefixed_text_fragments(
                        prompt,
                        style="class:user",
                        initial_prefix="\n  › ",
                        continuation_prefix="    ",
                        handler=prompt_handler,
                    ))
                    fragments.append(("class:user", "\n", prompt_handler))
                else:
                    fragments.extend([
                        ("class:user", "\n  › ", prompt_handler),
                        ("class:activity.label", f"User prompt · {len(prompt)} chars", prompt_handler),
                        ("class:activity", " · click to expand\n", prompt_handler),
                    ])
            elif kind == "assistant":
                fragments.append(("class:assistant.marker", "\n  • "))
                fragments.extend(self._pt_markdown_fragments(str(payload), first_prefix="", continuation_prefix="  "))
                fragments.append(("class:assistant", "\n"))
            elif kind == "conversation":
                fragments.append(("class:workspace", "\n  " + str(payload) + "\n"))
            elif kind == "command":
                fragments.extend(self._pt_command_response_fragments(str(payload)))
            elif kind == "error":
                fragments.append(("class:error", "\n  ✕ " + str(payload) + "\n"))
            elif kind == "approval":
                approval = payload if isinstance(payload, dict) else {"command": str(payload)}
                allow_always = approval.get("allow_always") is True
                family_label = str(approval.get("family_label") or "this command type")
                fragments.extend([
                    ("class:warning", "\n  ! Command approval required\n"),
                    ("class:code", "    " + str(approval.get("command", "")) + "\n"),
                    ("class:trust", "    [y] Approve once" + (f"   [a] Always allow {family_label}" if allow_always else "") + "   [n] Reject\n"),
                ])
            elif kind == "change_approval" and isinstance(payload, dict):
                files = payload.get("files") or []
                # Render the patch as a real diff. Passing it through the
                # Markdown renderer turns ``-``/``+`` lines into bullets and
                # strips the very markers users need when reviewing a change.
                fragments.append(("class:warning", "\n  ! Proposed file changes\n"))
                paths = ", ".join(
                    str(item.get("path", "file"))
                    for item in files
                    if isinstance(item, dict)
                ) or "workspace"
                fragments.append(("class:code", "    " + paths + "\n"))
                for item in files:
                    if isinstance(item, dict) and item.get("unified_diff"):
                        fragments.extend(self._pt_diff_fragments(str(item["unified_diff"]), bool(item.get("diff_truncated"))))
                        fragments.append(("class:assistant", "\n"))
                # Keep the decision at the end of the patch. With follow-output
                # enabled this is the last visible row when an approval pauses
                # a task, so a long diff can never hide the action prompt.
                fragments.extend([
                    ("class:warning", "  ! File changes require approval\n"),
                    ("class:trust", "    Apply these changes?  [y] Apply   [n] Reject\n"),
                    ("class:separator", "  " + "-" * max(18, min(52, self._terminal_width() - 8)) + "\n"),
                ])
            elif kind == "approval_resolved" and isinstance(payload, tuple) and len(payload) == 2:
                approved, command = payload
                if approved:
                    fragments.extend([
                        ("class:approval", "\n  ✔ You approved LimoCode to run "),
                        ("class:code", str(command)),
                        ("class:approval", " this time\n"),
                    ])
                else:
                    fragments.extend([
                        ("class:warning", "\n  ! You rejected LimoCode running "),
                        ("class:code", str(command)),
                        ("class:warning", "\n"),
                    ])
            elif kind == "activity" and isinstance(payload, TaskView):
                fragments.append(("class:activity", "\n"))
                fragments.extend(self._pt_activity_fragments(payload))
            elif kind == "completion" and isinstance(payload, TaskView):
                fragments.extend(self._pt_completion_fragments(payload))
            else:
                fragments.append(("class:workspace", "\n  " + str(payload) + "\n"))
        if self._screen_active:
            view = self._screen_active[1]
            dots = "." * (int(time.monotonic() * 3) % 4)
            spinner = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")[int(time.monotonic() * 8) % 10]
            if view.cancelling:
                fragments.append(("class:warning", f"\n  ! Cancelling · {format_duration(view.duration_seconds)}\n"))
            else:
                fragments.append(("class:thinking", f"\n  {spinner} Working{dots} · {format_duration(view.duration_seconds)}\n"))
            if self._screen_stream:
                fragments.append(("class:assistant.marker", "\n  • "))
                fragments.extend(self._pt_markdown_fragments(self._screen_stream, first_prefix="", continuation_prefix="  "))
                fragments.append(("class:assistant", "\n"))
            if view.operations or view.context_updates:
                fragments.extend(self._pt_activity_fragments(view))
        compact = self._compact_work_snapshot()
        if compact and not compact.completed.is_set():
            # Keep the command status deliberately small: it should read like
            # Codex's live activity hint, while the final result becomes a
            # normal green command line once the worker completes.
            frames = (".", "..", "...", " ..", "  .", "   ")
            frame = frames[int(time.monotonic() * 5) % len(frames)]
            fragments.append((
                "class:command-working",
                f"\n  {frame:<3} Working · compacting conversation context · {format_duration(compact.duration_seconds)}\n",
            ))
        return fragments

    def _pt_workspace_card_fragments(self) -> list[tuple[str, str]]:
        """Render the compact workspace card shown at the start of a session.

        The real Codex TUI uses a narrow rounded-corner card rather than a
        full-width banner. Keeping this as fragments (instead of an embedded
        ANSI string) preserves Prompt Toolkit styling and mouse behaviour.
        """
        terminal_width = max(40, self._terminal_width())
        card_width = min(64, terminal_width - 4)
        # Two-column indentation plus the two vertical borders consume six
        # cells on each rendered line.
        inner_width = card_width - 6
        workspace = str(self.config.workspace)
        if len(workspace) > inner_width - 14:
            workspace = "..." + workspace[-(inner_width - 17):]
        model = str(self.config.model)
        if len(model) > inner_width - 20:
            model = model[: max(8, inner_width - 23)] + "..."
        def line(content: str, style: str = "class:workspace") -> list[tuple[str, str]]:
            clipped = content[:inner_width]
            return [
                ("class:header.border", "  │ "),
                (style, clipped.ljust(inner_width)),
                ("class:header.border", " │\n"),
            ]

        fragments: list[tuple[str, str]] = [
            ("class:header.border", f"  ╭{'─' * (card_width - 4)}╮\n"),
        ]
        fragments.extend(line(">_ LimoCode", "class:header.title"))
        fragments.extend(line(""))
        fragments.extend(line(f"model:     {model}  /model to change"))
        fragments.extend(line(f"directory: {workspace}", "class:workspace.path"))
        fragments.append(("class:header.border", f"  ╰{'─' * (card_width - 4)}╯\n"))
        if self._pt_workspace_trusted is None:
            fragments.append(("class:trust", "  Trust this workspace? [y] Trust and continue [n] Exit\n"))
        fragments.append(("class:workspace", "\n"))
        return fragments

    def _pt_prefixed_text_fragments(
        self,
        text: str,
        *,
        style: str,
        initial_prefix: str,
        continuation_prefix: str,
        handler: Any | None = None,
    ) -> list[tuple[str, str] | tuple[str, str, Any]]:
        """Render text with a stable message gutter across terminal wrapping.

        Codex's history cells explicitly wrap before rendering so a long URL or
        sentence never loses its ``›``/``•`` gutter when the terminal wraps.
        Prompt Toolkit otherwise wraps the complete formatted line after the
        prefix, which leaves continuation rows flush-left. Keep wrapping local
        to user prompts here; Markdown messages have their own renderer.
        """
        available = max(16, self._terminal_width() - len(continuation_prefix))
        fragments: list[tuple[str, str] | tuple[str, str, Any]] = []
        first = True
        for source_line in str(text).splitlines() or [""]:
            wrapped = textwrap.wrap(
                source_line,
                width=available,
                replace_whitespace=False,
                drop_whitespace=False,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [""]
            for item in wrapped:
                prefix = initial_prefix if first else continuation_prefix
                fragment: tuple[str, str] | tuple[str, str, Any]
                fragment = (style, prefix + item, handler) if handler else (style, prefix + item)
                fragments.append(fragment)
                first = False
        return fragments

    def _pt_prompt_mouse_handler(self, prompt_key: str):
        def handler(event):
            if event.event_type == MouseEventType.MOUSE_UP:
                if prompt_key in self._pt_expanded_prompts:
                    self._pt_expanded_prompts.remove(prompt_key)
                else:
                    self._pt_expanded_prompts.add(prompt_key)
                if self._pt_app:
                    self._pt_app.invalidate()
            return None

        return handler

    def _pt_hint_text(self):
        if self._pt_approval:
            suffix = ", a to always allow this command type" if self._pt_approval[2] else ""
            return [("class:error", f"  Command approval required: y once{suffix}, n reject")]
        if self._pt_change_approval:
            return [("class:trust", "  File changes waiting for approval: press y to apply, n to reject")]
        if self._pt_workspace_trusted is not True:
            return [("class:trust", "  Trust this workspace with y or exit with n")]
        compact = self._compact_work_snapshot()
        if compact and not compact.completed.is_set():
            frames = (".", "..", "...", " ..", "  .", "   ")
            frame = frames[int(time.monotonic() * 5) % len(frames)]
            return [(
                "class:command-working",
                f"  {frame:<3} Working · compacting conversation context · {format_duration(compact.duration_seconds)}",
            )]
        # Model and directory are already visible in the workspace card. The
        # bottom hint stays deliberately small, as in Codex, so it never
        # competes with the composer or steals transcript space.
        return [("class:footer", "  ? for shortcuts · Ctrl+T activity")]

    def _pt_mode_text(self):
        """Show the active file-change permission mode directly under the composer."""
        mode = str(getattr(self.config, "permission_mode", "approval") or "approval").lower()
        if mode == "auto":
            return [("class:success", "  mode: auto  ·  file changes apply automatically")]
        return [("class:trust", "  mode: approval  ·  file changes ask before applying")]

    def _pt_activity_fragments(self, view: TaskView) -> list[tuple[str, str]]:
        command_count = sum(operation.tool == "run_command" for operation in view.operations)
        context_update_count = len(view.context_updates)
        changed_files = [
            operation
            for operation in view.operations
            if operation.tool == "write_file" and operation.ok and (operation.result or {}).get("changed")
        ]
        summary_parts: list[str] = []
        if len(changed_files) == 1:
            summary_parts.append(operation_summary(changed_files[0]))
        elif len(changed_files) > 1:
            summary_parts.append(f"Updated {len(changed_files)} files")
        if command_count:
            summary_parts.append(f"Ran {command_count} command{'s' if command_count != 1 else ''}")
        if context_update_count:
            summary_parts.append(
                f"Context {context_update_count} update{'s' if context_update_count != 1 else ''}"
            )
        if view.subagents:
            completed = sum(item.get("status") == "completed" for item in view.subagents)
            summary_parts.append(f"{completed}/{len(view.subagents)} specialists")
        if view.review_verdict == "pass":
            summary_parts.append("审查通过")
        elif view.review_verdict == "reject":
            summary_parts.append("审查打回")
        if not summary_parts and not context_update_count:
            summary_parts.append(f"Completed {len(view.operations)} operation{'s' if len(view.operations) != 1 else ''}")
        label = " · ".join(summary_parts)
        duration = format_duration(view.duration_seconds)
        handler = self._pt_activity_mouse_handler(view.task_id)
        pending = view.status in {"queued", "running"} or any(operation.ok is None for operation in view.operations)
        failed_operations = sum(operation.ok is False for operation in view.operations)
        if view.status == "failed":
            icon, style, state_prefix = "✕", "class:error", "Task failed · "
        elif view.status == "cancelled":
            icon, style, state_prefix = "!", "class:warning", "Task cancelled · "
        elif pending:
            icon = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")[int(time.monotonic() * 8) % 10]
            style, state_prefix = "class:thinking", "Working · "
        elif failed_operations:
            icon, style, state_prefix = "!", "class:warning", f"{failed_operations} operation{'s' if failed_operations != 1 else ''} failed · "
        else:
            icon, style, state_prefix = "●", "class:success", ""
        label = state_prefix + label
        if not self._pt_activity_is_expanded(view):
            return [
                (style, f"  {icon} ", handler),
                ("class:activity.label", label, handler),
                ("class:activity", f" · {duration} · Ctrl+T for details\n", handler),
            ]
        fragments: list[tuple[str, str]] = [
            ("class:activity", "  ▾ ", handler),
            ("class:activity.label", label, handler),
            ("class:activity", f" · {duration}\n", handler),
        ]
        if view.context_updates:
            fragments.append(("class:activity.label", "    Context\n"))
            for update in view.context_updates:
                fragments.append(("class:activity", f"      {update}\n"))
        if view.subagents:
            fragments.append(("class:activity.label", "    Specialists\n"))
            for specialist in view.subagents:
                role = str(specialist.get("role", "specialist")).title()
                status = str(specialist.get("status", "running"))
                marker = "*" if status == "running" else "+" if status == "completed" else "!"
                style = "class:thinking" if status == "running" else "class:success" if status == "completed" else "class:error"
                duration = specialist.get("duration_ms")
                detail = f" · {format_duration(float(duration) / 1000)}" if isinstance(duration, (int, float)) and duration else ""
                fragments.append((style, f"      {marker} "))
                fragments.append(("class:activity.label", f"{role} · {status}{detail}\n"))
                summary = str(specialist.get("error") or specialist.get("summary") or "").replace("\n", " ").strip()
                if summary:
                    fragments.append(("class:activity", f"        {textwrap.shorten(summary, width=120, placeholder='...')}\n"))
        if view.review_verdict:
            review_style = "class:success" if view.review_verdict == "pass" else "class:error"
            review_label = "审查员已确认结果符合请求" if view.review_verdict == "pass" else "审查员未通过，主 Agent 正在修正"
            fragments.append((review_style, f"    {'✓' if view.review_verdict == 'pass' else '!'} {review_label}\n"))
            review_text = view.review_summary.replace("\n", " ").strip()
            review_text = re.sub(r"^VERDICT:\s*(PASS|REJECT)\s*", "", review_text, flags=re.IGNORECASE)
            if review_text:
                fragments.append(("class:activity", f"      {textwrap.shorten(review_text, width=120, placeholder='...')}\n"))
        for index, operation in enumerate(view.operations):
            marker = "✓" if operation.ok else "✕" if operation.ok is False else "●"
            style = "class:success" if operation.ok else "class:error" if operation.ok is False else "class:activity"
            operation_handler = self._pt_operation_mouse_handler(view.task_id, index)
            is_expanded = (view.task_id, index) in self._pt_expanded_operations
            fragments.append((style, f"    {marker} {'▾' if is_expanded else '▸'} ", operation_handler))
            fragments.append(("class:activity.label", operation_summary(operation), operation_handler))
            fragments.append(("class:activity", "\n", operation_handler))
            if is_expanded:
                fragments.extend(self._pt_operation_detail_fragments(view, index, operation))
        return fragments

    def _pt_latest_activity_view(self) -> TaskView | None:
        if self._screen_active:
            return self._screen_active[1]
        for kind, payload in reversed(self._pt_history):
            if kind == "activity" and isinstance(payload, TaskView):
                return payload
        return None

    def _pt_activity_is_expanded(self, view: TaskView) -> bool:
        # The legacy renderer owns ``_screen_activity_expanded``. The
        # Prompt Toolkit transcript keeps each task independent so expanding
        # one completed task never expands every historical activity block.
        return view.task_id in self._pt_expanded_activities

    def _pt_toggle_activity(self, view: TaskView) -> None:
        if view.task_id in self._pt_expanded_activities:
            self._pt_expanded_activities.remove(view.task_id)
        else:
            self._pt_expanded_activities.add(view.task_id)
        if self._pt_app:
            self._pt_app.invalidate()

    def _pt_view_for_id(self, task_id: str) -> TaskView | None:
        if self._screen_active and self._screen_active[1].task_id == task_id:
            return self._screen_active[1]
        for kind, payload in self._pt_history:
            if kind == "activity" and isinstance(payload, TaskView) and payload.task_id == task_id:
                return payload
        return None

    def _pt_activity_mouse_handler(self, task_id: str):
        def handler(event):
            if event.event_type == MouseEventType.MOUSE_UP:
                view = self._pt_view_for_id(task_id)
                if view:
                    self._pt_toggle_activity(view)
            return None

        return handler

    def _pt_operation_mouse_handler(self, task_id: str, index: int):
        def handler(event):
            if event.event_type == MouseEventType.MOUSE_UP:
                key = (task_id, index)
                if key in self._pt_expanded_operations:
                    self._pt_expanded_operations.remove(key)
                    self._pt_expanded_diffs.discard(key)
                    self._pt_expanded_outputs.discard(key)
                else:
                    self._pt_expanded_operations.add(key)
                if self._pt_app:
                    self._pt_app.invalidate()
            return None

        return handler

    def _pt_diff_mouse_handler(self, task_id: str, index: int):
        def handler(event):
            if event.event_type == MouseEventType.MOUSE_UP:
                key = (task_id, index)
                if key in self._pt_expanded_diffs:
                    self._pt_expanded_diffs.remove(key)
                else:
                    self._pt_expanded_diffs.add(key)
                if self._pt_app:
                    self._pt_app.invalidate()
            return None

        return handler

    def _pt_output_mouse_handler(self, task_id: str, index: int):
        def handler(event):
            if event.event_type == MouseEventType.MOUSE_UP:
                key = (task_id, index)
                if key in self._pt_expanded_outputs:
                    self._pt_expanded_outputs.remove(key)
                else:
                    self._pt_expanded_outputs.add(key)
                if self._pt_app:
                    self._pt_app.invalidate()
            return None

        return handler

    def _pt_operation_detail_fragments(
        self,
        view: TaskView,
        index: int,
        operation: ToolOperation,
    ) -> list[tuple[str, str]]:
        """Render the second disclosure level without printing raw payloads."""
        result = operation.result or {}
        key = (view.task_id, index)
        fragments: list[tuple[str, str]] = []
        path = str(result.get("path") or operation.arguments.get("path") or "")
        if operation.tool == "run_command":
            command = str(operation.arguments.get("command") or "")
            if command:
                fragments.extend([("class:activity", "      Command: "), ("class:code", command + "\n")])
            if result.get("returncode") is not None:
                fragments.append(("class:activity", f"      Exit code: {result['returncode']}\n"))
        elif path:
            fragments.extend([("class:activity", "      Path: "), ("class:code", path + "\n")])
        if operation.tool == "write_file" and result.get("changed"):
            added = int(result.get("added_lines") or 0)
            removed = int(result.get("removed_lines") or 0)
            fragments.append(("class:activity", f"      Changes: +{added} -{removed}\n"))
            diff_handler = self._pt_diff_mouse_handler(view.task_id, index)
            diff_open = key in self._pt_expanded_diffs
            fragments.extend([
                ("class:activity", f"      {'▾' if diff_open else '▸'} ", diff_handler),
                ("class:activity.label", "File diff", diff_handler),
                ("class:activity", "\n", diff_handler),
            ])
            if diff_open:
                fragments.extend(self._pt_diff_fragments(str(result.get("unified_diff") or ""), bool(result.get("diff_truncated"))))
        output = str(result.get("output") or "")
        if output:
            output_handler = self._pt_output_mouse_handler(view.task_id, index)
            output_open = key in self._pt_expanded_outputs
            line_count = len(output.splitlines()) or 1
            fragments.extend([
                ("class:activity", f"      {'▾' if output_open else '▸'} ", output_handler),
                ("class:activity.label", f"Command output · {line_count} lines", output_handler),
                ("class:activity", "\n", output_handler),
            ])
            if output_open:
                for line in _bounded_lines(output):
                    fragments.append(("class:code.block", "        " + line + "\n"))
        if operation.ok is False:
            fragments.append(("class:error", f"      {operation_error(operation)}\n"))
        return fragments

    @staticmethod
    def _pt_diff_fragments(diff_text: str, truncated: bool) -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = []
        old_line: int | None = None
        new_line: int | None = None
        for line in diff_text.splitlines():
            if line.startswith(("+++", "---", "@@")):
                if line.startswith("@@"):
                    match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                    if match:
                        old_line, new_line = int(match.group(1)), int(match.group(2))
                fragments.append(("class:diff.header", f"      {line}\n"))
            elif line.startswith("+"):
                number = f"{new_line:>4}" if new_line is not None else "    "
                fragments.append(("class:diff.add.number", f"      {number} + "))
                fragments.append(("class:diff.add", line[1:] + "\n"))
                if new_line is not None:
                    new_line += 1
            elif line.startswith("-"):
                number = f"{old_line:>4}" if old_line is not None else "    "
                fragments.append(("class:diff.remove.number", f"      {number} - "))
                fragments.append(("class:diff.remove", line[1:] + "\n"))
                if old_line is not None:
                    old_line += 1
            elif line.startswith("\\ No newline at end of file"):
                fragments.append(("class:diff.header", f"      {line}\n"))
            else:
                old_number = f"{old_line:>4}" if old_line is not None else "    "
                new_number = f"{new_line:>4}" if new_line is not None else "    "
                fragments.append(("class:diff.context.number", f"      {old_number}/{new_number}   "))
                fragments.append(("class:diff.context", line[1:] if line.startswith(" ") else line))
                fragments.append(("class:diff.context", "\n"))
                if old_line is not None:
                    old_line += 1
                if new_line is not None:
                    new_line += 1
        if truncated:
            fragments.append(("class:activity", "      ... diff truncated ...\n"))
        return fragments

    def _pt_completion_fragments(self, view: TaskView) -> list[tuple[str, str]]:
        # Match Codex's FinalMessageSeparator: successful short turns end in
        # a quiet divider. Runtime is only surfaced for concrete tool work
        # lasting at least a minute, rather than a noisy "Completed · 4.2s" on
        # every conversational response. Failure/cancellation still need an
        # explicit visible status.
        if view.status == "failed":
            label, style = "✕ Failed", "class:error"
        elif view.status == "cancelled":
            label, style = "! Cancelled", "class:warning"
        elif (view.operations or view.context_updates) and view.duration_seconds >= 60:
            label, style = f"Worked for {format_duration(view.duration_seconds)}", "class:completion"
        else:
            label, style = "", "class:separator"

        available = max(12, self._terminal_width() - 2)
        if not label:
            return [("class:separator", "\n  " + "─" * available + "\n")]
        prefix = "── "
        suffix = " " + "─" * max(1, available - len(prefix) - len(label))
        return [
            ("class:separator", "\n  " + prefix),
            (style, label),
            ("class:separator", suffix + "\n"),
        ]

    @staticmethod
    def _pt_command_feedback_style(text: str) -> tuple[str, str]:
        """Choose a quiet visual treatment for local slash-command feedback."""
        normalized = " ".join(str(text).strip().casefold().split())
        error_prefixes = (
            "usage:",
            "invalid ",
            "unknown command:",
            "unable ",
            "task failed:",
            "memory command failed:",
            "activity unavailable:",
            "details unavailable:",
            "model selection failed:",
            "conversation compaction failed:",
            "context compaction failed:",
            "conversation context is unavailable",
            "wait for the active task",
            "no active conversation context is available",
            "conversation context is unavailable",
            "no completed task context is available",
            "no resumable conversations are available",
        )
        if normalized.startswith(error_prefixes):
            return "class:error", "✕"

        success_prefixes = (
            "updated ",
            "model switched",
            "skills reloaded",
            "automatic skill selection enabled",
            "skill selected:",
            "memory saved:",
            "memory deleted:",
            "conversation compacted:",
            "conversation context already compact",
            "context compacted:",
            "context already compact",
            "new conversation selected",
            "opened conversation ",
            "conversation ",
            "activity expanded",
            "activity collapsed",
            "continue selected",
        )
        if normalized.startswith(success_prefixes):
            return "class:success", "✓"
        return "class:command.info", "•"

    def _pt_command_response_fragments(self, text: str) -> list[tuple[str, str]]:
        """Render slash-command feedback as compact status text, not a panel."""
        lines = str(text).splitlines() or [""]
        style, icon = self._pt_command_feedback_style(text)
        content_width = max(28, min(88, self._terminal_width() - 8))
        fragments: list[tuple[str, str]] = []
        first_line = True
        for line in lines:
            rendered = self._render_markdown_line(line, False)
            wrapped = textwrap.wrap(rendered, width=content_width, replace_whitespace=False) or [""]
            for part in wrapped:
                prefix = f"\n  {icon} " if first_line else "    "
                fragments.append((style, prefix))
                fragments.extend(self._pt_inline_markdown_fragments(part, default_style=style))
                fragments.append((style, "\n"))
                first_line = False

        # Keep each local command result distinct from the next turn without
        # reviving the old boxed command panel. A quiet rule also gives short
        # success lines, such as /compact, a clear visual endpoint.
        rule_width = max(18, min(52, self._terminal_width() - 8))
        fragments.append(("class:separator", "  " + "─" * rule_width + "\n"))
        return fragments

    @staticmethod
    def _pt_inline_markdown_fragments(text: str, default_style: str = "class:assistant") -> list[tuple[str, str]]:
        """Render the small Markdown subset useful in a live terminal reply."""
        source = text.replace("\\`", "`").replace("\\*", "*")
        token = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*|__([^_]+)__|\[([^\]]+)\]\([^)]*\)|(?<!\*)\*([^*]+)\*(?!\*)")
        fragments: list[tuple[str, str]] = []
        position = 0
        for match in token.finditer(source):
            if match.start() > position:
                fragments.append((default_style, source[position:match.start()]))
            if match.group(1) is not None:
                fragments.append(("class:code", match.group(1)))
            elif match.group(2) is not None or match.group(3) is not None:
                fragments.append(("class:emphasis", match.group(2) or match.group(3) or ""))
            elif match.group(4) is not None:
                fragments.append(("class:link", match.group(4)))
            else:
                fragments.append(("class:emphasis", match.group(5) or ""))
            position = match.end()
        if position < len(source) or not fragments:
            tail = source[position:]
            # A streaming chunk can end midway through a Markdown delimiter.
            # Hide only the unfinished marker; the next refresh will render
            # the complete construct once its closing delimiter arrives.
            if tail.count("`") % 2:
                tail = tail.replace("`", "")
            if tail.count("**") % 2:
                tail = tail.replace("**", "")
            if tail.count("__") % 2:
                tail = tail.replace("__", "")
            fragments.append((default_style, tail))
        return fragments

    @staticmethod
    def _markdown_table_cells(line: str) -> list[str] | None:
        """Parse a Markdown pipe-table row while preserving escaped pipes."""
        # Gateways occasionally HTML-escape indentation (``&#x20;`` or
        # ``&nbsp;``) before serialising model output.  Normalize only
        # whitespace before looking for structural pipe boundaries: decoding
        # every entity first would turn a literal ``&#124;`` inside a cell into
        # a separator.
        text = TerminalApp._normalize_table_whitespace(line).strip()
        if "|" not in text:
            return None
        # Some model gateways escape a leading Markdown pipe as ``\|``. It is
        # still a table boundary, not literal content, so normalize it first.
        if text.startswith("\\|"):
            text = text[1:]
        if text.endswith("\\|"):
            text = text[:-2] + "|"
        if text.startswith("|"):
            text = text[1:]
        if text.endswith("|"):
            text = text[:-1]
        cells: list[str] = []
        current: list[str] = []
        escaped = False
        for character in text:
            if escaped:
                current.append(character)
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "|":
                cells.append(html.unescape("".join(current)).replace("\xa0", " ").strip())
                current = []
            else:
                current.append(character)
        if escaped:
            current.append("\\")
        cells.append(html.unescape("".join(current)).replace("\xa0", " ").strip())
        return cells if len(cells) >= 2 and any(cells) else None

    @staticmethod
    def _normalize_table_whitespace(line: str) -> str:
        """Normalize entity-encoded whitespace without changing table pipes."""
        return re.sub(
            r"&(?:nbsp|#0*32|#x0*20);",
            " ",
            str(line),
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _is_table_fence_language(language: str) -> bool:
        """Whether a fenced block is presentation text that may contain a table."""
        name = language.strip().split(maxsplit=1)[0].casefold() if language.strip() else ""
        return name in {
            "",
            "ascii",
            "console",
            "markdown",
            "md",
            "output",
            "plain",
            "plaintext",
            "text",
        }

    @staticmethod
    def _is_ascii_table_rule(line: str) -> bool:
        """Recognize the box-drawing rule emitted by many Markdown models."""
        text = TerminalApp._normalize_table_whitespace(line).strip()
        if not text.startswith("+") or text.count("+") < 2:
            return False
        return bool(re.fullmatch(r"\+[+\-=:\s]+\+", text))

    @classmethod
    def _ascii_table_block(cls, lines: list[str], start: int) -> tuple[list[list[str]], int] | None:
        """Extract rows from a ``+-----+`` table and return its end index."""
        if start >= len(lines) or not cls._is_ascii_table_rule(lines[start]):
            return None
        rows: list[list[str]] = []
        index = start + 1
        # The opening rule itself is meaningful.  In a streamed response the
        # header can arrive before its following separator; render the known
        # row immediately rather than flashing the raw ASCII frame.
        saw_rule = True
        while index < len(lines):
            line = lines[index]
            if cls._is_ascii_table_rule(line):
                saw_rule = True
                index += 1
                continue
            cells = cls._markdown_table_cells(line)
            if cells is None:
                break
            rows.append(cells)
            index += 1
        # Treat a header-only or still-streaming table as a table as soon as
        # it has one row.  Showing a raw ``+---+`` frame while the model is
        # still producing the body is visually worse than rendering the known
        # header in Codex's borderless table form.
        if not saw_rule or not rows:
            return None
        column_count = len(rows[0])
        if any(len(row) != column_count for row in rows):
            return None
        return rows, index

    @classmethod
    def _is_markdown_table_separator(cls, line: str, column_count: int) -> bool:
        cells = cls._markdown_table_cells(line)
        if not cells or len(cells) != column_count:
            return False
        return all(re.fullmatch(r":?-{3,}:?", cell) is not None for cell in cells)

    @classmethod
    def _markdown_table_alignments(cls, line: str, column_count: int) -> list[str]:
        """Read GFM table alignment markers from the delimiter row."""
        cells = cls._markdown_table_cells(line) or []
        if len(cells) != column_count:
            return ["left"] * column_count
        alignments: list[str] = []
        for cell in cells:
            if cell.startswith(":") and cell.endswith(":"):
                alignments.append("center")
            elif cell.endswith(":"):
                alignments.append("right")
            else:
                alignments.append("left")
        return alignments

    def _pt_table_block_at(
        self,
        lines: list[str],
        start: int,
    ) -> tuple[list[list[str]], list[str], int] | None:
        """Return one complete or streaming table block beginning at ``start``.

        Keep table recognition independent from Markdown fence state.  Models
        commonly wrap diagnostic tables in ``text`` or ``markdown`` fences;
        the table itself remains presentation data and should use the same
        Codex-style borderless renderer as an unfenced table.
        """
        ascii_table = self._ascii_table_block(lines, start)
        if ascii_table is not None:
            rows, end = ascii_table
            return rows, ["left"] * len(rows[0]), end

        cells = self._markdown_table_cells(lines[start])
        if not cells or start + 1 >= len(lines):
            return None
        if not self._is_markdown_table_separator(lines[start + 1], len(cells)):
            return None

        rows = [cells]
        alignments = self._markdown_table_alignments(lines[start + 1], len(cells))
        index = start + 2
        while index < len(lines):
            next_cells = self._markdown_table_cells(lines[index])
            if not next_cells or len(next_cells) != len(cells):
                break
            rows.append(next_cells)
            index += 1
        return rows, alignments, index

    def _pt_table_fragments(
        self,
        rows: list[list[str]],
        *,
        first_prefix: str,
        continuation_prefix: str,
        alignments: list[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Render tables with Codex's borderless, width-aware column layout."""
        column_count = len(rows[0])
        alignments = (alignments or ["left"] * column_count)[:column_count]
        alignments += ["left"] * max(0, column_count - len(alignments))
        normalized = [row[:column_count] + [""] * max(0, column_count - len(row)) for row in rows]
        cleaned = [
            [self._render_markdown_line(cell, False).strip() for cell in row]
            for row in normalized
        ]
        # Codex's markdown renderer allocates widths in terminal display cells,
        # not Python character counts. This keeps Chinese headers and values
        # aligned and avoids the raw +---+ grid shown in the reported output.
        table_width = max(12, self._terminal_width() - self._display_width(continuation_prefix))
        reserved_width = column_count * 2 + max(0, column_count - 1) * 2
        available = table_width - reserved_width
        if available < column_count * 3:
            return self._pt_table_records_fragments(
                cleaned,
                first_prefix=first_prefix,
                continuation_prefix=continuation_prefix,
            )

        natural_widths = [
            max(3, max(self._display_width(row[index]) for row in cleaned))
            for index in range(column_count)
        ]
        widths = self._fit_table_widths(natural_widths, available)
        if widths is None:
            return self._pt_table_records_fragments(
                cleaned,
                first_prefix=first_prefix,
                continuation_prefix=continuation_prefix,
            )

        fragments: list[tuple[str, str]] = []
        first_line = True

        def prefix() -> str:
            nonlocal first_line
            result = first_prefix if first_line else continuation_prefix
            first_line = False
            return result

        def table_row(cells: list[str], style: str) -> None:
            wrapped = [self._wrap_display_text(value, width) for value, width in zip(cells, widths)]
            height = max(len(cell_lines) for cell_lines in wrapped)
            for row_index in range(height):
                fragments.append((style, prefix()))
                for column, (cell_lines, width) in enumerate(zip(wrapped, widths)):
                    value = cell_lines[row_index] if row_index < len(cell_lines) else ""
                    remaining = max(0, width - self._display_width(value))
                    if alignments[column] == "right":
                        left_padding, right_padding = remaining, 0
                    elif alignments[column] == "center":
                        left_padding, right_padding = remaining // 2, remaining - remaining // 2
                    else:
                        left_padding, right_padding = 0, remaining
                    fragments.append((style, " " + " " * left_padding + value + " " * right_padding))
                    if column + 1 < len(widths):
                        fragments.append(("class:table.cell", "   "))
                fragments.append(("class:table.cell", "\n"))

        def rule(character: str) -> None:
            fragments.append(("class:table.border", prefix() + " "))
            segments = "  ".join(character * (width + 2) for width in widths)
            fragments.append(("class:table.border", segments + "\n"))

        table_row(cleaned[0], "class:table.header")
        rule("━")
        for row_index, values in enumerate(cleaned[1:]):
            table_row(values, "class:table.cell")
            if row_index + 1 < len(cleaned[1:]):
                rule("─")
        return fragments

    @staticmethod
    def _display_width(value: str) -> int:
        """Return terminal-cell width with CJK and combining marks handled."""
        if get_cwidth is not None:
            return sum(get_cwidth(character) for character in str(value))
        width = 0
        for character in str(value):
            if unicodedata.combining(character):
                continue
            if unicodedata.east_asian_width(character) in {"F", "W"}:
                width += 2
            elif unicodedata.category(character).startswith("C"):
                continue
            else:
                width += 1
        return width

    @classmethod
    def _take_display_width(cls, value: str, width: int) -> tuple[str, str]:
        """Split text on a terminal-cell boundary without splitting CJK glyphs."""
        used = 0
        for index, character in enumerate(value):
            character_width = cls._display_width(character)
            if used and used + character_width > width:
                return value[:index], value[index:]
            if not used and character_width > width:
                return character, value[index + 1:]
            used += character_width
        return value, ""

    @classmethod
    def _wrap_display_text(cls, value: str, width: int) -> list[str]:
        """Wrap text by display cells, preferring whitespace break points."""
        remaining = str(value).strip()
        if not remaining:
            return [""]
        result: list[str] = []
        width = max(1, width)
        while cls._display_width(remaining) > width:
            candidate, remainder = cls._take_display_width(remaining, width)
            split_at = candidate.rstrip().rfind(" ")
            if split_at > 0:
                result.append(candidate[:split_at].rstrip())
                remaining = remaining[split_at:].lstrip()
            else:
                result.append(candidate.rstrip())
                remaining = remainder.lstrip()
        result.append(remaining)
        return result

    @staticmethod
    def _fit_table_widths(natural_widths: list[int], available: int) -> list[int] | None:
        """Shrink table columns proportionally, with Codex's three-cell floor."""
        minimum = 3
        if available < len(natural_widths) * minimum:
            return None
        widths = natural_widths[:]
        while sum(widths) > available:
            candidates = [index for index, width in enumerate(widths) if width > minimum]
            if not candidates:
                return None
            widest = max(candidates, key=lambda index: widths[index])
            widths[widest] -= 1
        return widths

    def _pt_table_records_fragments(
        self,
        rows: list[list[str]],
        *,
        first_prefix: str,
        continuation_prefix: str,
    ) -> list[tuple[str, str]]:
        """Use Codex's record fallback when an aligned table cannot fit."""
        headers = rows[0]
        fragments: list[tuple[str, str]] = []
        first_line = True

        def prefix() -> str:
            nonlocal first_line
            result = first_prefix if first_line else continuation_prefix
            first_line = False
            return result

        label_width = max(self._display_width(header) for header in headers)
        value_width = max(12, self._terminal_width() - self._display_width(continuation_prefix) - label_width - 5)
        for row_index, row in enumerate(rows[1:]):
            for header, value in zip(headers, row):
                padding = " " * max(0, label_width - self._display_width(header))
                values = self._wrap_display_text(value, value_width)
                fragments.append(("class:table.header", prefix() + " " + header + padding + "  "))
                fragments.append(("class:table.cell", values[0] + "\n"))
                continuation = continuation_prefix + " " * (label_width + 3)
                for line in values[1:]:
                    fragments.append(("class:table.cell", continuation + line + "\n"))
            if row_index + 1 < len(rows[1:]):
                rule_width = max(8, min(self._terminal_width() - self._display_width(continuation_prefix), 72))
                fragments.append(("class:table.border", prefix() + " " + "─" * rule_width + "\n"))
        return fragments

    def _pt_markdown_fragments(
        self,
        text: str,
        *,
        first_prefix: str,
        continuation_prefix: str,
    ) -> list[tuple[str, str]]:
        """Render assistant Markdown with visual hierarchy without raw markers."""
        lines = str(text).splitlines() or [""]
        fragments: list[tuple[str, str]] = []
        code_fence_language: str | None = None
        first_output = True
        line_index = 0
        while line_index < len(lines):
            raw_line = lines[line_index]
            stripped = self._normalize_table_whitespace(raw_line).strip()
            if stripped.startswith("```"):
                code_fence_language = None if code_fence_language is not None else stripped[3:].strip()
                line_index += 1
                continue
            prefix = first_prefix if first_output else continuation_prefix
            if code_fence_language is not None:
                table_block = (
                    self._pt_table_block_at(lines, line_index)
                    if self._is_table_fence_language(code_fence_language)
                    else None
                )
                if table_block is not None:
                    table_rows, alignments, line_index = table_block
                    fragments.extend(self._pt_table_fragments(
                        table_rows,
                        first_prefix=prefix,
                        continuation_prefix=continuation_prefix,
                        alignments=alignments,
                    ))
                    first_output = False
                    continue
                fragments.append(("class:code.block", prefix + "  " + raw_line + "\n"))
            else:
                table_block = self._pt_table_block_at(lines, line_index)
                if table_block is not None:
                    table_rows, alignments, line_index = table_block
                    fragments.extend(self._pt_table_fragments(
                        table_rows,
                        first_prefix=prefix,
                        continuation_prefix=continuation_prefix,
                        alignments=alignments,
                    ))
                    first_output = False
                    continue
                heading = re.match(r"^\s{0,3}#{1,6}\s+(.+)$", raw_line)
                bullet = re.match(r"^\s*[-*+]\s+(.+)$", raw_line)
                if heading:
                    parts = self._pt_markdown_wrap_parts(heading.group(1).strip(), continuation_prefix)
                    for index, part in enumerate(parts):
                        line_prefix = prefix if index == 0 else continuation_prefix
                        fragments.append(("class:heading", line_prefix + part + "\n"))
                elif bullet:
                    marker = "" if first_output and first_prefix == "" else "• "
                    parts = self._pt_markdown_wrap_parts(bullet.group(1), continuation_prefix + "  ")
                    for index, part in enumerate(parts):
                        line_prefix = (prefix + marker) if index == 0 else continuation_prefix + "  "
                        if marker and index == 0:
                            fragments.append(("class:assistant.bullet", line_prefix))
                        else:
                            fragments.append(("class:assistant", line_prefix))
                        fragments.extend(self._pt_inline_markdown_fragments(part))
                        fragments.append(("class:assistant", "\n"))
                else:
                    content = bullet.group(1) if bullet else raw_line
                    parts = self._pt_markdown_wrap_parts(content, continuation_prefix)
                    for index, part in enumerate(parts):
                        line_prefix = prefix if index == 0 else continuation_prefix
                        fragments.append(("class:assistant", line_prefix))
                        fragments.extend(self._pt_inline_markdown_fragments(part))
                        fragments.append(("class:assistant", "\n"))
            first_output = False
            line_index += 1
        return fragments

    def _pt_markdown_wrap_parts(self, text: str, continuation_prefix: str) -> list[str]:
        """Wrap plain Markdown rows before display, retaining rich inline rows.

        Codex re-renders its Markdown cell at the current terminal width. Our
        renderer keeps inline syntax as separate Prompt Toolkit fragments, so
        breaking a delimiter across rows would discard styling mid-stream. Wrap
        unadorned prose explicitly for a stable two-column gutter and leave
        inline-rich content to Prompt Toolkit's safe visual wrapping.
        """
        if re.search(r"[`*_\[\]]", text):
            return [text]
        width = max(16, self._terminal_width() - len(continuation_prefix))
        return textwrap.wrap(
            text,
            width=width,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]

    def _pt_render_text(self, text: str, prefix: str = "") -> str:
        # Keep the visual language compact while removing raw Markdown markers.
        lines = str(text).replace("\\`", "`").splitlines() or [""]
        rendered: list[str] = []
        in_code = False
        for line in lines:
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            clean = self._render_markdown_line(line, in_code)
            rendered.append(prefix + clean if not rendered else "  " + clean)
        return "\n".join(rendered)

    def _pt_header_text(self):
        # Keep the legacy header accessor in sync with the Prompt Toolkit
        # transcript; integrations that render it separately should receive
        # the same Codex-style workspace card.
        return self._pt_workspace_card_fragments()

    def _pt_footer_text(self):
        if self._pt_approval:
            return [("class:error", " Command approval required · press y to approve, n to reject")]
        compact = self._compact_work_snapshot()
        if compact and not compact.completed.is_set():
            frames = (".", "..", "...", " ..", "  .", "   ")
            frame = frames[int(time.monotonic() * 5) % len(frames)]
            return [(
                "class:command-working",
                f" {frame:<3} Working · compacting conversation context · {format_duration(compact.duration_seconds)}",
            )]
        status = self._screen_status or "Ready"
        if status.casefold() in {"ready", "ready · /help · /model · /skills"}:
            return [("class:footer", " ? for shortcuts · Ctrl+T activity")]
        return [("class:footer", f" {status}   ·   ? for shortcuts")]

    def _run_line_interface(self) -> None:
        """Keep pipes and basic terminals usable without terminal control sequences."""
        if sys.stdin.isatty() and not self._confirm_workspace_trust():
            return
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

    def _confirm_workspace_trust(self) -> bool:
        """Ask for explicit trust unless this workspace was already approved."""
        if self._pt_workspace_trusted is True:
            return True
        self._write("LimoCode", "bold")
        print(f"  workspace  {self.config.workspace}")
        print(f"  model      {self.config.model}  ({'demo' if self.demo else 'live'})")
        while True:
            try:
                answer = input("  Trust this workspace? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            if answer in {"y", "yes"}:
                saved = self._trust_current_workspace()
                if saved:
                    self._write("Workspace trust saved for future sessions.", "green")
                    print()
                    return True
                self._write(
                    self.trust_store.last_error or "Unable to save workspace trust.",
                    "red",
                )
                self._write("Trust was not recorded; no task was run.", "yellow")
                return False
            if answer in {"", "n", "no"}:
                self._write("Workspace was not trusted; no task was run.", "yellow")
                return False
            self._write("Enter y to trust this workspace or n to exit.", "yellow")

    def _run_fullscreen(self) -> None:
        """Render task state in the alternate screen while polling keyboard and events."""
        self._render_screen()
        while True:
            self._pump_screen_task()
            self._poll_compaction()
            try:
                key = self._read_key()
            except KeyboardInterrupt:
                key = "\x03"
            if key is not None and not self._handle_screen_key(key):
                return
            self._render_screen()
            time.sleep(0.03)

    def _read_key(self) -> str | None:
        try:
            import msvcrt
        except ImportError:
            return None
        if not msvcrt.kbhit():
            return None
        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            return {"H": "UP", "P": "DOWN", "I": "PAGEUP", "Q": "PAGEDOWN"}.get(msvcrt.getwch(), "")
        return key

    def _handle_screen_key(self, key: str) -> bool:
        if key == "\x03":
            active = self._screen_active
            if active and active[0].status not in {"completed", "failed", "cancelled"}:
                if time.monotonic() - self._screen_last_ctrl_c < 1.2:
                    return False
                self._screen_last_ctrl_c = time.monotonic()
                self.service.cancel_task(active[0].id)
                self._screen_status = "Cancelling task... Press Ctrl+C again to exit"
                return True
            if time.monotonic() - self._screen_last_ctrl_c < 1.2:
                return False
            self._screen_last_ctrl_c = time.monotonic()
            self._screen_status = "Press Ctrl+C again to exit"
            return True
        if key == "\t":
            self._screen_focus_activity = bool(self._screen_active or self._last_task_id)
            return True
        if key == "UP":
            if self._screen_focus_activity and self._screen_activity_expanded:
                self._screen_selected_operation = max(-1, self._screen_selected_operation - 1)
                return True
            self._screen_scroll += 1
            return True
        if key == "DOWN":
            if self._screen_focus_activity and self._screen_activity_expanded:
                view = self._screen_current_view()
                if view and view.operations:
                    self._screen_selected_operation = min(len(view.operations) - 1, self._screen_selected_operation + 1)
                return True
            self._screen_scroll = max(0, self._screen_scroll - 1)
            return True
        if key == "PAGEUP":
            self._screen_scroll += max(4, self._screen_height() // 2)
            return True
        if key == "PAGEDOWN":
            self._screen_scroll = max(0, self._screen_scroll - max(4, self._screen_height() // 2))
            return True
        if key in {"\r", "\n"}:
            if self._screen_focus_activity and not self._screen_input:
                if self._screen_activity_expanded:
                    if self._screen_selected_operation < 0:
                        self._screen_activity_expanded = False
                    elif self._screen_selected_operation in self._screen_expanded_operations:
                        self._screen_expanded_operations.remove(self._screen_selected_operation)
                    else:
                        self._screen_expanded_operations.add(self._screen_selected_operation)
                else:
                    self._screen_activity_expanded = True
                return True
            line = self._screen_input.strip()
            self._screen_input = ""
            if not line:
                return True
            return self._submit_screen_line(line)
        if key == "\b":
            self._screen_input = self._screen_input[:-1]
            self._screen_focus_activity = False
            return True
        if key and key >= " ":
            self._screen_input += key
            self._screen_focus_activity = False
        return True

    def _submit_screen_line(self, line: str) -> bool:
        try:
            command, arguments = parse_command(line)
        except ValueError as exc:
            self._screen_status = f"Invalid command: {exc}"
            self._append_screen_text(self._screen_status)
            return True
        if command == "task":
            resume_from = self._active_conversation_id()
            if resume_from:
                self._start_screen_task(arguments[0], resume_from=resume_from)
            else:
                self._start_screen_task(arguments[0])
            return True
        if command in {"quit", "exit"}:
            return False
        if command == "activity":
            try:
                view = self._detail_view(arguments)
            except ValueError as exc:
                self._screen_status = str(exc)
                self._append_screen_text(f"Activity unavailable: {exc}")
                return True
            if self._pt_app:
                self._pt_toggle_activity(view)
                self._screen_status = "Activity expanded" if self._pt_activity_is_expanded(view) else "Activity collapsed"
            else:
                self._screen_activity_expanded = not self._screen_activity_expanded
                self._screen_status = "Activity expanded" if self._screen_activity_expanded else "Activity collapsed"
            return True
        if command == "inspect":
            # The line-mode inspector owns the terminal and cannot coexist
            # with the Prompt Toolkit event loop.  Render the same bounded
            # information inline instead of silently doing nothing.
            return self._screen_show_details(arguments, inspect=True)
        if command == "prompt":
            self._screen_prompt_details(arguments)
            return True
        if self._screen_active and (
            command in {
                "config", "model", "models", "skills", "skill", "compact", "new", "continue",
                "history", "resume", "open", "details", "detail", "inspect", "activity",
                "mode", "changes", "undo",
            }
            or (command == "memory" and (not arguments or arguments[0].lower() != "status"))
        ):
            self._screen_status = "Wait for the active task to finish or press Ctrl+C to cancel it."
            self._append_screen_text(self._screen_status)
            return True
        if command == "clear":
            self._screen_lines.clear()
            self._pt_history.clear()
            self._pt_expanded_activities.clear()
            self._pt_expanded_operations.clear()
            self._pt_expanded_diffs.clear()
            self._pt_expanded_outputs.clear()
            self._pt_expanded_prompts.clear()
            self._screen_status = "Ready"
            return True
        if command == "new":
            return self._screen_new(arguments)
        if command == "help":
            self._append_screen_text(HELP)
            return True
        if command == "config" and not arguments:
            self._append_screen_text(
                "Configuration:\n"
                f"  workspace: {self.config.workspace}\n  model: {self.config.model}\n"
                f"  API timeout: {self.config.model_timeout}s\n  max turns: {self.config.max_turns}"
            )
            return True
        if command == "mode":
            if len(arguments) > 1:
                self._screen_status = "Usage: /mode [approval|auto]"
            else:
                try:
                    payload = self.service.set_permission_mode(arguments[0]) if arguments else self.service.permission_status()
                    self.config = self.service.config
                    self._screen_status = f"File changes mode: {payload['mode']}"
                except ValueError as exc:
                    self._screen_status = str(exc)
            self._append_screen_text(self._screen_status)
            return True
        if command == "changes":
            if len(arguments) > 1:
                self._screen_status = "Usage: /changes [task-id-prefix]"
            else:
                task_id = None
                if arguments:
                    record = self._find_task(arguments[0])
                    task_id = record.id if record else None
                changesets = self.service.list_changesets(task_id)
                self._screen_status = "No tracked Agent file changes." if not changesets else "\n".join(
                    f"{item['id'][:8]}  {item['status']}  " + ", ".join(str(file.get('path', 'file')) for file in item.get('files', []))
                    for item in changesets
                )
            self._append_screen_text(self._screen_status)
            return True
        if command == "undo":
            if len(arguments) != 1:
                self._screen_status = "Usage: /undo <changeset-id-prefix>"
            else:
                matches = [item for item in self.service.list_changesets() if item["id"].startswith(arguments[0])]
                result = self.service.undo_changeset(matches[0]["id"]) if len(matches) == 1 else None
                self._screen_status = f"Undo: {result['status']}" if result else "ChangeSet not found or ambiguous."
            self._append_screen_text(self._screen_status)
            return True
        if command == "config":
            if len(arguments) < 2:
                self._screen_status = "Usage: /config <name> <value>"
                self._append_screen_text(self._screen_status)
                return True
            if arguments[0].lower().replace("_", "-") == "model":
                self._screen_switch_model(" ".join(arguments[1:]))
                self._append_screen_text(self._screen_status)
                return True
            try:
                self.config, self.demo = apply_config_change(self.config, self.demo, arguments[0], " ".join(arguments[1:]))
                self._rebuild_service()
            except ValueError as exc:
                self._screen_status = str(exc)
                self._append_screen_text(self._screen_status)
                return True
            self._screen_status = f"Updated {arguments[0]}"
            self._append_screen_text(self._screen_status)
            return True
        if command == "model" and not arguments:
            current = self.models.current()
            self._append_screen_text(f"Model: {current.name}\nProvider: {current.provider}\nContext: {current.context_window} tokens")
            return True
        if command == "model":
            self._screen_switch_model(" ".join(arguments))
            self._append_screen_text(self._screen_status)
            return True
        if command == "models":
            self._append_screen_text("Available models:\n" + "\n".join(f"  {item.name}" for item in self.models.available()))
            return True
        if command == "skills":
            skills = self.service.skill_manager.metadata()
            if skills:
                self._append_screen_text("Available skills:\n" + "\n".join(f"  {item.name}: {item.description}" for item in skills))
            else:
                self._append_screen_text("No skills are available in this workspace.")
            return True
        if command == "skill":
            if len(arguments) != 1:
                self._screen_status = "Usage: /skill <name|auto|reload>"
                self._append_screen_text(self._screen_status)
                return True
            name = arguments[0]
            try:
                if name == "reload":
                    self.service.reload_skills()
                    self._screen_status = "Skills reloaded"
                elif name == "auto":
                    self._selected_skills = ()
                    self.service.set_selected_skills(())
                    self._screen_status = "Automatic skill selection enabled"
                else:
                    self.service.set_selected_skills((name,))
                    self._selected_skills = (name,)
                    self._screen_status = f"Skill selected: {name}"
            except ValueError as exc:
                self._screen_status = str(exc)
            self._append_screen_text(self._screen_status)
            return True
        if command == "history":
            return self._screen_history()
        if command == "resume":
            return self._screen_resume(arguments)
        if command == "memory":
            return self._screen_memory(arguments)
        if command == "compact":
            return self._screen_compact(arguments)
        if command == "continue":
            return self._screen_continue(arguments)
        if command == "open":
            return self._screen_open(arguments)
        if command in {"details", "detail"}:
            return self._screen_show_details(arguments)
        if command == "":
            self._screen_status = "Enter a task or a slash command. Type /help for commands."
            return True
        self._screen_status = f"Unknown command: /{command}. Type /help."
        self._append_screen_text(self._screen_status)
        return True

    def _memory_status_text(self) -> str:
        """Query the service defensively so status remains safe during a task."""
        status_method = getattr(self.service, "memory_status", None)
        if not callable(status_method):
            return "Memory status is unavailable in this service version."
        try:
            return format_memory_status(status_method(self._last_task_id))
        except Exception as exc:  # The status command must never interrupt the TUI.
            return f"Memory status is unavailable: {_short_value(exc, 180)}"

    def _screen_memory(self, arguments: list[str]) -> bool:
        """Run project-memory commands and keep their feedback in the TUI transcript."""
        try:
            if not arguments:
                items = self.service.list_memories()
                if not items:
                    self._append_screen_text("No project memory is stored yet.")
                else:
                    self._append_screen_text("Project memory:\n" + "\n".join(
                        f"  {item.id:<4} {textwrap.shorten(item.content, width=76, placeholder='...')}" for item in items
                    ))
                return True
            action = arguments[0].lower()
            if action == "status" and len(arguments) == 1:
                self._append_screen_text(self._memory_status_text())
                self._screen_status = "Memory status shown"
                return True
            if action == "add" and len(arguments) > 1:
                item = self.service.add_memory(" ".join(arguments[1:]))
                self._append_screen_text(f"Memory saved: {item.id}")
                return True
            if action == "search" and len(arguments) > 1:
                items = self.service.search_memories(" ".join(arguments[1:]))
                self._append_screen_text("Memory search:\n" + ("\n".join(
                    f"  {item.id:<4} {textwrap.shorten(item.content, width=76, placeholder='...')}" for item in items
                ) or "  No matching memory."))
                return True
            if action == "delete" and len(arguments) == 2:
                item_id = int(arguments[1])
                if not self.service.delete_memory(item_id):
                    raise ValueError("memory does not exist")
                self._append_screen_text(f"Memory deleted: {item_id}")
                return True
            raise ValueError("Usage: /memory [status|add <content>|search <query>|delete <id>]")
        except (ValueError, OSError) as exc:
            self._screen_status = str(exc)
            self._append_screen_text(f"Memory command failed: {exc}")
            return True

    def _screen_compact(self, arguments: list[str]) -> bool:
        # Resolve a just-finished worker before deciding whether another
        # compaction can begin; otherwise a quick second /compact could hide
        # the first completion line between polling ticks.
        self._poll_compaction()
        if arguments:
            self._screen_status = "Usage: /compact"
            self._append_screen_text(self._screen_status)
            return True
        if self._compact_work_running():
            self._screen_status = "Conversation compaction is already working"
            self._append_screen_text(self._screen_status)
            return True
        conversation_id = self._active_conversation_key()
        if not conversation_id:
            self._screen_status = "No active conversation context is available to compact. Use /resume to select one."
            self._append_screen_text(self._screen_status)
            return True
        if not self._start_compaction(conversation_id):
            self._screen_status = "Conversation compaction is already working"
            self._append_screen_text(self._screen_status)
            return True
        # A tiny context can finish before the next poll tick.  Publish it
        # immediately when possible; larger histories remain visibly Working
        # and are finalized by the Prompt Toolkit/full-screen poll loop.
        self._poll_compaction()
        if not self._pt_app:
            work = self._compact_work_snapshot()
            if work and not work.completed.is_set():
                # The plain line interface has no redraw loop, so wait there
                # rather than returning a prompt that hides the final result.
                work.completed.wait()
                self._poll_compaction()
        return True

    def _screen_continue(self, arguments: list[str]) -> bool:
        if not arguments:
            self._screen_status = "Usage: /continue <instruction>"
            self._append_screen_text(self._screen_status)
            return True
        parent = self._continuation_source()
        if not parent:
            self._screen_status = (
                "No completed task context is available. Run a task first, or use /resume <conversation-id> to select one."
            )
            self._append_screen_text(self._screen_status)
            return True
        try:
            self._start_screen_task(" ".join(arguments), resume_from=parent.id)
        except ValueError as exc:
            self._screen_status = str(exc)
            self._append_screen_text(f"Unable to continue task: {exc}")
        return True

    def _screen_new(self, arguments: list[str]) -> bool:
        if arguments:
            self._screen_status = "Usage: /new"
            self._append_screen_text(self._screen_status)
            return True
        self._active_conversation_task_id = None
        self._screen_status = "New conversation selected"
        self._append_screen_text("New conversation selected. Your next message will start without prior task context.")
        return True

    def _list_conversations(
        self,
        *,
        limit: int | None = None,
        resumable_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Read conversation summaries from the service without exposing tasks as rows."""
        list_conversations = getattr(self.service, "list_conversations", None)
        if not callable(list_conversations):
            return []
        try:
            records = list_conversations(limit=limit, resumable_only=resumable_only)
        except TypeError:
            # Keep the TUI usable with a persisted service from an older local
            # installation while new conversation metadata is being migrated.
            records = list_conversations(limit=limit)
            if resumable_only:
                records = [
                    item for item in records
                    if isinstance(item, dict) and item.get("continuation_task_id")
                ]
        return [dict(item) for item in records if isinstance(item, dict)]

    def _find_conversation(self, prefix: str, *, resumable_only: bool = False) -> dict[str, Any]:
        normalized = prefix.strip().lower()
        if not normalized:
            raise ValueError("conversation id must not be empty")
        matches = [
            item
            for item in self._list_conversations(resumable_only=resumable_only)
            if str(item.get("id") or item.get("conversation_id") or "").lower().startswith(normalized)
        ]
        if len(matches) != 1:
            raise ValueError("conversation id prefix must match exactly one conversation")
        return matches[0]

    @staticmethod
    def _conversation_id(summary: dict[str, Any]) -> str:
        return str(summary.get("id") or summary.get("conversation_id") or "")

    @staticmethod
    def _conversation_task_text(
        summary: dict[str, Any],
        field: str,
        *,
        fallback: str = "Untitled conversation",
    ) -> str:
        """Read a task title from a service snapshot or a legacy string."""
        value = summary.get(field)
        if isinstance(value, dict):
            value = value.get("task")
        text = str(value or "").strip()
        return text or fallback

    @staticmethod
    def _conversation_task_count(summary: dict[str, Any]) -> int:
        try:
            return max(1, int(summary.get("task_count", 1) or 1))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _conversation_title(summary: dict[str, Any], *, fallback: str = "Untitled conversation") -> str:
        """Use the persisted conversation title, with legacy task text as a fallback."""
        title = str(summary.get("title") or "").strip()
        return title or fallback

    def _conversation_line(self, summary: dict[str, Any]) -> str:
        conversation_id = self._conversation_id(summary)
        task_count = self._conversation_task_count(summary)
        latest_snapshot = summary.get("latest_task")
        status = str(
            summary.get("latest_status")
            or (latest_snapshot.get("status") if isinstance(latest_snapshot, dict) else "saved")
        )
        root_task = self._conversation_task_text(summary, "root_task")
        title = self._conversation_title(summary, fallback=root_task)
        return (
            f"  {conversation_id[:8]}  {task_count} task{'s' if task_count != 1 else ''}"
            f"  {status:<10}  {task_summary(title, 54)}"
        )

    def _conversation_records(self, summary: dict[str, Any]) -> list[TaskRecord]:
        """Load all saved task turns for one conversation in chronological order."""
        conversation_id = self._conversation_id(summary)
        resolver = getattr(self.service, "conversation_tasks", None)
        if callable(resolver) and conversation_id:
            records = resolver(conversation_id)
            if isinstance(records, list) and all(isinstance(item, TaskRecord) for item in records):
                return records
        # The summary itself makes a useful compatibility source if a caller
        # only exposes task snapshots. Normal service operation uses the
        # branch above so tool/event detail is available for each task.
        records: list[TaskRecord] = []
        for item in summary.get("tasks", []):
            if not isinstance(item, dict):
                continue
            task_id = item.get("id")
            if isinstance(task_id, str):
                record = self.service.get_task(task_id)
                if record:
                    records.append(record)
        return records

    def _continuation_record(self, summary: dict[str, Any]) -> TaskRecord:
        task_id = summary.get("continuation_task_id")
        record = self.service.get_continuable_task(task_id) if isinstance(task_id, str) else None
        if not record:
            raise ValueError("the selected conversation has no resumable task context")
        return record

    def _screen_history(self) -> bool:
        conversations = self._list_conversations(limit=12)
        if not conversations:
            self._screen_status = "No conversation history is available yet."
            self._append_screen_text(self._screen_status)
            return True
        self._screen_status = "Conversation history shown"
        self._append_screen_text(
            "Conversation history:\n"
            + "\n".join(self._conversation_line(item) for item in conversations)
            + "\nUse /resume <conversation-id> to continue one."
        )
        return True

    def _screen_resume(self, arguments: list[str]) -> bool:
        """Provide a usable fallback when the interactive picker is unavailable."""
        if len(arguments) > 1:
            self._screen_status = "Usage: /resume [conversation-id-prefix]"
            self._append_screen_text(self._screen_status)
            return True
        if arguments:
            try:
                summary = self._find_conversation(arguments[0], resumable_only=True)
                record = self._continuation_record(summary)
            except ValueError as exc:
                self._screen_status = str(exc)
                self._append_screen_text(f"Unable to resume conversation: {exc}")
                return True
            records = self._conversation_records(summary) or self._conversation_lineage(record)
            self._last_task_id = record.id
            self._screen_last_view = self._view_for(record)
            if self._pt_app:
                self._append_pt_conversation_transcript(records)
            else:
                self._append_screen_text(self._screen_conversation_summary(records))
            self._active_conversation_task_id = record.id
            conversation_id = self._conversation_id(summary)
            self._screen_status = (
                f"Resumed conversation {conversation_id[:8]} · "
                f"{self._conversation_task_count(summary)} task"
                f"{'s' if self._conversation_task_count(summary) != 1 else ''}"
            )
            self._append_screen_text(
                f"Conversation {conversation_id[:8]} is active. Send a normal message to continue it, or use /new to start fresh."
            )
            return True
        conversations = self._list_conversations(limit=12, resumable_only=True)
        if not conversations:
            self._screen_status = "No resumable conversations are available. Start a task first."
            self._append_screen_text(self._screen_status)
            return True
        self._screen_status = "Choose a saved conversation"
        self._append_screen_text(
            "Resumable conversations:\n"
            + "\n".join(self._conversation_line(item) for item in conversations)
            + "\nUse /resume <conversation-id-prefix> to activate one."
        )
        return True

    def _active_conversation_source(self) -> TaskRecord | None:
        """Return the explicitly active resumable conversation, if one exists."""
        task_id = self._active_conversation_task_id
        if not task_id:
            return None
        record = self.service.get_continuable_task(task_id)
        if record:
            return record
        # A task can disappear after pruning or become unavailable after a
        # workspace change. Never silently fall back to an unrelated history.
        self._active_conversation_task_id = None
        return None

    def _active_conversation_id(self) -> str | None:
        record = self._active_conversation_source()
        return record.id if record else None

    def _active_conversation_key(self) -> str | None:
        """Return the stable conversation id used for conversation-level actions."""
        record = self._active_conversation_source()
        return (record.conversation_id or record.id) if record else None

    def _continuation_source(self) -> TaskRecord | None:
        """Prefer the active session, then an explicitly viewed/latest task."""
        active = self._active_conversation_source()
        if active:
            return active
        if self._last_task_id:
            selected = self.service.get_continuable_task(self._last_task_id)
            if selected:
                return selected
        return self.service.latest_continuable_task()

    def _detail_view(self, arguments: list[str]) -> TaskView:
        if len(arguments) > 1:
            raise ValueError("usage: /details [task-id-prefix]")
        if arguments:
            return self._view_for(self._find_task(arguments[0]))
        view = self._pt_latest_activity_view()
        if view:
            return view
        if self._last_task_id:
            record = self.service.get_task(self._last_task_id)
            if record:
                return self._view_for(record)
        raise ValueError("no task is available; run a task or use a task id")

    def _conversation_lineage(self, record: TaskRecord) -> list[TaskRecord]:
        """Use persisted parent links when available, with old services safe."""
        resolve = getattr(self.service, "conversation_lineage", None)
        if callable(resolve):
            try:
                records = resolve(record.id)
            except Exception:
                records = []
            if isinstance(records, list) and records and records[-1].id == record.id:
                return records
        return [record]

    def _append_pt_conversation_transcript(self, records: list[TaskRecord]) -> None:
        """Render an opened conversation as normal transcript turns.

        This deliberately avoids a raw task/event dump: each historical task
        is represented by its prompt, final response, and collapsed activity,
        matching the live conversation visual treatment.
        """
        if len(records) > 1:
            self._pt_history.append(("conversation", f"Restored conversation · {len(records)} task turns"))
        for item in records:
            view = self._view_for(item)
            self._pt_history.append(("user", item.task))
            if item.status == "completed":
                self._pt_history.append(("assistant", item.result or "Completed."))
            elif item.status == "cancelled":
                self._pt_history.append(("system", "Task cancelled; its saved context can still be continued."))
            else:
                self._pt_history.append(("error", present_model_error(item.error or "task failed")))
            if view.operations or view.context_updates:
                self._pt_history.append(("activity", view))
            self._pt_history.append(("completion", view))

    def _screen_conversation_summary(self, records: list[TaskRecord]) -> str:
        """Fallback presentation for the minimal screen/line interfaces."""
        lines = [f"Restored conversation · {len(records)} task turn{'s' if len(records) != 1 else ''}"]
        for index, item in enumerate(records, start=1):
            lines.extend(["", f"Turn {index} · Task {item.id[:8]} · {item.status}", f"Prompt: {item.task}"])
            if item.status == "completed" and item.result:
                lines.extend(["Result:", item.result])
            elif item.status == "cancelled":
                lines.append("Result: Task cancelled; saved context remains available.")
            elif item.error:
                lines.append(f"Error: {present_model_error(item.error)}")
        return "\n".join(lines)

    def _screen_open(self, arguments: list[str]) -> bool:
        """Inspect exactly one task without selecting its conversation.

        ``/resume`` owns the stateful operation. Keeping ``/open`` read-only
        prevents a user browsing old task details from silently changing where
        their next ordinary message will be sent.
        """
        if len(arguments) != 1:
            self._screen_status = "Usage: /open <task-id-prefix>"
            self._append_screen_text(self._screen_status)
            return True
        try:
            record = self._find_task(arguments[0])
        except ValueError as exc:
            self._screen_status = str(exc)
            self._append_screen_text(f"Unable to open task: {exc}")
            return True
        self._last_task_id = record.id
        view = self._view_for(record)
        self._screen_last_view = view
        if self._pt_app:
            self._pt_history.append(("conversation", f"Opened task {record.id[:8]} · inspection"))
            self._append_pt_conversation_transcript([record])
        else:
            self._append_screen_text(self._screen_task_summary(record, view, include_result=True, include_details=True))
        self._screen_status = f"Opened task {record.id[:8]} for inspection"
        self._append_screen_text("Task inspection does not change the active conversation. Use /resume to continue a saved conversation.")
        return True

    def _screen_show_details(self, arguments: list[str], *, inspect: bool = False) -> bool:
        try:
            view = self._detail_view(arguments)
            record = self.service.get_task(view.task_id)
            if not record:
                raise ValueError("task context is unavailable")
        except ValueError as exc:
            self._screen_status = str(exc)
            self._append_screen_text(f"Details unavailable: {exc}")
            return True
        self._append_screen_text(self._screen_task_summary(record, view, include_result=False, include_details=True))
        self._screen_status = "Operation details shown inline" if inspect else f"Details for task {record.id[:8]}"
        return True

    @staticmethod
    def _screen_task_summary(
        record: TaskRecord,
        view: TaskView,
        *,
        include_result: bool,
        include_details: bool = False,
    ) -> str:
        lines = [
            f"Task {record.id[:8]} · {record.status}",
            f"Prompt: {task_summary(record.task, 76)}",
            f"Operations: {len(view.operations)} · {format_duration(view.duration_seconds)}",
        ]
        for index, operation in enumerate(view.operations, start=1):
            marker = "OK" if operation.ok else "Failed" if operation.ok is False else "Running"
            lines.append(f"  {index}. [{marker}] {operation_summary(operation)}")
            if include_details and operation.ok is False:
                lines.append(f"     {operation_error(operation)}")
        if record.status == "failed":
            lines.append(f"Error: {present_model_error(record.error or view.error or 'task failed')}")
        elif include_result and record.result:
            lines.extend(["", "Result:", record.result])
        return "\n".join(lines)

    def _screen_switch_model(self, name: str) -> bool:
        try:
            self.config = self.models.switch(name)
            self.service.update_config(self.config)
            self.models = ModelManager(self.config)
            self._screen_status = f"Model switched to {self.config.model}"
        except ValueError as exc:
            self._screen_status = str(exc)
        return True

    def _start_screen_task(self, task: str, *, resume_from: str | None = None) -> None:
        if self._pt_workspace_trusted is not True:
            self._screen_status = "Trust this workspace before running a task (y / n)"
            return
        if self._screen_active and self._screen_active[0].status in {"queued", "running"}:
            self._screen_status = "A task is already running. Press Ctrl+C to cancel it."
            return
        record = self.service.create_task(task, demo=self.demo, resume_from=resume_from)
        self._last_task_id = record.id
        view = TaskView(record.id, task)
        self._screen_active = (record, view, 0)
        self._screen_last_view = view
        self._screen_stream = ""
        self._screen_error_seen = False
        self._screen_activity_expanded = False
        self._screen_selected_operation = -1
        self._screen_expanded_operations.clear()
        self._screen_status = "Working..."
        if len(task) > LONG_PROMPT_CHARS and not self._pt_app:
            self._append_screen_text(f"> User prompt · {len(task)} chars  (/prompt {record.id[:8]})")

    def _pump_screen_task(self) -> None:
        if not self._screen_active:
            return
        record, view, sequence = self._screen_active
        for event in self.service.events(record.id, sequence):
            sequence = event.sequence
            view.apply(event)
            if event.type == "assistant_delta":
                self._screen_stream += str(event.data.get("delta", ""))
            elif event.type == "model_retrying":
                self._screen_stream = ""
            elif event.type == "tool_started":
                self._screen_stream = ""
            elif event.type == "task_error":
                if not self._screen_error_seen:
                    self._screen_error_seen = True
                    self._append_screen_text(f"Task failed: {present_model_error(str(event.data.get('error', 'unknown error')))}")
            elif event.type == "task_cancelled":
                self._append_screen_text("Task cancelled.")
        self._screen_active = (record, view, sequence)
        if record.status not in TERMINAL_TASK_STATUSES:
            state = "Cancelling..." if view.cancelling else "Working..."
            self._screen_status = f"{state} · {format_duration(view.duration_seconds)}"
            return
        view.finish(record.status)
        self._active_conversation_task_id = (
            record.id if self.service.get_continuable_task(record.id) else None
        )
        if record.status == "completed":
            self._append_screen_text(record.result or self._screen_stream or "Completed.")
        self._screen_stream = ""
        self._screen_status = "Ready · /help · /model · /skills"
        self._screen_active = None

    def _screen_prompt_details(self, arguments: list[str]) -> None:
        try:
            record = self._detail_record(arguments)
        except ValueError as exc:
            self._screen_status = str(exc)
            return
        self._append_screen_text(f"User prompt ({len(record.task)} chars):\n{record.task}")

    def _append_screen_text(self, text: str) -> None:
        if self._pt_app:
            self._pt_history.append(("command", str(text)))
        width = max(36, self._terminal_width() - 4)
        for line in str(text).splitlines() or [""]:
            self._screen_lines.extend(textwrap.wrap(line, width=width, replace_whitespace=False) or [""])
        self._screen_lines = self._screen_lines[-SCREEN_HISTORY_LIMIT:]

    def _screen_height(self) -> int:
        try:
            return max(12, os.get_terminal_size().lines)
        except OSError:
            return 24

    def _screen_activity_lines(self) -> list[str]:
        view = self._screen_current_view()
        if not view:
            return []
        elapsed = int(view.duration_seconds)
        marker = "v" if self._screen_activity_expanded else ">"
        cursor = ">" if self._screen_focus_activity and self._screen_selected_operation < 0 else " "
        context_count = len(view.context_updates)
        context_label = f" · {context_count} context update{'s' if context_count != 1 else ''}" if context_count else ""
        lines = [f"{cursor} {marker} Agent activity · {len(view.operations)} operations{context_label} · {elapsed}s  (Tab, Enter)"]
        if not self._screen_activity_expanded:
            return lines
        for update in view.context_updates:
            lines.append(f"    Context: {update}")
        for index, operation in enumerate(view.operations):
            icon = "+" if operation.ok else "!"
            cursor = ">" if self._screen_focus_activity and index == self._screen_selected_operation else " "
            lines.append(f"{cursor} {icon} {operation_summary(operation)}")
            if index in self._screen_expanded_operations:
                lines.extend(self._screen_operation_details(operation))
        if self._screen_active and not view.operations and not view.context_updates:
            lines.append("  ... Waiting for the first operation")
        return lines

    def _screen_current_view(self) -> TaskView | None:
        if self._screen_active:
            return self._screen_active[1]
        if self._screen_last_view and self._screen_last_view.task_id == self._last_task_id:
            return self._screen_last_view
        if self._last_task_id:
            record = self.service.get_task(self._last_task_id)
            return self._view_for(record) if record else None
        return None

    def _screen_operation_details(self, operation: ToolOperation) -> list[str]:
        result = operation.result or {}
        lines = [f"    Tool: {operation.tool}"]
        if operation.arguments.get("path"):
            lines.append(f"    Path: {operation.arguments['path']}")
        if operation.tool == "run_command" and operation.arguments.get("command"):
            lines.append(f"    Command: {_short_value(operation.arguments['command'], 90)}")
            lines.append(f"    Exit code: {result.get('returncode', '?')}")
        if result.get("bytes") is not None:
            lines.append(f"    Size: {result['bytes']} bytes")
        output = result.get("output")
        if output:
            output_lines = str(output).splitlines()
            lines.append(f"    > Command output · {len(output_lines)} lines")
            lines.extend(f"      {line}" for line in _bounded_lines(output, 8))
        if result.get("error"):
            lines.append(f"    Error: {_short_value(result['error'], 150)}")
        return lines

    def _render_screen(self) -> None:
        """Redraw the alternate screen. Scrolling is an offset from the latest content."""
        self._poll_compaction()
        width = max(40, min(140, self._terminal_width() + 2))
        height = self._screen_height()
        # Reserve dedicated rows for the composer, mode indicator, and status.
        body_height = max(3, height - 6)
        content = list(self._screen_lines)
        if self._screen_stream:
            content.extend(self._screen_stream.splitlines() or [self._screen_stream])
        if self._screen_active:
            content.append(f"... {self._screen_status}")
        compact = self._compact_work_snapshot()
        if compact and not compact.completed.is_set():
            frames = (".", "..", "...", " ..", "  .", "   ")
            frame = frames[int(time.monotonic() * 5) % len(frames)]
            content.append(
                f"{frame:<3} Working · compacting conversation context · {format_duration(compact.duration_seconds)}"
            )
        content.extend(self._screen_activity_lines())
        content = [textwrap.shorten(line, width=width - 4, placeholder="...") if line else "" for line in content]
        end = max(0, len(content) - self._screen_scroll)
        start = max(0, end - body_height)
        visible = content[start:end]
        frame = (
            width,
            height,
            tuple(visible),
            self._screen_input,
            self._screen_status,
            self._screen_focus_activity,
            self._screen_scroll,
        )
        if frame == self._screen_last_frame:
            return
        self._screen_last_frame = frame
        rows: list[tuple[str, bool, bool]] = []
        rows.append((f" LimoCode{' ' * max(1, width - len(' LimoCode') - len('Model: ' + self.config.model) - 1)}Model: {self.config.model}", True, False))
        rows.append(("-" * width, False, True))
        for line in visible:
            rows.append((f" {line}", False, False))
        for _ in range(body_height - len(visible)):
            rows.append(("", False, False))
        rows.append(("-" * width, False, True))
        focus = "[activity] " if self._screen_focus_activity else ""
        rows.append((f" > {focus}{self._screen_input}", True, False))
        mode = str(getattr(self.config, "permission_mode", "approval") or "approval").lower()
        mode_text = (
            " mode: auto  ·  file changes apply automatically"
            if mode == "auto"
            else " mode: approval  ·  file changes ask before applying"
        )
        rows.append((mode_text, False, True))
        rows.append((self._screen_status, False, True))
        for row, (text, bold, dim) in enumerate(rows, start=1):
            self._screen_write_row(row, text, width, bold=bold, dim=dim)
        input_column = min(width, len(f" > {focus}{self._screen_input}") + 1)
        sys.stdout.write(f"\x1b[{height - 2};{input_column}H\x1b[?25h")
        sys.stdout.flush()

    def _screen_write_row(self, row: int, text: str, width: int, *, bold: bool = False, dim: bool = False) -> None:
        content = text[:width].ljust(width)
        code = "1" if bold else "2" if dim else "0"
        sys.stdout.write(f"\x1b[{row};1H\x1b[K\x1b[{code}m{content}\x1b[0m")

    def _enter_alternate_screen(self) -> None:
        self._screen_last_frame = None
        sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[?25h")
        sys.stdout.flush()

    def _leave_alternate_screen(self) -> None:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

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
        if command == "mode":
            self._mode(arguments)
            return True
        if command == "changes":
            self._changes(arguments)
            return True
        if command == "undo":
            self._undo(arguments)
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
        if command == "new":
            self._new(arguments)
            return True
        if command == "continue":
            self._continue(arguments)
            return True
        if command == "history":
            self._history()
            return True
        if command == "resume":
            self._resume(arguments)
            return True
        if command == "open":
            self._open(arguments)
            return True
        if command in {"details", "detail"}:
            self._details(arguments)
            return True
        if command == "inspect":
            self._inspect(arguments)
            return True
        if command == "activity":
            self._inspect(arguments)
            return True
        if command == "prompt":
            self._prompt_details(arguments)
            return True
        if command == "clear":
            self._clear()
            self._banner()
            return True
        self._write(f"unknown command: /{command}. Type /help.", "red")
        return True

    def _run_task(self, task: str, resume_from: str | None = None) -> None:
        if self._pt_workspace_trusted is not True and not self._confirm_workspace_trust():
            return
        if resume_from is None:
            resume_from = self._active_conversation_id()
        record = self.service.create_task(task, demo=self.demo, resume_from=resume_from)
        self._last_task_id = record.id
        view = TaskView(record.id, task)
        self._stream(record, view)

    def _stream(self, record: TaskRecord, view: TaskView) -> None:
        sequence = 0
        interrupted = False
        announced_analysis = False
        shown_terminal_error = False
        streamed_response = ""
        streamed_started = False
        stream_line_open = False
        live_status = False
        spinner_index = 0
        next_spinner_at = 0.0

        def finish_status() -> None:
            nonlocal live_status
            if live_status:
                sys.stdout.write("\r\x1b[2K")
                sys.stdout.flush()
                live_status = False

        def render_status(label: str) -> None:
            nonlocal live_status
            if not sys.stdout.isatty():
                return
            sys.stdout.write(f"\r\x1b[2K  {label}")
            sys.stdout.flush()
            live_status = True

        def finish_stream_line() -> None:
            """Finish a partial streamed line before writing a status event."""
            nonlocal stream_line_open
            if stream_line_open:
                sys.stdout.write("\n")
                sys.stdout.flush()
                stream_line_open = False

        def write_stream_delta(delta: str) -> None:
            """Write each model chunk immediately into the normal terminal buffer."""
            nonlocal streamed_started, stream_line_open
            finish_status()
            if not streamed_started:
                print()
                self._write_rule()
                self._write("  Result", "blue")
                streamed_started = True
                sys.stdout.write("  ")
            # The non-stream renderer handles complete Markdown paragraphs.
            # During streaming we prefer immediate, readable prose over waiting
            # for a newline; suppress common marker characters as chunks arrive.
            rendered = self._render_markdown_delta(delta).replace("\r\n", "\n")
            sys.stdout.write(rendered.replace("\n", "\n  "))
            sys.stdout.flush()
            stream_line_open = not rendered.endswith("\n")

        def show_event(event: AgentEvent) -> None:
            nonlocal announced_analysis, shown_terminal_error, streamed_response
            operation = view.apply(event)
            if event.type == "model_thinking" and not announced_analysis:
                announced_analysis = True
                return
            if event.type == "tool_started":
                operation = view.operations[-1] if view.operations else None
                if operation:
                    finish_stream_line()
                    render_status(self._operation_status(operation))
                return
            if event.type == "tool_finished" and operation:
                announced_analysis = False
                return
            if event.type == "assistant_delta":
                delta = str(event.data.get("delta", ""))
                if delta:
                    write_stream_delta(delta)
                    streamed_response += delta
                return
            if event.type == "model_retrying":
                finish_status()
                finish_stream_line()
                self._write("  ... Retrying model request", "yellow")
                return
            if event.type == "model_rate_limited":
                finish_status()
                finish_stream_line()
                self._write("  ... Waiting for model rate limit", "yellow")
                return
            if event.type == "task_error":
                if shown_terminal_error:
                    return
                shown_terminal_error = True
                finish_status()
                finish_stream_line()
                self._write(f"  ! {present_model_error(str(event.data.get('error', 'unknown error')))}", "red")
                return
            if event.type == "command_approval_requested":
                finish_status()
                finish_stream_line()
                self._write("  ! Command approval required", "yellow")
                self._resolve_approval(record, event)
            if event.type == "changeset_approval_requested":
                finish_status()
                finish_stream_line()
                files = event.data.get("files") or []
                paths = ", ".join(str(item.get("path", "file")) for item in files if isinstance(item, dict))
                self._write(f"  ! File changes require approval: {paths or 'workspace'}", "yellow")
                for item in files:
                    if isinstance(item, dict) and item.get("unified_diff"):
                        self._write_block(str(item["unified_diff"]))
                self._resolve_change_approval(record, event)

        while True:
            try:
                events = self.service.events(record.id, sequence)
                for event in events:
                    sequence = event.sequence
                    show_event(event)
                if record.status in TERMINAL_TASK_STATUSES:
                    break
                now = time.monotonic()
                if announced_analysis and now >= next_spinner_at:
                    dots = (".", "..", "...")[spinner_index % 3]
                    operation_count = len(view.operations)
                    activity = f"  Activity · {operation_count} operation{'s' if operation_count != 1 else ''}" if operation_count else ""
                    render_status(f"Thinking{dots}{activity}")
                    spinner_index += 1
                    next_spinner_at = now + 0.25
                time.sleep(0.08)
            except KeyboardInterrupt:
                if not interrupted:
                    interrupted = self.service.cancel_task(record.id)
                    self._write("  cancellation requested", "yellow")
                else:
                    self._write("  waiting for task cleanup", "yellow")
        for event in self.service.events(record.id, sequence):
            show_event(event)
        view.finish(record.status)
        self._active_conversation_task_id = (
            record.id if self.service.get_continuable_task(record.id) else None
        )
        finish_status()
        if record.status == "completed":
            result = record.result or "No final result."
            if streamed_response:
                finish_stream_line()
            else:
                print()
                self._write_rule()
                self._write("  Result", "blue")
                self._write_block(result)
            if view.operations or view.context_updates:
                context = f" | {len(view.context_updates)} context updates" if view.context_updates else ""
                self._write(
                    f"  Activity: {len(view.operations)} operations{context} in {view.duration_seconds:.1f}s  (/activity {view.task_id[:8]})",
                    "dim",
                )
        elif record.status == "cancelled" and record.result:
            self._write("\nTask cancelled.", "yellow")
            self._write_block(record.result)

    @staticmethod
    def _operation_status(operation: ToolOperation) -> str:
        if operation.tool == "write_file":
            return f"Editing {operation.arguments.get('path', 'file')}..."
        if operation.tool == "read_file":
            return f"Reading {operation.arguments.get('path', 'file')}..."
        if operation.tool == "list_files":
            return "Inspecting project files..."
        if operation.tool == "run_command":
            return f"Running {_short_value(operation.arguments.get('command', 'command'), 58)}..."
        return "Working..."

    def _render_task_header(self, view: TaskView) -> None:
        width = self._terminal_width()
        label = f" Task {view.task_id[:8]} "
        line = "-" * max(8, width - len(label) - 5)
        self._write(f"\n  +--{label}{line}+", "bold")
        self._write(f"  |  {task_summary(view.task, width - 8)}")
        self._write(f"  +{'-' * max(8, width - 3)}+", "bold")

    def _render_operation(self, operation: ToolOperation) -> None:
        if operation.ok:
            colour = "green"
            text = operation_summary(operation)
            self._write(f"  + {text}", colour)
            return
        self._write(f"  ! {operation_error(operation)}", "yellow")
        if operation.tool == "run_command":
            command = _short_value(operation.arguments.get("command", ""), 96)
            if command:
                self._write(f"    $ {command}", "dim")

    def _render_completion(self, view: TaskView, *, include_duration: bool = True) -> None:
        parts = [f"{view.duration_seconds:.1f}s"] if include_duration else []
        if view.turns:
            parts.append(f"{view.turns} turns")
        parts.append(f"{len(view.operations)} operations")
        if view.context_updates:
            parts.append(f"{len(view.context_updates)} context updates")
        self._write(f"\n  + Completed ({' | '.join(parts)})", "green")
        if view.operations or view.context_updates:
            context = f" | {len(view.context_updates)} context updates" if view.context_updates else ""
            self._write(
                f"  > Agent activity: {len(view.operations)} operations{context} (use /inspect {view.task_id[:8]})",
                "dim",
            )

    def _resolve_approval(self, record: TaskRecord, event: AgentEvent) -> None:
        approval_id = event.data.get("approval_id")
        command = event.data.get("command")
        if not isinstance(approval_id, str) or not isinstance(command, str):
            return
        allow_always = event.data.get("allow_always") is True
        family_label = str(event.data.get("family_label") or "this command type")
        while True:
            try:
                prompt = "  approve this command? [y] once"
                if allow_always:
                    prompt += f" [a] always allow {family_label}"
                decision = input(prompt + " [N] reject ").strip().lower()
            except KeyboardInterrupt:
                decision = "n"
                print()
            if decision in {"", "n", "no"}:
                self.service.approve_command(record.id, approval_id, False)
                return
            if decision in {"y", "yes"}:
                self.service.approve_command(record.id, approval_id, True, scope="once")
                return
            if allow_always and decision in {"a", "always"}:
                self.service.approve_command(record.id, approval_id, True, scope="always")
                return
            self._write("  enter y, a, or n" if allow_always else "  enter y or n", "yellow")

    def _resolve_change_approval(self, record: TaskRecord, event: AgentEvent) -> None:
        approval_id = event.data.get("approval_id")
        if not isinstance(approval_id, str):
            return
        while True:
            try:
                decision = input("  apply these file changes? [y/N] ").strip().lower()
            except KeyboardInterrupt:
                decision = "n"
                print()
            if decision in {"", "n", "no"}:
                self.service.approve_changeset(record.id, approval_id, False)
                return
            if decision in {"y", "yes"}:
                self.service.approve_changeset(record.id, approval_id, True)
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
            print(f"  permission-mode  {self.config.permission_mode}")
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

    def _mode(self, arguments: list[str]) -> None:
        if len(arguments) > 1:
            raise ValueError("usage: /mode [approval|auto]")
        if not arguments:
            self._write(f"file changes: {self.service.permission_status()['mode']}", "bold")
            return
        payload = self.service.set_permission_mode(arguments[0])
        self.config = self.service.config
        self._write(f"file changes mode: {payload['mode']}", "green")

    def _changes(self, arguments: list[str]) -> None:
        if len(arguments) > 1:
            raise ValueError("usage: /changes [task-id-prefix]")
        task_id = None
        if arguments:
            record = self._find_task(arguments[0])
            if not record:
                raise ValueError("task not found")
            task_id = record.id
        changesets = self.service.list_changesets(task_id)
        if not changesets:
            self._write("no tracked Agent file changes", "dim")
            return
        self._write("file changes", "bold")
        for item in changesets:
            files = ", ".join(str(file.get("path", "file")) for file in item.get("files", []))
            self._write(f"  {item['id'][:8]}  {item['status']:<16} {files}")

    def _undo(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            raise ValueError("usage: /undo <changeset-id-prefix>")
        matches = [item for item in self.service.list_changesets() if item["id"].startswith(arguments[0])]
        if len(matches) != 1:
            raise ValueError("changeset not found or ambiguous")
        result = self.service.undo_changeset(matches[0]["id"])
        if not result:
            raise ValueError("changeset not found")
        if result["status"] == "undone":
            self._write(f"reverted {result['id'][:8]}", "green")
        elif result["status"] == "conflict":
            self._write(f"undo conflict: {result.get('error', 'file changed')}", "yellow")
        else:
            self._write(f"undo failed: {result.get('error', result['status'])}", "red")

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
        self._active_conversation_task_id = None
        self._refresh_workspace_trust()

    def _refresh_workspace_trust(self) -> None:
        """Reload the trust decision whenever the configured workspace changes."""
        self._pt_workspace_trusted = True if self.trust_store.is_trusted(self.config.workspace) else None

    def _trust_current_workspace(self) -> bool:
        """Persist trust before marking the interactive session as trusted."""
        saved = self.trust_store.trust(self.config.workspace)
        self._pt_workspace_trusted = True if saved else None
        return saved

    def _memory(self, arguments: list[str]) -> None:
        if not arguments:
            items = self.service.list_memories()
            self._show_memories(items)
            return
        action = arguments[0].lower()
        if action == "status" and len(arguments) == 1:
            for line in self._memory_status_text().splitlines():
                self._write(line)
            return
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
        raise ValueError("usage: /memory [status|add <content>|search <query>|delete <id>]")

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
        conversation_id = self._active_conversation_key()
        if not conversation_id:
            raise ValueError("no active conversation context is available; use /resume <conversation-id>")
        result = self.service.compact_conversation(conversation_id)
        if not result:
            raise ValueError("conversation context is unavailable")
        if result.compacted:
            self._write(f"conversation compacted: {result.before_tokens} -> {result.after_tokens} tokens", "green")
        else:
            self._write("conversation context is already compact", "green")

    def _continue(self, arguments: list[str]) -> None:
        if not arguments:
            raise ValueError("usage: /continue <instruction>")
        parent = self._continuation_source()
        if not parent:
            raise ValueError("no completed task context is available; run a task first or use /resume <conversation-id>")
        self._run_task(" ".join(arguments), resume_from=parent.id)

    def _history(self) -> None:
        conversations = self._list_conversations(limit=12)
        if not conversations:
            self._write("no conversation history")
            return
        self._write("conversation history", "bold")
        for item in conversations:
            print(self._conversation_line(item))
        self._write("use /resume <conversation-id> to continue one", "dim")

    def _resume(self, arguments: list[str]) -> None:
        if len(arguments) > 1:
            raise ValueError("usage: /resume [conversation-id-prefix]")
        if arguments:
            summary = self._find_conversation(arguments[0], resumable_only=True)
            record = self._continuation_record(summary)
            records = self._conversation_records(summary) or self._conversation_lineage(record)
            self._last_task_id = record.id
            self._screen_last_view = self._view_for(record)
            self._active_conversation_task_id = record.id
            self._write(
                f"restored conversation · {self._conversation_task_count(summary)} task"
                f"{'s' if self._conversation_task_count(summary) != 1 else ''}",
                "dim",
            )
            for item in records:
                view = self._view_for(item)
                self._render_task_header(view)
                for operation in view.operations[:MAX_VISIBLE_OPERATIONS]:
                    self._render_operation(operation)
                if len(view.operations) > MAX_VISIBLE_OPERATIONS:
                    self._write(f"  ... {len(view.operations) - MAX_VISIBLE_OPERATIONS} operations collapsed", "dim")
                if item.status == "completed" and item.result:
                    self._render_completion(view, include_duration=False)
                    self._write_rule()
                    self._write_block(item.result)
                elif item.status == "failed":
                    self._write(f"\n  ! {present_model_error(item.error or view.error or 'task failed')}", "red")
                elif item.status == "cancelled":
                    self._write("\n  ! Task cancelled; saved context remains available.", "yellow")
            self._write("  This conversation is active. Enter a normal message to continue it; use /new to start fresh.", "green")
            return
        conversations = self._list_conversations(limit=12, resumable_only=True)
        if not conversations:
            self._write("no resumable conversations; start a task first")
            return
        self._write("resumable conversations", "bold")
        for item in conversations:
            print(self._conversation_line(item))
        self._write("use /resume <conversation-id> to activate one", "dim")

    def _open(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            raise ValueError("usage: /open <task-id-prefix>")
        record = self._find_task(arguments[0])
        self._last_task_id = record.id
        view = self._view_for(record)
        self._screen_last_view = view
        self._write(f"opened task {record.id[:8]} · inspection", "dim")
        self._render_task_header(view)
        for operation in view.operations[:MAX_VISIBLE_OPERATIONS]:
            self._render_operation(operation)
        if len(view.operations) > MAX_VISIBLE_OPERATIONS:
            self._write(f"  ... {len(view.operations) - MAX_VISIBLE_OPERATIONS} operations collapsed", "dim")
        if record.status == "completed" and record.result:
            self._render_completion(view, include_duration=False)
            self._write_rule()
            self._write_block(record.result)
        elif record.status == "failed":
            self._write(f"\n  ! {present_model_error(record.error or view.error or 'task failed')}", "red")
        elif record.status == "cancelled":
            self._write("\n  ! Task cancelled; saved context remains available.", "yellow")
        self._write("  Task inspection does not change the active conversation. Use /resume to continue one.", "dim")

    def _new(self, arguments: list[str]) -> None:
        if arguments:
            raise ValueError("usage: /new")
        self._active_conversation_task_id = None
        self._write("new conversation selected; the next message starts fresh", "green")

    def _details(self, arguments: list[str]) -> None:
        record = self._detail_record(arguments)
        view = self._view_for(record)
        self._write(f"\nDetails: task {record.id[:8]}", "bold")
        self._write("  Prompt:", "dim")
        self._write_detail_value(record.task, indent="    ")
        if view.context_updates:
            self._write(f"  Context ({len(view.context_updates)}):", "bold")
            for update in view.context_updates:
                self._write(f"    {update}", "dim")
        if not view.operations:
            if not view.context_updates:
                self._write("  No tool operations were recorded.", "dim")
            return
        self._write(f"  Operations ({len(view.operations)}):", "bold")
        for index, operation in enumerate(view.operations, start=1):
            marker = "+" if operation.ok else "!"
            colour = "green" if operation.ok else "yellow"
            self._write(f"  {marker} {index}. {operation_summary(operation)}", colour)
            self._render_operation_detail(operation)

    def _inspect(self, arguments: list[str]) -> None:
        """Offer keyboard expansion on Windows without adding a terminal dependency."""
        record = self._detail_record(arguments)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            self._details(arguments)
            return
        try:
            import msvcrt
        except ImportError:
            self._details(arguments)
            return
        view = self._view_for(record)
        if not view.operations:
            self._details(arguments)
            return
        selected = 0
        expanded: set[int] = set()
        while True:
            self._clear()
            self._write(f"LimoCode details  task {record.id[:8]}", "bold")
            self._write("  Up/Down: select | Enter: expand/collapse | q: return", "dim")
            self._write(f"  {task_summary(record.task, self._terminal_width() - 4)}", "dim")
            for index, operation in enumerate(view.operations):
                cursor = ">" if index == selected else " "
                marker = "+" if operation.ok else "!"
                colour = "green" if operation.ok else "yellow"
                self._write(f"{cursor} {marker} {index + 1}. {operation_summary(operation)}", colour)
                if index in expanded:
                    self._render_operation_detail(operation)
            key = msvcrt.getwch()
            if key.lower() == "q" or key == "\x1b":
                self._clear()
                return
            if key in {"\r", " "}:
                if selected in expanded:
                    expanded.remove(selected)
                else:
                    expanded.add(selected)
                continue
            if key in {"\x00", "\xe0"}:
                key = msvcrt.getwch()
                if key == "H":
                    selected = max(0, selected - 1)
                elif key == "P":
                    selected = min(len(view.operations) - 1, selected + 1)

    def _detail_record(self, arguments: list[str]) -> TaskRecord:
        if len(arguments) > 1:
            raise ValueError("usage: /details [task-id-prefix]")
        if arguments:
            return self._find_task(arguments[0])
        if self._last_task_id:
            record = self.service.get_task(self._last_task_id)
            if record:
                return record
        raise ValueError("no task is available; run a task or use /details <task-id>")

    def _find_task(self, prefix: str) -> TaskRecord:
        matches = [item for item in self.service.list_tasks() if item["id"].startswith(prefix)]
        if len(matches) != 1:
            raise ValueError("task prefix must match exactly one task")
        return self.service.get_task(matches[0]["id"])

    def _view_for(self, record: TaskRecord) -> TaskView:
        view = TaskView(record.id, record.task)
        for event in self.service.events(record.id):
            view.apply(event)
        if record.status in TERMINAL_TASK_STATUSES:
            view.finish(record.status)
        else:
            view.status = record.status
        return view

    def _render_operation_detail(self, operation: ToolOperation) -> None:
        arguments = operation.arguments
        result = operation.result or {}
        if operation.tool == "run_command" and arguments.get("command"):
            self._write("    Command:", "dim")
            self._write_detail_value(arguments["command"], indent="      ")
            if result.get("returncode") is not None:
                self._write(f"    Exit code: {result['returncode']}", "dim")
        elif arguments.get("path"):
            self._write(f"    Path: {arguments['path']}", "dim")
        if arguments.get("content"):
            self._write("    Content preview:", "dim")
            self._write_detail_value(arguments["content"], indent="      ")
        output = result.get("output")
        if output:
            self._write("    Output:", "dim")
            self._write_detail_value(output, indent="      ")
        if not result.get("ok"):
            self._write(f"    Error: {operation_error(operation)}", "red")

    def _write_detail_value(self, value: Any, *, indent: str) -> None:
        text = str(value or "")
        clipped = text[:MAX_DETAIL_CHARS]
        for line in _bounded_lines(clipped):
            self._write(f"{indent}{line}")
        if len(text) > MAX_DETAIL_CHARS:
            self._write(f"{indent}... additional content hidden ...", "dim")

    def _write_rule(self) -> None:
        self._write("  " + "-" * self._terminal_width(), "dim")

    def _banner(self) -> None:
        self._write("LimoCode", "bold")
        print(f"  workspace  {self.config.workspace}")
        print(f"  model      {self.config.model}  ({'demo' if self.demo else 'live'})")
        print("  Enter a task, or /help for commands.\n")

    def _prompt(self) -> str:
        return self._colour("LimoCode > ", "blue")

    def _terminal_width(self) -> int:
        columns = os.get_terminal_size().columns if sys.stdout.isatty() else 88
        return max(48, min(96, columns - 2))

    def _write_block(self, text: str, max_lines: int = 40) -> None:
        lines = str(text).replace("\\`", "`").replace("\\*", "*").splitlines() or [""]
        clipped = lines[:max_lines]
        in_code_block = False
        for line in clipped:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                continue
            self._write_markdown_line(line, in_code_block)
        if len(lines) > max_lines:
            self._write(f"  ... {len(lines) - max_lines} response lines hidden ...", "dim")

    def _write_markdown_line(self, line: str, in_code_block: bool = False) -> None:
        """Write one model-response line using a restrained terminal Markdown style."""
        width = self._terminal_width()
        stripped = line.strip()
        heading = re.fullmatch(r"(?:\*\*|__)(.+?)(?:\*\*|__)", stripped)
        if heading:
            self._write(f"  {heading.group(1).strip()}", "blue")
            return
        bullet = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        if bullet:
            rendered = self._render_markdown_line(bullet.group(2), in_code_block)
            prefix = "  - "
        else:
            rendered = self._render_markdown_line(line, in_code_block)
            prefix = "  "
        for wrapped in textwrap.wrap(rendered, width=max(20, width - len(prefix)), replace_whitespace=False) or [""]:
            print(prefix + wrapped)

    @staticmethod
    def _render_markdown_line(line: str, in_code_block: bool) -> str:
        """Render the small Markdown subset model replies commonly use in a terminal."""
        if in_code_block:
            return "  " + line
        rendered = line.replace("\\`", "`").replace("\\*", "*")
        rendered = re.sub(r"^\s{0,3}#{1,6}\s+", "", rendered)
        rendered = re.sub(r"\*\*(.+?)\*\*", r"\1", rendered)
        rendered = re.sub(r"__(.+?)__", r"\1", rendered)
        rendered = re.sub(r"`([^`]+)`", r"\1", rendered)
        rendered = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", rendered)
        rendered = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", rendered)
        return rendered

    @staticmethod
    def _render_markdown_delta(delta: str) -> str:
        """Keep streamed prose readable even when Markdown tokens cross chunks."""
        return delta.replace("\\`", "").replace("\\*", "*").replace("**", "").replace("__", "").replace("`", "")

    def _clear(self) -> None:
        print("\033[2J\033[H" if self._use_colour else "\n" * 3, end="")

    def _write(self, text: str, colour: str | None = None) -> None:
        print(self._colour(text, colour))

    def _colour(self, text: str, colour: str | None) -> str:
        if not self._use_colour or not colour:
            return text
        codes = {"bold": "1", "dim": "2", "blue": "36", "green": "32", "yellow": "33", "red": "31"}
        code = codes.get(colour)
        return f"\033[{code}m{text}\033[0m" if code else text


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal-first local Coding Agent")
    parser.add_argument("--workspace")
    parser.add_argument("--model", help="Override the configured model for this session")
    parser.add_argument("--model-timeout", type=int, help="Model request timeout in seconds")
    parser.add_argument("--max-turns", type=int, help="Maximum model/tool rounds for one task")
    parser.add_argument("--timeout", type=int, dest="command_timeout", help="Local command timeout in seconds")
    parser.add_argument("--approval-timeout", type=int, help="Command approval timeout in seconds")
    parser.add_argument("--min-request-interval", type=int, dest="request_gap", help="Minimum delay between model requests in milliseconds")
    parser.add_argument(
        "--color",
        choices=sorted(COLOR_MODES),
        default="auto",
        help="Color mode for the interactive TUI (default: auto)",
    )
    parser.add_argument("--demo", action="store_true", help="Start in deterministic offline demo mode")
    args = parser.parse_args()
    config = Config.from_env(args.workspace).with_overrides(
        model=args.model,
        model_timeout=args.model_timeout,
        max_turns=args.max_turns,
        command_timeout=args.command_timeout,
        command_approval_timeout=args.approval_timeout,
        model_min_request_interval_ms=args.request_gap,
    )
    if config.max_turns < 1:
        parser.error("--max-turns must be at least 1")
    if config.model_timeout < 1:
        parser.error("--model-timeout must be at least 1")
    if config.command_timeout < 1:
        parser.error("--timeout must be at least 1")
    if config.command_approval_timeout < 1:
        parser.error("--approval-timeout must be at least 1")
    if config.model_min_request_interval_ms < 0:
        parser.error("--min-request-interval must be at least 0")
    TerminalApp(config, demo=args.demo, color=args.color).run()


if __name__ == "__main__":
    main()
