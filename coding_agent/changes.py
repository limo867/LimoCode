"""Durable, conflict-aware workspace changes used by every UI.

The model never receives a direct file-system write path.  It can only propose
a change through :class:`ChangeSetManager`; the service decides when it may be
applied.  This keeps Approval and Auto modes on the same representation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import difflib
import hashlib
from pathlib import Path
from typing import Any
import uuid

from .config import Config
from .workspace import Workspace


PROPOSED = "proposed"
WAITING_APPROVAL = "waiting_approval"
APPROVED = "approved"
REJECTED = "rejected"
APPLYING = "applying"
APPLIED = "applied"
UNDOING = "undoing"
UNDONE = "undone"
CONFLICT = "conflict"
FAILED = "failed"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(content: str | None) -> str | None:
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class FileChange:
    path: str
    operation: str
    before: str | None
    after: str | None
    before_hash: str | None
    after_hash: str | None
    unified_diff: str
    added_lines: int
    removed_lines: int

    @classmethod
    def write(cls, path: str, before: str | None, after: str) -> "FileChange":
        lines = list(
            difflib.unified_diff(
                (before or "").splitlines(),
                after.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
                n=3,
            )
        )
        return cls(
            path=path,
            operation="create" if before is None else "modify",
            before=before,
            after=after,
            before_hash=_digest(before),
            after_hash=_digest(after),
            unified_diff="\n".join(lines),
            added_lines=sum(1 for line in lines if line.startswith("+") and not line.startswith("+++")),
            removed_lines=sum(1 for line in lines if line.startswith("-") and not line.startswith("---")),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileChange":
        return cls(**{key: data.get(key) for key in cls.__dataclass_fields__})

    def preview(self, limit: int) -> dict[str, Any]:
        diff = self.unified_diff
        return {
            "path": self.path,
            "operation": self.operation,
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "unified_diff": diff[:limit],
            "diff_truncated": len(diff) > limit,
        }


@dataclass
class ChangeSet:
    id: str
    task_id: str
    mode: str
    changes: list[FileChange]
    status: str = PROPOSED
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    error: str | None = None

    def snapshot(self, *, preview_limit: int = 4000, include_content: bool = False) -> dict[str, Any]:
        result = {
            "id": self.id,
            "task_id": self.task_id,
            "mode": self.mode,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "can_undo": self.status == APPLIED,
            "files": [item.preview(preview_limit) for item in self.changes],
        }
        if include_content:
            result["changes"] = [asdict(item) for item in self.changes]
        return result

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "ChangeSet":
        changes = data.get("changes") or []
        return cls(
            id=str(data["id"]),
            task_id=str(data["task_id"]),
            mode=str(data.get("mode", "approval")),
            status=str(data.get("status", PROPOSED)),
            created_at=str(data.get("created_at", _timestamp())),
            updated_at=str(data.get("updated_at", _timestamp())),
            error=data.get("error"),
            changes=[FileChange.from_dict(item) for item in changes if isinstance(item, dict)],
        )


class ChangeSetManager:
    """Prepares, applies, and undoes workspace file changes atomically."""

    def __init__(self, config: Config):
        self.config = config
        self.workspace = Workspace(config.workspace)

    def prepare_write(self, task_id: str, path: str, content: str) -> ChangeSet:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        if len(content) > self.config.max_file_chars:
            raise ValueError(f"content exceeds {self.config.max_file_chars} character limit")
        target = self.workspace.resolve(path)
        if target.exists() and not target.is_file():
            raise ValueError("path is not a regular file")
        before: str | None = None
        if target.exists():
            try:
                before = target.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("existing file is not valid UTF-8 text") from exc
        change = FileChange.write(path, before, content)
        return ChangeSet(id=uuid.uuid4().hex, task_id=task_id, mode=self.config.permission_mode, changes=[change])

    def apply(self, changeset: ChangeSet) -> ChangeSet:
        if changeset.status not in {APPROVED, PROPOSED}:
            raise ValueError(f"changeset cannot be applied from {changeset.status}")
        changeset.status = APPLYING
        changeset.updated_at = _timestamp()
        conflict = self._verify_current(changeset, undo=False)
        if conflict:
            changeset.status = CONFLICT
            changeset.error = conflict
            changeset.updated_at = _timestamp()
            return changeset
        try:
            for change in changeset.changes:
                self._write(change.path, change.after or "")
        except OSError as exc:
            changeset.status = FAILED
            changeset.error = f"could not apply changeset: {exc}"
        else:
            changeset.status = APPLIED
            changeset.error = None
        changeset.updated_at = _timestamp()
        return changeset

    def undo(self, changeset: ChangeSet) -> ChangeSet:
        if changeset.status != APPLIED:
            raise ValueError("only applied changesets can be undone")
        changeset.status = UNDOING
        changeset.updated_at = _timestamp()
        conflict = self._verify_current(changeset, undo=True)
        if conflict:
            changeset.status = CONFLICT
            changeset.error = conflict
            changeset.updated_at = _timestamp()
            return changeset
        try:
            for change in changeset.changes:
                target = self.workspace.resolve(change.path)
                if change.before is None:
                    target.unlink(missing_ok=True)
                else:
                    self._write(change.path, change.before)
        except OSError as exc:
            changeset.status = FAILED
            changeset.error = f"could not undo changeset: {exc}"
        else:
            changeset.status = UNDONE
            changeset.error = None
        changeset.updated_at = _timestamp()
        return changeset

    def _verify_current(self, changeset: ChangeSet, *, undo: bool) -> str | None:
        for change in changeset.changes:
            target = self.workspace.resolve(change.path)
            if target.exists() and not target.is_file():
                return f"conflict: {change.path} is no longer a regular file"
            current: str | None
            if target.exists():
                try:
                    current = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    return f"conflict: {change.path} is no longer UTF-8 text"
            else:
                current = None
            expected = change.after_hash if undo else change.before_hash
            if _digest(current) != expected:
                action = "undo" if undo else "apply"
                return f"conflict: {change.path} changed before {action}"
        return None

    def _write(self, path: str, content: str) -> None:
        target = self.workspace.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
