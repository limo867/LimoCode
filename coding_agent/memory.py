"""Persistent, project-scoped memory with lightweight relevance retrieval."""

from __future__ import annotations

from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class MemoryMatch:
    """One durable memory selected for a task, with retrieval diagnostics."""

    item: MemoryItem
    score: int
    matched_terms: tuple[str, ...]
    truncated: bool = False


class MemoryStore:
    """Stores durable project constraints separately from task and event history."""

    # Saving durable facts must be intentional. Broad words such as "must"
    # occur in ordinary one-off tasks and otherwise pollute project memory.
    _EXPLICIT_MEMORY_MARKERS = (
        "remember", "remember:", "remember this", "please remember",
        "\u8bb0\u4f4f", "\u957f\u671f\u8bb0\u5fc6", "\u4fdd\u5b58\u5230\u8bb0\u5fc6",
    )
    _MAX_RETRIEVAL_SCAN = 500

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

    def status(self) -> dict[str, object]:
        """Return storage health without exposing the SQLite connection."""
        if not self._connection:
            return {
                "available": False,
                "path": str(self.path) if self.path else None,
                "count": 0,
                "error": self.error or "memory storage is disabled",
            }
        with self._lock:
            try:
                row = self._connection.execute("SELECT COUNT(*) FROM memories").fetchone()
            except sqlite3.Error as exc:
                return {
                    "available": False,
                    "path": str(self.path) if self.path else None,
                    "count": 0,
                    "error": f"memory read failed: {exc}",
                }
        return {
            "available": True,
            "path": str(self.path) if self.path else None,
            "count": int(row[0]) if row else 0,
            "error": None,
        }

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
        return [match.item for match in self.retrieve_matches(query, limit=limit)]

    def retrieve(self, task: str, limit: int = 6) -> list[MemoryItem]:
        return [match.item for match in self.retrieve_matches(task, limit=limit)]

    def retrieve_matches(self, query: str, limit: int = 6, max_chars: int | None = None) -> list[MemoryMatch]:
        """Rank relevant memories and keep their combined prompt footprint bounded."""
        terms = self._terms(query)
        if not terms:
            return []
        ranked: list[MemoryMatch] = []
        for item in self.list(self._MAX_RETRIEVAL_SCAN):
            matched_terms = tuple(term for term in terms if term.lower() in item.content.lower())
            if not matched_terms:
                continue
            score = self._score_match(query, item.content, matched_terms)
            ranked.append(MemoryMatch(item=item, score=score, matched_terms=matched_terms))
        ranked.sort(key=lambda match: (-match.score, -match.item.id))

        selected: list[MemoryMatch] = []
        remaining = max_chars if max_chars is not None else None
        for match in ranked:
            if len(selected) >= max(1, limit):
                break
            if remaining is None:
                selected.append(match)
                continue
            if remaining <= 0:
                break
            content_length = len(match.item.content)
            if content_length <= remaining:
                selected.append(match)
                remaining -= content_length
                continue
            # Keep a useful prefix when the best match alone exceeds the
            # budget. Never let a single memory consume the entire prompt.
            if not selected and remaining >= 80:
                clipped = match.item.content[: max(0, remaining - 16)].rstrip() + "\n[truncated]"
                selected.append(replace(match, item=replace(match.item, content=clipped), truncated=True))
            break
        return selected

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
        """Persist only user-explicit memory requests, never generic task constraints."""
        candidates = re.split(r"[\n.!?\u3002\uff01\uff1f]+", task)
        saved: list[MemoryItem] = []
        for candidate in candidates:
            text = candidate.strip(" -\t")
            lower = text.lower()
            if 8 <= len(text) <= 500 and any(marker in lower for marker in self._EXPLICIT_MEMORY_MARKERS):
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

    @staticmethod
    def _score_match(query: str, content: str, matched_terms: tuple[str, ...]) -> int:
        """A deterministic lexical score suitable for a small local memory DB."""
        lowered_content = content.lower()
        lowered_query = query.strip().lower()
        score = 0
        for term in matched_terms:
            occurrences = lowered_content.count(term.lower())
            score += min(occurrences, 3) * (6 if len(term) >= 4 else 3)
        if len(lowered_query) >= 8 and lowered_query in lowered_content:
            score += 24
        return score
