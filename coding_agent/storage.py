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
            CREATE TABLE IF NOT EXISTS sessions (
                task_id TEXT PRIMARY KEY,
                task_context TEXT NOT NULL,
                messages TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );
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

    def load_events(self, task_id: str, after: int = 0, limit: int | None = None) -> list[AgentEvent]:
        query = "SELECT id, task_id, sequence, timestamp, type, data FROM events WHERE task_id = ? AND sequence > ? ORDER BY sequence"
        arguments: list[object] = [task_id, after]
        if limit is not None:
            query += " LIMIT ?"
            arguments.append(limit)
        with self._lock:
            rows = self._connection.execute(query, arguments).fetchall()
        return self._events_from_rows(rows)

    def load_recent_events(self, task_id: str, limit: int = 500) -> list[AgentEvent]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, task_id, sequence, timestamp, type, data FROM events WHERE task_id = ? ORDER BY sequence DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return list(reversed(self._events_from_rows(rows)))

    def last_event_sequence(self, task_id: str) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE task_id = ?", (task_id,)).fetchone()
        return int(row["sequence"])

    def save_session(self, task_id: str, task_context: str, messages: list[dict[str, Any]], updated_at: str) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO sessions(task_id, task_context, messages, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET task_context=excluded.task_context,
                   messages=excluded.messages, updated_at=excluded.updated_at""",
                (task_id, task_context, json.dumps(messages, ensure_ascii=False, default=str), updated_at),
            )
            self._connection.commit()

    def load_session(self, task_id: str) -> tuple[str, list[dict[str, Any]]] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT task_context, messages FROM sessions WHERE task_id = ?", (task_id,)
            ).fetchone()
        if not row:
            return None
        try:
            messages = json.loads(row["messages"])
        except (TypeError, ValueError):
            return None
        if not isinstance(messages, list) or not all(isinstance(message, dict) for message in messages):
            return None
        return str(row["task_context"]), messages

    def delete_task(self, task_id: str) -> None:
        """Remove a pruned task together with its persisted public events."""
        with self._lock:
            self._connection.execute("DELETE FROM events WHERE task_id = ?", (task_id,))
            self._connection.execute("DELETE FROM sessions WHERE task_id = ?", (task_id,))
            self._connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _events_from_rows(rows: list[sqlite3.Row]) -> list[AgentEvent]:
        return [AgentEvent(type=row["type"], task_id=row["task_id"], data=json.loads(row["data"]), sequence=row["sequence"], id=row["id"], timestamp=row["timestamp"]) for row in rows]
