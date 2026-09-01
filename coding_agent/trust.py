"""Persistent workspace trust decisions shared by every local UI.

The preferred store is user-local so a trust decision follows the operator
between projects and between the TUI and Web processes.  Some managed Windows
profiles expose that directory as read-only to the application sandbox.  In
that case the default store transparently falls back to a hidden, ignored file
inside the configured workspace; both UIs use the same fallback path and can
therefore still share the decision.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


TRUST_FILE_NAME = "trusted-workspaces.json"


def default_trust_store_path() -> Path:
    """Return the user-level location without placing trust state in a project."""
    try:
        return Path.home() / ".limocode" / TRUST_FILE_NAME
    except RuntimeError:
        # A missing home directory should not prevent the terminal from
        # starting. This fallback is only used when a user-level path cannot
        # be determined.
        return Path.cwd() / ".limocode" / TRUST_FILE_NAME


def canonical_workspace_path(workspace: str | Path) -> str:
    """Produce one stable key for equivalent workspace paths."""
    path = Path(workspace).expanduser()
    try:
        path = path.resolve(strict=False)
    except (OSError, RuntimeError):
        path = path.absolute()
    return os.path.normcase(os.path.normpath(str(path)))


class WorkspaceTrustStore:
    """Store explicit workspace approvals in a compact local JSON file.

    ``path`` is injectable so callers and tests can isolate trust state from
    the user's normal ``~/.limocode`` directory.
    """

    def __init__(self, path: str | Path | None = None, *, allow_workspace_fallback: bool | None = None):
        self._uses_default_path = path is None
        self.path = Path(path).expanduser() if path is not None else default_trust_store_path()
        # Explicit paths are commonly used by tests or operators who want a
        # strict, isolated store.  Only the shared default opts into the
        # workspace fallback unless explicitly requested.
        self.allow_workspace_fallback = (
            self._uses_default_path if allow_workspace_fallback is None else bool(allow_workspace_fallback)
        )
        self.last_error: str | None = None
        self.last_used_path: Path | None = None

    @staticmethod
    def workspace_fallback_path(workspace: str | Path) -> Path:
        """Return the ignored per-workspace store used when the user store is read-only."""
        path = Path(workspace).expanduser()
        try:
            path = path.resolve(strict=False)
        except (OSError, RuntimeError):
            path = path.absolute()
        return path / ".coding-agent" / TRUST_FILE_NAME

    @staticmethod
    def runtime_fallback_path() -> Path:
        """Return a user-writable fallback independent of a project ACL.

        Managed Windows profiles can deny access to both the home-based
        settings folder and a selected project's hidden state directory. The
        system temp location remains suitable for a local, shared Web/TUI
        trust record for the current Windows profile.
        """
        return Path(tempfile.gettempdir()) / "limocode" / TRUST_FILE_NAME

    def candidate_paths(self, workspace: str | Path) -> tuple[Path, ...]:
        """Return the primary and (when enabled) shared fallback locations."""
        paths = [self.path]
        if self._uses_default_path:
            legacy = self.path.parent.parent / ".local-codex" / TRUST_FILE_NAME
            if legacy.resolve(strict=False) != self.path.resolve(strict=False):
                paths.append(legacy)
            runtime = self.runtime_fallback_path()
            if runtime.resolve(strict=False) not in {item.resolve(strict=False) for item in paths}:
                paths.append(runtime)
        if self.allow_workspace_fallback:
            fallback = self.workspace_fallback_path(workspace)
            if fallback.resolve(strict=False) != self.path.resolve(strict=False):
                paths.append(fallback)
        return tuple(paths)

    def is_trusted(self, workspace: str | Path) -> bool:
        """Return whether this exact resolved workspace was explicitly trusted."""
        self.last_error = None
        self.last_used_path = None
        key = canonical_workspace_path(workspace)
        for candidate in self.candidate_paths(workspace):
            values = self._read_workspaces(candidate)
            if key in values:
                self.last_used_path = candidate
                return True
        if self.allow_workspace_fallback:
            fallback = self.workspace_fallback_path(workspace)
            fallback_values = self._read_workspaces(fallback)
            if key in fallback_values:
                self.last_used_path = fallback
                # A usable fallback is sufficient for both UIs.  Keep the
                # primary read error only when neither location can answer.
                self.last_error = None
                return True
        return False

    def trust(self, workspace: str | Path) -> bool:
        """Persist an explicit approval, returning ``False`` if storage fails."""
        key = canonical_workspace_path(workspace)
        primary_values = self._read_workspaces(self.path)
        primary_values.add(key)
        if self._write_workspaces(self.path, primary_values):
            self.last_used_path = self.path
            self.last_error = None
            return True

        if self.allow_workspace_fallback:
            if self._uses_default_path:
                runtime = self.runtime_fallback_path()
                runtime_values = self._read_workspaces(runtime)
                runtime_values.add(key)
                if self._write_workspaces(runtime, runtime_values):
                    self.last_used_path = runtime
                    self.last_error = None
                    return True
            fallback = self.workspace_fallback_path(workspace)
            fallback_values = self._read_workspaces(fallback)
            fallback_values.add(key)
            if self._write_workspaces(fallback, fallback_values):
                self.last_used_path = fallback
                self.last_error = None
                return True

        return False

    def trusted_workspaces(self) -> tuple[str, ...]:
        """Expose stored keys for diagnostics and focused tests."""
        values: set[str] = set()
        for candidate in self.candidate_paths(Path.cwd()):
            values.update(self._read_workspaces(candidate))
        return tuple(sorted(values))

    def describe(self, workspace: str | Path) -> dict[str, str | bool | None]:
        """Return the effective trust state and storage locations for a UI.

        Showing both paths is useful when a Web server was started from a
        different directory or when a managed profile forced the fallback.
        It also lets the browser report a concrete fix instead of a generic
        "request failed" toast.
        """
        trusted = self.is_trusted(workspace)
        fallback = self.workspace_fallback_path(workspace) if self.allow_workspace_fallback else None
        used = self.last_used_path or self.path
        runtime = self.runtime_fallback_path() if self._uses_default_path else None
        storage = (
            "workspace" if fallback is not None and used == fallback
            else "runtime" if runtime is not None and used == runtime
            else "user"
        )
        return {
            "trusted": trusted,
            "workspace_key": canonical_workspace_path(workspace),
            "store_path": str(self.path),
            "fallback_path": str(fallback) if fallback is not None else None,
            "effective_path": str(used),
            "storage": storage,
            "error": self.last_error,
        }

    def _load_workspaces(self) -> set[str]:
        """Backward-compatible primary-store accessor."""
        return self._read_workspaces(self.path)

    def _read_workspaces(self, path: Path) -> set[str]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        except OSError as exc:
            self.last_error = f"Unable to read workspace trust: {exc}"
            return set()
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            self.last_error = f"Unable to read workspace trust: {exc}"
            return set()
        if not isinstance(payload, dict) or not isinstance(payload.get("workspaces"), list):
            self.last_error = "Unable to read workspace trust: invalid file format"
            return set()
        # A successful read clears a stale error from an earlier candidate;
        # callers that need to distinguish primary/fallback use last_used_path.
        self.last_error = None
        return {item for item in payload["workspaces"] if isinstance(item, str)}

    def _write_workspaces(self, path: Path, workspaces: set[str]) -> bool:
        payload = {
            "version": 1,
            "workspaces": sorted(workspaces),
        }
        temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, path)
        except OSError as exc:
            self.last_error = f"Unable to save workspace trust: {exc}"
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True
