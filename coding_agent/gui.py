"""Desktop GUI backed by AgentService, without duplicating agent logic."""

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .config import Config
from .service import AgentService


def tool_result_view(tool: str, result: dict[str, object]) -> tuple[str, str] | None:
    """Return the GUI view and bounded display text for a completed tool."""
    if tool == "run_command":
        title = f"$ {result.get('command', '')}".rstrip()
        details = result.get("output") or result.get("error") or "Command completed without output."
        suffix = f"\nExit code: {result['returncode']}" if "returncode" in result else ""
        return "commands", f"{title}\n{details}{suffix}\n\n"
    if tool == "write_file":
        path = result.get("path", "(unknown path)")
        before = result.get("previous_preview") or "(new file)"
        after = result.get("content_preview") or ""
        state = "changed" if result.get("changed") else "unchanged"
        return "changes", f"{path} ({state})\n--- before\n{before}\n+++ after\n{after}\n\n"
    return None


class AgentWindow:
    def __init__(self, root: tk.Tk, config: Config, demo: bool = False):
        self.root, self.config, self.demo = root, config, demo
        self.service = AgentService(config)
        self.record = None
        self.last_sequence = 0
        root.title("Local Coding Agent")
        root.geometry("1040x700")
        root.minsize(780, 520)

        controls = tk.Frame(root, padx=12, pady=10)
        controls.pack(fill=tk.X)
        tk.Label(controls, text="Task").grid(row=0, column=0, sticky="w")
        self.task = tk.Entry(controls)
        self.task.grid(row=0, column=1, columnspan=5, sticky="ew", padx=(8, 0))
        tk.Label(controls, text="Model").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.model = tk.StringVar(value=config.model)
        tk.Entry(controls, textvariable=self.model).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        tk.Label(controls, text="API timeout (s)").grid(row=1, column=2, sticky="e", pady=(8, 0))
        self.model_timeout = tk.Spinbox(controls, from_=1, to=600, width=7)
        self.model_timeout.delete(0, tk.END)
        self.model_timeout.insert(0, str(config.model_timeout))
        self.model_timeout.grid(row=1, column=3, sticky="w", padx=(8, 0), pady=(8, 0))
        tk.Label(controls, text="Workspace").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.workspace = tk.StringVar(value=str(config.workspace))
        tk.Entry(controls, textvariable=self.workspace).grid(row=2, column=1, columnspan=3, sticky="ew", padx=(8, 4), pady=(8, 0))
        tk.Button(controls, text="Browse", command=self.choose_workspace).grid(row=2, column=4, pady=(8, 0))
        self.demo_var = tk.BooleanVar(value=demo)
        tk.Checkbutton(controls, text="Offline demo", variable=self.demo_var).grid(row=2, column=5, padx=(8, 0), pady=(8, 0))
        tk.Label(controls, text="Max turns").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.max_turns = tk.Spinbox(controls, from_=1, to=100, width=7)
        self.max_turns.delete(0, tk.END)
        self.max_turns.insert(0, str(config.max_turns))
        self.max_turns.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        tk.Label(controls, text="Command timeout (s)").grid(row=3, column=2, sticky="e", pady=(8, 0))
        self.timeout = tk.Spinbox(controls, from_=1, to=600, width=7)
        self.timeout.delete(0, tk.END)
        self.timeout.insert(0, str(config.command_timeout))
        self.timeout.grid(row=3, column=3, sticky="w", padx=(8, 0), pady=(8, 0))
        self.start_button = tk.Button(controls, text="Start", command=self.start)
        self.start_button.grid(row=3, column=4, pady=(8, 0))
        self.stop_button = tk.Button(controls, text="Stop", command=self.stop, state=tk.DISABLED)
        self.stop_button.grid(row=3, column=5, padx=(8, 0), pady=(8, 0))
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)

        actions = tk.Frame(root, padx=12)
        actions.pack(fill=tk.X)
        tk.Button(actions, text="Copy result", command=self.copy_result).pack(side=tk.LEFT)
        tk.Button(actions, text="Open workspace", command=self.open_workspace).pack(side=tk.LEFT, padx=6)
        tk.Button(actions, text="Clear", command=self.clear).pack(side=tk.LEFT)
        self.status = tk.Label(actions, text="Ready", fg="#52616b")
        self.status.pack(side=tk.RIGHT)

        panes = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED)
        panes.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        self.events = scrolledtext.ScrolledText(panes, state=tk.DISABLED, wrap=tk.WORD, width=58)
        panes.add(self.events, minsize=300)
        self.views = ttk.Notebook(panes)
        self.commands = scrolledtext.ScrolledText(self.views, state=tk.DISABLED, wrap=tk.WORD, width=58)
        self.changes = scrolledtext.ScrolledText(self.views, state=tk.DISABLED, wrap=tk.WORD, width=58)
        self.result = scrolledtext.ScrolledText(self.views, state=tk.DISABLED, wrap=tk.WORD, width=58)
        self.views.add(self.commands, text="Command output")
        self.views.add(self.changes, text="File changes")
        self.views.add(self.result, text="Final answer")
        panes.add(self.views, minsize=360)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _updated_config(self) -> Config:
        workspace = Path(self.workspace.get()).expanduser().resolve()
        return replace(
            self.config,
            workspace=workspace,
            model=self.model.get().strip() or self.config.model,
            model_timeout=int(self.model_timeout.get()),
            max_turns=int(self.max_turns.get()),
            command_timeout=int(self.timeout.get()),
        )

    def choose_workspace(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.workspace.get())
        if selected:
            self.workspace.set(selected)

    def start(self) -> None:
        if not self.task.get().strip() or (self.record and self.record.status == "running"):
            return
        try:
            self.config = self._updated_config()
        except (ValueError, OSError):
            self.status.config(text="Invalid workspace or numeric setting", fg="#a33131")
            return
        self.service = AgentService(self.config)
        self.clear()
        self.record = self.service.create_task(self.task.get(), demo=self.demo_var.get())
        self.last_sequence = 0
        self.status.config(text="Running", fg="#2563eb")
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.root.after(100, self.poll)

    def poll(self) -> None:
        if not self.record:
            return
        for event in self.service.events(self.record.id, self.last_sequence):
            self.last_sequence = event.sequence
            self._append(self.events, f"[{event.type}]\n{event.data}\n\n")
            self._route_event(event.type, event.data)
        if self.record.status in {"completed", "failed", "cancelled"}:
            self._append(self.result, self.record.result or self.record.error or "No final result.")
            colour = "#14804a" if self.record.status == "completed" else "#a33131"
            self.status.config(text=self.record.status.capitalize(), fg=colour)
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)
            return
        self.root.after(100, self.poll)

    def stop(self) -> None:
        if self.record and self.service.cancel_task(self.record.id):
            self.status.config(text="Cancelling", fg="#a56a00")
            self.stop_button.config(state=tk.DISABLED)

    def _append(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.insert(tk.END, text)
        widget.see(tk.END)
        widget.configure(state=tk.DISABLED)

    def _route_event(self, event_type: str, data: dict[str, object]) -> None:
        if event_type == "command_approval_requested":
            approval_id, command = data.get("approval_id"), data.get("command")
            if self.record and isinstance(approval_id, str) and isinstance(command, str):
                approved = messagebox.askyesno(
                    "Approve command",
                    f"The agent requested a high-risk command:\n\n{command}\n\nRun it in the selected workspace?",
                    parent=self.root,
                )
                self.service.approve_command(self.record.id, approval_id, approved)
        elif event_type == "tool_finished":
            tool = data.get("tool")
            result = data.get("result")
            if isinstance(tool, str) and isinstance(result, dict):
                view = tool_result_view(tool, result)
                if view:
                    target, text = view
                    widget = self.commands if target == "commands" else self.changes
                    self._append(widget, text)
                    self.views.select(widget)
        elif event_type in {"assistant_message", "task_finished", "task_error", "task_cancelled"}:
            final = data.get("content") or data.get("result") or data.get("error")
            if final:
                self._append(self.result, str(final) if isinstance(final, str) else json.dumps(final, ensure_ascii=False, indent=2))
                self.views.select(self.result)

    def clear(self) -> None:
        for widget in (self.events, self.commands, self.changes, self.result):
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.configure(state=tk.DISABLED)
        self.views.select(self.result)

    def copy_result(self) -> None:
        text = self.result.get("1.0", tk.END).strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status.config(text="Result copied", fg="#14804a")

    def open_workspace(self) -> None:
        target = str(Path(self.workspace.get()).expanduser())
        try:
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target])
        except OSError:
            self.status.config(text="Could not open workspace", fg="#a33131")

    def close(self) -> None:
        if self.record and self.record.status == "running":
            self.service.cancel_task(self.record.id)
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Coding Agent desktop GUI")
    parser.add_argument("--workspace")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    root = tk.Tk()
    AgentWindow(root, Config.from_env(args.workspace), args.demo)
    root.mainloop()


if __name__ == "__main__":
    main()
