"""Open and close the desktop GUI without running an Agent task."""

import argparse
from pathlib import Path
import sys
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coding_agent.config import Config
from coding_agent.gui import AgentWindow


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local tkinter GUI smoke test")
    parser.add_argument("--workspace")
    parser.add_argument("--duration-ms", type=int, default=250)
    args = parser.parse_args()
    if args.duration_ms < 1 or args.duration_ms > 10_000:
        parser.error("--duration-ms must be between 1 and 10000")
    root = tk.Tk()
    AgentWindow(root, Config.from_env(args.workspace), demo=True)
    root.after(args.duration_ms, root.destroy)
    root.mainloop()
    print("GUI smoke test passed")


if __name__ == "__main__":
    main()
