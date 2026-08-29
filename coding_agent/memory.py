"""Persistent, project-scoped memory with lightweight relevance retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from threading import Lock


@dataclass(frozen=True)
class MemoryItem:
    id: int
    content: str
    created_at: str
    source: str


class MemoryStore:
    """Stores durable project constraints separately from task and event history."""

    _LONG_TERM_MARKERS = (
        "always", "never", "must", "must not", "do not", "project uses", "project use",
        "以后", "统一", "必须", "不要", "项目使用", "项目统一", "约定", "规则",
    )

    def __init__(self, path: Path | None):
        self.path = path
        self.error: str | None = None
        self._lock = Lock()
        self._connection: sqlite3.Connection | None = None
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(path, check_same_thread=False)
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, source TEXT NOT NULL)"
            )
            self._connection.commit()
        except (OSError, sqlite3.Error) as exc:
            self.error = f"memory storage is unavailable: {exc}"
            self._connection = None

    def list(self, limit: int = 50) -> list[MemoryItem]:
        return self._query("SELECT id, content, created_at, source FROM memories ORDER BY id DESC LIMIT ?", (max(1, limit),))

    def add(self, content: str, source: str = "manual") -> MemoryItem:
        text = content.strip()
        if not text:
            raise ValueError("memory content must not be empty")
        if len(text) > 2000:
            raise ValueError("memory content exceeds 2000 characters")
        connection = self._require_connection()
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            try:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO memories(content, created_at, source) VALUES (?, ?, ?)", (text, timestamp, source)
                )
                connection.commit()
                if cursor.lastrowid:
                    return MemoryItem(cursor.lastrowid, text, timestamp, source)
                row = connection.execute(
                    "SELECT id, content, created_at, source FROM memories WHERE content = ?", (text,)
                ).fetchone()
            except sqlite3.Error as exc:
                raise OSError(f"memory write failed: {exc}") from exc
        return MemoryItem(*row)

    def search(self, query: str, limit: int = 8) -> list[MemoryItem]:
        terms = self._terms(query)
        if not terms:
            return []
        where = " OR ".join("LOWER(content) LIKE ?" for _ in terms)
        parameters = tuple(f"%{term.lower()}%" for term in terms) + (max(1, limit),)
        return self._query(
            f"SELECT id, content, created_at, source FROM memories WHERE {where} ORDER BY id DESC LIMIT ?", parameters
        )

    def retrieve(self, task: str, limit: int = 6) -> list[MemoryItem]:
        return self.search(task, limit)

    def delete(self, item_id: int) -> bool:
        connection = self._require_connection()
        with self._lock:
            try:
                cursor = connection.execute("DELETE FROM memories WHERE id = ?", (item_id,))
                connection.commit()
                return cursor.rowcount == 1
            except sqlite3.Error as exc:
                raise OSError(f"memory delete failed: {exc}") from exc

    def extract_from_task(self, task: str) -> list[MemoryItem]:
        """Persist explicit stable project rules, not transient work or tool output."""
        candidates = re.split(r"[\n。！？.!?]+", task)
        saved: list[MemoryItem] = []
        for candidate in candidates:
            text = candidate.strip(" -\t")
            lower = text.lower()
            if 8 <= len(text) <= 500 and any(marker in lower for marker in self._LONG_TERM_MARKERS):
                try:
                    saved.append(self.add(text, source="automatic"))
                except (OSError, ValueError):
                    continue
        return saved

    def close(self) -> None:
        if self._connection:
            self._connection.close()
            self._connection = None

    def _query(self, statement: str, parameters: tuple[object, ...]) -> list[MemoryItem]:
        if not self._connection:
            return []
        with self._lock:
            try:
                rows = self._connection.execute(statement, parameters).fetchall()
            except sqlite3.Error:
                return []
        return [MemoryItem(*row) for row in rows]

    def _require_connection(self) -> sqlite3.Connection:
        if not self._connection:
            raise OSError(self.error or "memory storage is unavailable")
        return self._connection

    @staticmethod
    def _terms(query: str) -> list[str]:
        terms: list[str] = []
        for term in re.findall(r"[a-zA-Z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", query):
            terms.append(term)
            if all("\u4e00" <= char <= "\u9fff" for char in term):
                terms.extend(term[index : index + 2] for index in range(len(term) - 1))
        return list(dict.fromkeys(terms))
