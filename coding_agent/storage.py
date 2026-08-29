"""SQLite persistence for task snapshots and public events."""

import json
import sqlite3
from threading import Lock
from pathlib import Path
from typing import Any

from .events import AgentEvent


class TaskStore:
    def __init__(self, path: Path):
        self.path = path
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = Lock()
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                type TEXT NOT NULL,
                data TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );
            CREATE INDEX IF NOT EXISTS events_task_sequence ON events(task_id, sequence);
            """
        )
        self._connection.commit()

    def save_task(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO tasks(id, task, status, result, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET task=excluded.task, status=excluded.status,
                   result=excluded.result, error=excluded.error""",
                (snapshot["id"], snapshot["task"], snapshot["status"], snapshot.get("result"), snapshot.get("error"), snapshot["created_at"]),
            )
            self._connection.commit()

    def save_event(self, event: AgentEvent) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO events(id, task_id, sequence, timestamp, type, data) VALUES (?, ?, ?, ?, ?, ?)",
                (event.id, event.task_id, event.sequence, event.timestamp, event.type, json.dumps(event.data, ensure_ascii=False)),
            )
            self._connection.commit()

    def load_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT id, task, status, result, error, created_at FROM tasks ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def load_events(self, task_id: str) -> list[AgentEvent]:
        with self._lock:
            rows = self._connection.execute("SELECT id, task_id, sequence, timestamp, type, data FROM events WHERE task_id = ? ORDER BY sequence", (task_id,)).fetchall()
        return [AgentEvent(type=row["type"], task_id=row["task_id"], data=json.loads(row["data"]), sequence=row["sequence"], id=row["id"], timestamp=row["timestamp"]) for row in rows]

    def delete_task(self, task_id: str) -> None:
        """Remove a pruned task together with its persisted public events."""
        with self._lock:
            self._connection.execute("DELETE FROM events WHERE task_id = ?", (task_id,))
            self._connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()
