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
                created_at TEXT NOT NULL,
                conversation_id TEXT,
                parent_task_id TEXT
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
            CREATE TABLE IF NOT EXISTS changesets (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );
            CREATE INDEX IF NOT EXISTS changesets_task_updated ON changesets(task_id, updated_at);
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_titles (
                conversation_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subagents (
                id TEXT PRIMARY KEY,
                parent_task_id TEXT NOT NULL,
                role TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT,
                files TEXT NOT NULL,
                changeset_ids TEXT NOT NULL DEFAULT '[]',
                operations INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(parent_task_id) REFERENCES tasks(id)
            );
            CREATE INDEX IF NOT EXISTS subagents_parent_updated ON subagents(parent_task_id, updated_at);
            """
        )
        # Existing workspaces may already have an earlier tasks table. These
        # nullable columns keep old local history readable while the service
        # derives and backfills the explicit conversation grouping.
        task_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "parent_task_id" not in task_columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN parent_task_id TEXT")
        if "conversation_id" not in task_columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN conversation_id TEXT")
        subagent_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(subagents)").fetchall()
        }
        if "changeset_ids" not in subagent_columns:
            self._connection.execute("ALTER TABLE subagents ADD COLUMN changeset_ids TEXT NOT NULL DEFAULT '[]'")
        if "duration_ms" not in subagent_columns:
            self._connection.execute("ALTER TABLE subagents ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0")
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS tasks_parent_task_id ON tasks(parent_task_id)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS tasks_conversation_created ON tasks(conversation_id, created_at)"
        )
        self._connection.commit()

    def save_task(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO tasks(id, task, status, result, error, created_at, conversation_id, parent_task_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET task=excluded.task, status=excluded.status,
                   result=excluded.result, error=excluded.error,
                   conversation_id=excluded.conversation_id,
                   parent_task_id=excluded.parent_task_id""",
                (
                    snapshot["id"],
                    snapshot["task"],
                    snapshot["status"],
                    snapshot.get("result"),
                    snapshot.get("error"),
                    snapshot["created_at"],
                    snapshot.get("conversation_id"),
                    snapshot.get("parent_task_id"),
                ),
            )
            self._connection.commit()

    def set_task_parent(self, task_id: str, parent_task_id: str) -> None:
        """Backfill a lineage edge for a task written before parent ids existed."""
        with self._lock:
            self._connection.execute(
                "UPDATE tasks SET parent_task_id = ? WHERE id = ? AND parent_task_id IS NULL",
                (parent_task_id, task_id),
            )
            self._connection.commit()

    def set_task_conversation(self, task_id: str, conversation_id: str) -> None:
        """Persist a service-derived conversation assignment for legacy history."""
        with self._lock:
            self._connection.execute(
                "UPDATE tasks SET conversation_id = ? WHERE id = ?",
                (conversation_id, task_id),
            )
            self._connection.commit()

    def set_conversation_title(self, conversation_id: str, title: str, updated_at: str) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO conversation_titles(conversation_id, title, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at""",
                (conversation_id, title, updated_at),
            )
            self._connection.commit()

    def load_conversation_titles(self) -> dict[str, str]:
        with self._lock:
            rows = self._connection.execute("SELECT conversation_id, title FROM conversation_titles").fetchall()
        return {str(row["conversation_id"]): str(row["title"]) for row in rows if row["title"]}

    def save_subagent(self, snapshot: dict[str, Any]) -> None:
        """Persist a read-only specialist result independently of event logs."""
        with self._lock:
            self._connection.execute(
                """INSERT INTO subagents(id, parent_task_id, role, task, status, summary, files, changeset_ids, operations, error, duration_ms, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status, summary=excluded.summary,
                   files=excluded.files, changeset_ids=excluded.changeset_ids, operations=excluded.operations, error=excluded.error, duration_ms=excluded.duration_ms,
                   updated_at=excluded.updated_at""",
                (
                    snapshot["id"], snapshot["parent_task_id"], snapshot["role"], snapshot["task"],
                    snapshot["status"], snapshot.get("summary"), json.dumps(snapshot.get("files", []), ensure_ascii=False),
                    json.dumps(snapshot.get("changeset_ids", []), ensure_ascii=False), int(snapshot.get("operations", 0)), snapshot.get("error"), int(snapshot.get("duration_ms", 0)), snapshot["created_at"], snapshot["updated_at"],
                ),
            )
            self._connection.commit()

    def load_subagents(self, parent_task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, parent_task_id, role, task, status, summary, files, changeset_ids, operations, error, duration_ms, created_at, updated_at "
                "FROM subagents WHERE parent_task_id = ? ORDER BY created_at",
                (parent_task_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["files"] = json.loads(item["files"])
            except (TypeError, ValueError):
                item["files"] = []
            try:
                item["changeset_ids"] = json.loads(item["changeset_ids"])
            except (TypeError, ValueError):
                item["changeset_ids"] = []
            result.append(item)
        return result

    def save_event(self, event: AgentEvent) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO events(id, task_id, sequence, timestamp, type, data) VALUES (?, ?, ?, ?, ?, ?)",
                (event.id, event.task_id, event.sequence, event.timestamp, event.type, json.dumps(event.data, ensure_ascii=False)),
            )
            self._connection.commit()

    def load_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, task, status, result, error, created_at, conversation_id, parent_task_id "
                "FROM tasks ORDER BY created_at DESC"
            ).fetchall()
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

    def save_changeset(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO changesets(id, task_id, status, mode, payload, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status, mode=excluded.mode,
                   payload=excluded.payload, updated_at=excluded.updated_at""",
                (
                    snapshot["id"],
                    snapshot["task_id"],
                    snapshot["status"],
                    snapshot["mode"],
                    json.dumps(snapshot, ensure_ascii=False),
                    snapshot["updated_at"],
                ),
            )
            self._connection.commit()

    def load_changesets(self, task_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload FROM changesets"
        arguments: list[object] = []
        if task_id is not None:
            query += " WHERE task_id = ?"
            arguments.append(task_id)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._connection.execute(query, arguments).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                result.append(value)
        return result

    def load_changeset(self, changeset_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT payload FROM changesets WHERE id = ?", (changeset_id,)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["payload"])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def set_setting(self, name: str, value: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO settings(name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, value),
            )
            self._connection.commit()

    def get_setting(self, name: str) -> str | None:
        with self._lock:
            row = self._connection.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return str(row["value"]) if row else None

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
            self._connection.execute("DELETE FROM changesets WHERE task_id = ?", (task_id,))
            self._connection.execute("DELETE FROM subagents WHERE parent_task_id = ?", (task_id,))
            self._connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._connection.commit()

    def delete_conversation_title(self, conversation_id: str) -> None:
        """Remove the display title when its complete conversation is removed."""
        with self._lock:
            self._connection.execute(
                "DELETE FROM conversation_titles WHERE conversation_id = ?",
                (conversation_id,),
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _events_from_rows(rows: list[sqlite3.Row]) -> list[AgentEvent]:
        return [AgentEvent(type=row["type"], task_id=row["task_id"], data=json.loads(row["data"]), sequence=row["sequence"], id=row["id"], timestamp=row["timestamp"]) for row in rows]
