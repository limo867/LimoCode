"""Small tkinter desktop entry point backed by the shared AgentService."""

import argparse
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from pathlib import Path

from .config import Config
from .service import AgentService


class AgentWindow:
    def __init__(self, root: tk.Tk, config: Config, demo: bool = False):
        self.root, self.config, self.demo = root, config, demo
        self.service = AgentService(config)
        self.record = None
        root.title("Local Coding Agent")
        root.geometry("900x620")
        top = tk.Frame(root, padx=12, pady=12)
        top.pack(fill=tk.X)
        tk.Label(top, text="任务").pack(anchor=tk.W)
        self.task = tk.Entry(top)
        self.task.pack(fill=tk.X, pady=(4, 8))
        actions = tk.Frame(top)
        actions.pack(fill=tk.X)
        tk.Button(actions, text="开始", command=self.start).pack(side=tk.LEFT)
        self.stop_button = tk.Button(actions, text="停止", command=self.stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=6)
        tk.Button(actions, text="打开工作区", command=self.open_workspace).pack(side=tk.LEFT)
        self.status = tk.Label(actions, text="就绪", fg="#666")
        self.status.pack(side=tk.RIGHT)
        self.log = scrolledtext.ScrolledText(root, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        root.protocol("WM_DELETE_WINDOW", self.close)

    def start(self):
        if not self.task.get().strip() or self.record and self.record.status == "running":
            return
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)
        self.record = self.service.create_task(self.task.get(), demo=self.demo)
        self.event_count = 0
        self.status.config(text="运行中")
        self.stop_button.config(state=tk.NORMAL)
        self.root.after(100, self.poll)

    def poll(self):
        if not self.record:
            return
        events = self.service.events(self.record.id, getattr(self, "event_count", 0))
        self.event_count = getattr(self, "event_count", 0) + len(events)
        for event in events:
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, f"[{event.type}]\n{event.data}\n\n")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)
        if self.record.status in {"completed", "failed", "cancelled"}:
            self.status.config(text=self.record.status)
            self.stop_button.config(state=tk.DISABLED)
            return
        self.root.after(100, self.poll)

    def stop(self):
        if self.record:
            self.service.cancel_task(self.record.id)
            self.status.config(text="正在停止")

    def open_workspace(self):
        selected = filedialog.askdirectory(initialdir=str(self.config.workspace))
        if selected:
            self.config = Config.from_env(selected)
            self.service = AgentService(self.config)
            self.status.config(text=f"工作区: {Path(selected).name}")

    def close(self):
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
