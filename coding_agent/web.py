"""Minimal local Web API and SSE server; no Agent framework is involved."""

import json
import hashlib
import os
import sqlite3
import subprocess
import tempfile
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config
from .models import ModelManager
from .service import AgentService
from .trust import WorkspaceTrustStore
from .workspace import workspace_file_preview, workspace_image_bytes, workspace_tree


def _select_directory_with_windows_dialog() -> str:
    """Open a real Windows folder picker from a worker-safe STA process.

    Tk dialogs are not reliable when created from ``ThreadingHTTPServer``
    worker threads.  PowerShell's WinForms dialog runs in its own STA process
    and therefore remains visible on the user's desktop session.
    """
    legacy_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$d.Description = '选择 LimoCode 工作区'; "
        "$d.ShowNewFolderButton = $true; "
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Write($d.SelectedPath) }"
    )
    # Use an ASCII script: older source text could contain a malformed
    # localized quote and make PowerShell wait indefinitely for input.
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$dialog.Description = 'Select LimoCode workspace'; "
        "$dialog.ShowNewFolderButton = $true; "
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($dialog.SelectedPath) }"
    )
    # The web server itself is launched hidden.  Explicitly reset the child
    # window state so Windows does not inherit that hidden flag and place the
    # modal dialog behind an invisible console process.
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        # SW_SHOWNORMAL is a Win32 ShowWindow constant (1), not an attribute
        # exposed by Python's subprocess module.
        startupinfo.wShowWindow = 1
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=90,
        startupinfo=startupinfo,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail or f"folder picker exited with code {completed.returncode}")
    return completed.stdout.strip()


def _select_directory() -> str:
    """Return a selected directory, using the native Windows picker first."""
    if os.name == "nt":
        return _select_directory_with_windows_dialog()
    import tkinter as tk
    from tkinter import TclError, filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update()
        return filedialog.askdirectory(parent=root, title="选择 LimoCode 工作区") or ""
    except TclError:
        raise
    finally:
        root.destroy()


class ApiConflictError(ValueError):
    """A valid request that cannot run in the service's current state."""


class WorkspaceServiceRegistry:
    """Own one isolated AgentService for each Web-selected local workspace.

    A browser selection must never mutate the process-wide default service:
    another tab may still be streaming a task in the original folder.  Each
    selected directory instead receives its own AgentService, SQLite state,
    skill roots, memory store, and command working directory.
    """

    def __init__(self, config: Config, *, demo: bool = False, service: AgentService | None = None):
        self.base_config = config
        self.demo = bool(demo)
        self._lock = Lock()
        if service is not None:
            initial = service
        else:
            try:
                initial = AgentService(config)
            except (OSError, sqlite3.Error):
                initial = AgentService(self._fallback_config_for(config.workspace))
        initial.demo_default = self.demo
        self._services: dict[str, AgentService] = {self._key(config.workspace): initial}

    @staticmethod
    def _key(workspace: Path) -> str:
        return os.path.normcase(str(workspace.resolve()))

    def _resolve_workspace(self, value: str | Path | None) -> Path:
        if value is None:
            return self.base_config.workspace.resolve()
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("workspace must be a non-empty path")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.base_config.workspace / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"workspace is unavailable: {exc}") from exc
        if not resolved.is_dir():
            raise ValueError("workspace must be an existing directory")
        return resolved

    def resolve_workspace(self, value: str | Path | None) -> Path:
        """Validate and canonicalize a workspace without creating a service."""
        return self._resolve_workspace(value)

    def _workspace_database(self, value: Path | None, workspace: Path, default_name: str) -> Path:
        if value is None:
            return workspace / ".coding-agent" / default_name
        try:
            relative = value.resolve().relative_to(self.base_config.workspace.resolve())
        except ValueError:
            # An external database can be a valid default-service setting,
            # but reusing it for another selected project would merge memory
            # and conversations across workspace boundaries.
            return workspace / ".coding-agent" / default_name
        # Older programmatic callers sometimes place state files directly in
        # the startup workspace.  A newly selected project should still keep
        # its generated state hidden under the standard local directory.
        if relative == Path(default_name):
            return workspace / ".coding-agent" / default_name
        return workspace / relative

    def _config_for(self, workspace: Path) -> Config:
        return replace(
            self.base_config,
            workspace=workspace,
            history_db=self._workspace_database(
                self.base_config.history_db,
                workspace,
                "tasks.sqlite3",
            ),
            memory_db=self._workspace_database(
                self.base_config.memory_db,
                workspace,
                "memory.sqlite3",
            ),
        )

    def _fallback_config_for(self, workspace: Path) -> Config:
        """Use a user-owned state directory when a project cannot host SQLite.

        Some folders are read-only (or are on a mount with restrictive ACLs).
        Conversation persistence should not make the entire Web API unusable;
        keep the workspace itself unchanged and isolate its state by path.
        """
        digest = hashlib.sha256(str(workspace).encode("utf-8", "surrogatepass")).hexdigest()[:20]
        state_root = None
        for base in (Path.home() / ".limocode", Path(tempfile.gettempdir()) / "limocode"):
            candidate = base / "workspaces" / digest
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                state_root = candidate
                break
            except OSError:
                continue
        if state_root is None:
            # Let sqlite report the underlying failure if neither location is
            # writable; this is preferable to silently losing persistence.
            state_root = Path(tempfile.gettempdir()) / "limocode" / "workspaces" / digest
        return replace(
            self.base_config,
            workspace=workspace,
            history_db=state_root / "tasks.sqlite3",
            memory_db=state_root / "memory.sqlite3",
        )

    def config_for(self, workspace: str | Path | None) -> Config:
        """Return the isolated configuration for a validated workspace."""
        return self._config_for(self._resolve_workspace(workspace))

    def existing_service(self, workspace: str | Path | None) -> AgentService | None:
        """Look up an already-open service without creating SQLite state."""
        resolved = self._resolve_workspace(workspace)
        with self._lock:
            return self._services.get(self._key(resolved))

    def known_workspaces(self) -> tuple[Path, ...]:
        """Return workspaces opened during this local server's lifetime."""
        with self._lock:
            return tuple(service.config.workspace for service in self._services.values())

    def get(self, workspace: str | Path | None = None) -> AgentService:
        resolved = self._resolve_workspace(workspace)
        key = self._key(resolved)
        with self._lock:
            service = self._services.get(key)
            if service is None:
                try:
                    service = AgentService(self._config_for(resolved))
                except (OSError, sqlite3.Error):
                    # A read-only workspace must still be browsable and usable.
                    service = AgentService(self._fallback_config_for(resolved))
                service.demo_default = self.demo
                self._services[key] = service
            return service

    def close(self) -> None:
        with self._lock:
            services = list(self._services.values())
            self._services.clear()
        for service in services:
            service.memory_store.close()
            if service.store:
                service.store.close()


class ApiHandler(BaseHTTPRequestHandler):
    service: AgentService
    workspace_registry: WorkspaceServiceRegistry | None = None
    # The decision is deliberately independent from task execution. The web
    # client can ask for and record consent, while existing API clients remain
    # backward compatible and are not unexpectedly blocked.
    trust_store: WorkspaceTrustStore | None = None
    frontend_root = Path(__file__).resolve().parent.parent / "frontend"

    def _send_json(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def _body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1_000_000:
            raise ValueError("request body is too large")
        body = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        return body

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if parsed.path == "/api/workspaces/file":
            try:
                workspace = self._workspace_from_query(parsed)
                registry = self._registry()
                resolved_workspace = registry.resolve_workspace(workspace)
                self._require_trusted_workspace(resolved_workspace)
                service = registry.get(resolved_workspace)
                path = parse_qs(parsed.query).get("path", [""])[0]
                payload = workspace_file_preview(service.config.workspace, path)
                payload["workspace"] = str(service.config.workspace)
                self._send_json(200, payload)
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except (ValueError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/workspaces/file/raw":
            try:
                workspace = self._workspace_from_query(parsed)
                registry = self._registry()
                resolved_workspace = registry.resolve_workspace(workspace)
                self._require_trusted_workspace(resolved_workspace)
                service = registry.get(resolved_workspace)
                path = parse_qs(parsed.query).get("path", [""])[0]
                data, content_type = workspace_image_bytes(service.config.workspace, path)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except (ValueError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/workspaces/tree":
            try:
                workspace = self._workspace_from_query(parsed)
                # Resolve and check consent before creating an AgentService;
                # merely previewing an untrusted folder must remain read-only.
                registry = self._registry()
                resolved_workspace = registry.resolve_workspace(workspace)
                self._require_trusted_workspace(resolved_workspace)
                service = registry.get(resolved_workspace)
                query = parse_qs(parsed.query)
                path = query.get("path", [""])[0]
                if not isinstance(path, str):
                    raise ValueError("path must be a string")
                limit = self._limit_argument(parsed, default_limit=200, maximum_limit=500)
                payload = workspace_tree(service.config.workspace, path, limit=limit)
                payload["workspace"] = str(service.config.workspace)
                self._send_json(200, payload)
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/workspaces":
            try:
                workspace = self._workspace_from_query(parsed)
                if workspace is None:
                    self._send_json(200, self._workspaces_payload())
                else:
                    # Keep the original query form as a runtime-status API
                    # without creating a service or SQLite files merely by
                    # inspecting an untrusted directory.
                    self._send_json(200, self._inspect_workspace(workspace))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if parsed.path in {"/api/status", "/api/workspace"}:
            try:
                workspace = self._workspace_from_query(parsed)
                if workspace is None:
                    self._send_json(200, self._runtime_status())
                else:
                    self._send_json(200, self._inspect_workspace(workspace))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/health":
            # Include a small runtime fingerprint so the browser and operator
            # can verify which local process answered a request when several
            # launchers or stale tabs are involved.
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "LimoCode",
                    "pid": os.getpid(),
                    "workspace": str(self._registry().base_config.workspace),
                },
            )
            return
        if parsed.path == "/api/permissions":
            try:
                service = self._service_for_workspace(self._workspace_from_query(parsed))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, service.permission_status())
            return
        if parsed.path == "/api/changesets":
            try:
                service = self._service_for_workspace(self._workspace_from_query(parsed))
                task_id = parse_qs(parsed.query).get("task_id", [None])[0]
                if task_id is not None and not isinstance(task_id, str):
                    raise ValueError("task_id must be a string")
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"changesets": service.list_changesets(task_id=task_id or None)})
            return
        if len(parts) == 3 and parts[:2] == ["api", "changesets"] and parts[2]:
            try:
                service = self._service_for_workspace(self._workspace_from_query(parsed))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            changeset = service.get_changeset(parts[2])
            if not changeset:
                self._send_json(404, {"error": "changeset not found"})
                return
            self._send_json(200, changeset)
            return
        if parsed.path == "/api/models":
            try:
                self._send_json(200, self._models_payload(self._service_for_workspace(self._workspace_from_query(parsed))))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/skills":
            try:
                self._send_json(200, self._skills_payload(self._service_for_workspace(self._workspace_from_query(parsed))))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/memory/status":
            query = parse_qs(parsed.query)
            task_id = query.get("task_id", [None])[0]
            if task_id is not None and not isinstance(task_id, str):
                self._send_json(400, {"error": "task_id must be a string"})
                return
            try:
                service = self._service_for_task(task_id) if task_id else self._service_for_workspace(self._workspace_from_query(parsed))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                payload = (service or self.service).memory_status(task_id=task_id or None)
            except sqlite3.Error as exc:
                self._send_json(503, {"error": f"workspace state is unavailable: {exc}"})
                return
            self._send_json(200, payload)
            return
        if parsed.path == "/api/memory":
            try:
                limit = self._limit_argument(parsed, default_limit=50, maximum_limit=100)
                service = self._service_for_workspace(self._workspace_from_query(parsed))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            query = parse_qs(parsed.query).get("query", [""])[0].strip()
            try:
                items = service.search_memories(query, limit=limit) if query else service.list_memories(limit=limit)
            except sqlite3.Error as exc:
                self._send_json(503, {"error": f"workspace memory is unavailable: {exc}"})
                return
            self._send_json(
                200,
                {
                    "memories": [self._memory_payload(item) for item in items],
                    "query": query or None,
                    "limit": limit,
                },
            )
            return
        if parsed.path == "/api/conversations":
            try:
                limit, offset = self._page_arguments(parsed, default_limit=50, maximum_limit=100)
                resumable_only = self._boolean_query_argument(parsed, "resumable", default=False)
                workspace = self._workspace_from_query(parsed)
                service = self._service_for_workspace(None) if workspace is None else self._trusted_service_for_workspace(workspace)
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
                return
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            conversations = self._list_conversations(
                service,
                limit=limit,
                offset=offset,
                resumable_only=resumable_only,
            )
            self._send_json(
                200,
                {
                    "conversations": [self._conversation_payload(item, include_tasks=False) for item in conversations],
                    "limit": limit,
                    "offset": offset,
                    "resumable": resumable_only,
                    "next_offset": offset + len(conversations) if len(conversations) == limit else None,
                },
            )
            return
        if parsed.path == "/api/tasks":
            try:
                limit, offset = self._page_arguments(parsed, default_limit=50, maximum_limit=100)
                service = self._service_for_workspace(self._workspace_from_query(parsed))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            tasks = [
                self._task_payload(service, record)
                for record in [service.get_task(item["id"]) for item in service.list_tasks(limit=limit, offset=offset)]
                if record
            ]
            self._send_json(200, {"tasks": tasks, "limit": limit, "offset": offset, "next_offset": offset + len(tasks) if len(tasks) == limit else None})
            return
        if len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "files" and parts[2]:
            try:
                service, conversation = self._conversation_owner(
                    parts[2],
                    workspace=self._workspace_from_query(parsed),
                )
                self._require_trusted_workspace(service.config.workspace)
                query = parse_qs(parsed.query)
                path = query.get("path", [""])[0]
                if not isinstance(path, str):
                    raise ValueError("path must be a string")
                limit = self._limit_argument(parsed, default_limit=200, maximum_limit=500)
                payload = workspace_tree(service.config.workspace, path, limit=limit)
                payload.update({"workspace": str(service.config.workspace), "conversation_id": conversation["id"]})
                self._send_json(200, payload)
            except LookupError:
                self._send_json(404, {"error": "conversation not found"})
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if len(parts) == 3 and parts[:2] == ["api", "conversations"] and parts[2]:
            try:
                workspace = self._workspace_from_query(parsed)
                _service, conversation = self._conversation_owner(parts[2], workspace=workspace)
            except LookupError:
                self._send_json(404, {"error": "conversation not found"})
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            else:
                self._send_json(200, self._conversation_payload(conversation, include_tasks=True))
            return
        if len(parts) == 3 and parts[:2] == ["api", "tasks"] and parts[2]:
            service = self._service_for_task(parts[2])
            record = service.get_task(parts[2]) if service else None
            if not record:
                self._send_json(404, {"error": "task not found"})
            else:
                self._send_json(200, self._task_payload(service, record))
            return
        if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "subagents":
            service = self._service_for_task(parts[2])
            record = service.get_task(parts[2]) if service else None
            if not record:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, {"subagents": service.subagents(parts[2])})
            return
        if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "events":
            self._stream_events(parts[2])
            return
        if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "event-log":
            self._event_log(parts[2], parsed)
            return
        if parsed.path.startswith("/assets/"):
            asset_name = parsed.path.removeprefix("/assets/")
            if asset_name and Path(asset_name).name == asset_name:
                suffix = Path(asset_name).suffix.lower()
                content_type = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".map": "application/json; charset=utf-8"}.get(suffix, "application/octet-stream")
                self._serve_file(f"assets/{asset_name}", content_type)
            else:
                self._send_json(404, {"error": "not found"})
            return
        if parsed.path in {"/", "/index.html"}:
            self._serve_file("index.html", "text/html; charset=utf-8")
            return
        self._send_json(404, {"error": "not found"})

    def _runtime_status(self, service: AgentService | None = None) -> dict[str, Any]:
        service = service or self.service
        payload = self._runtime_status_for_config(
            service.config,
            demo=bool(getattr(service, "demo_default", False)),
        )
        payload["permissions"] = service.permission_status()
        return payload

    def _runtime_status_for_config(self, config: Config, *, demo: bool) -> dict[str, Any]:
        manager = ModelManager(config)
        current_model = self._model_payload(manager.current())
        available_models = [self._model_payload(model) for model in manager.available()]
        models = {
            "current": current_model,
            "available_models": [model["name"] for model in available_models],
        }
        trust_store = self._trust_store()
        trust = trust_store.describe(config.workspace)
        return {
            "workspace": str(config.workspace),
            "trusted": trust["trusted"],
            "trust_error": trust["error"],
            "trust": trust,
            "demo": demo,
            "model": config.model,
            "model_info": models["current"],
            "available_models": models["available_models"],
            # Do not expose api_key. These are the settings a local UI can
            # display or use to explain runtime limits.
            "config": {
                "base_url": config.base_url,
                "model_timeout": config.model_timeout,
                "max_turns": config.max_turns,
                "command_timeout": config.command_timeout,
                "command_approval_timeout": config.command_approval_timeout,
                "model_min_request_interval_ms": config.model_min_request_interval_ms,
                "max_context_tokens": config.max_context_tokens,
                "compaction_threshold": config.compaction_threshold,
                "memory_context_chars": config.memory_context_chars,
                "permission_mode": config.permission_mode,
            },
        }

    def _inspect_workspace(self, workspace: str | Path | None) -> dict[str, Any]:
        """Return selector metadata without initializing an AgentService.

        Picking a folder is a read-only UI action.  In particular, it must
        not create a ``.coding-agent`` directory or SQLite database before
        the operator explicitly starts work there.
        """
        registry = self._registry()
        resolved = registry.resolve_workspace(workspace)
        service = registry.existing_service(resolved)
        if service is not None:
            return self._runtime_status(service)
        return self._runtime_status_for_config(
            registry.config_for(resolved),
            demo=registry.demo,
        )

    def _workspaces_payload(self) -> dict[str, Any]:
        """List the default, trusted, and already-open local workspaces."""
        registry = self._registry()
        candidates: list[str | Path] = [registry.base_config.workspace]
        candidates.extend(registry.known_workspaces())
        candidates.extend(self._trust_store().trusted_workspaces())

        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                resolved = registry.resolve_workspace(candidate)
            except ValueError:
                # A project can be deleted or moved after it was trusted.
                # It is not selectable until its path exists again.
                continue
            key = registry._key(resolved)
            if key in seen:
                continue
            seen.add(key)
            status = self._inspect_workspace(resolved)
            entries.append(
                {
                    "path": status["workspace"],
                    "workspace": status["workspace"],
                    "name": resolved.name or str(resolved),
                    "trusted": bool(status["trusted"]),
                    "available": True,
                }
            )

        default_path = str(registry.base_config.workspace.resolve())
        entries.sort(key=lambda item: (item["path"] != default_path, item["name"].casefold(), item["path"].casefold()))
        return {"default_workspace": default_path, "workspaces": entries}

    def _models_payload(self, service: AgentService | None = None) -> dict[str, Any]:
        manager = ModelManager((service or self.service).config)
        current = self._model_payload(manager.current())
        available = [self._model_payload(model) for model in manager.available()]
        return {
            "current": current,
            "models": available,
            "available_models": [model["name"] for model in available],
        }

    def _skills_payload(self, service: AgentService | None = None) -> dict[str, Any]:
        service = service or self.service
        selected = set(service.selected_skills)
        skills = [
            {
                "name": skill.name,
                "description": skill.description,
                "selected": skill.name in selected,
            }
            for skill in service.skill_manager.metadata()
        ]
        return {
            "skills": skills,
            "selected_skills": list(service.selected_skills),
            "mode": "manual" if service.selected_skills else "auto",
        }

    @staticmethod
    def _model_payload(model: Any) -> dict[str, Any]:
        return {
            "name": model.name,
            "provider": model.provider,
            "context_window": model.context_window,
        }

    @staticmethod
    def _memory_payload(item: Any) -> dict[str, Any]:
        return {
            "id": item.id,
            "content": item.content,
            "created_at": item.created_at,
            "source": item.source,
        }

    @staticmethod
    def _conversation_payload(conversation: dict[str, Any], *, include_tasks: bool) -> dict[str, Any]:
        if include_tasks:
            return dict(conversation)
        return {key: value for key, value in conversation.items() if key != "tasks"}

    def _registry(self) -> WorkspaceServiceRegistry:
        registry = type(self).workspace_registry
        default_key = WorkspaceServiceRegistry._key(self.service.config.workspace)
        if registry is None or registry._services.get(default_key) is not self.service:
            registry = WorkspaceServiceRegistry(self.service.config, service=self.service)
            type(self).workspace_registry = registry
        return registry

    def _service_for_workspace(self, workspace: str | Path | None) -> AgentService:
        return self._registry().get(workspace)

    def _trusted_service_for_workspace(self, workspace: str | Path) -> AgentService:
        """Open project state only after explicit local-workspace consent."""
        registry = self._registry()
        resolved = registry.resolve_workspace(workspace)
        self._require_trusted_workspace(resolved)
        return registry.get(resolved)

    def _workspace_from_query(self, parsed) -> str | None:
        raw = parse_qs(parsed.query).get("workspace", [None])[0]
        if raw is not None and not isinstance(raw, str):
            raise ValueError("workspace must be a string")
        return raw

    def _all_services(self) -> list[AgentService]:
        registry = self._registry()
        with registry._lock:
            return list(registry._services.values())

    def _list_conversations(
        self,
        service: AgentService,
        *,
        limit: int,
        offset: int,
        resumable_only: bool,
    ) -> list[dict[str, Any]]:
        return [
            {**item, "workspace": str(service.config.workspace)}
            for item in service.list_conversations(
                limit=limit,
                offset=offset,
                resumable_only=resumable_only,
            )
        ]

    def _conversation_owner(
        self,
        conversation_id: str,
        *,
        workspace: str | Path | None = None,
    ) -> tuple[AgentService, dict[str, Any]]:
        """Find a conversation, optionally in its explicit workspace.

        A workspace query makes restored browser URLs deterministic after a
        Web server restart: only the named, already trusted project is opened.
        The no-workspace form remains compatible with existing local clients
        and only searches services that have already been initialized.
        """
        if workspace is not None:
            resolved = self._registry().resolve_workspace(workspace)
            self._require_trusted_workspace(resolved)
            services = [self._registry().get(resolved)]
        else:
            services = self._all_services()
        for service in services:
            for conversation in service.list_conversations():
                if conversation["id"] == conversation_id:
                    entry = dict(conversation)
                    entry["workspace"] = str(service.config.workspace)
                    return service, entry
        raise LookupError(conversation_id)

    def _service_for_task(self, task_id: str) -> AgentService | None:
        for service in self._all_services():
            if service.get_task(task_id):
                return service
        return None

    def _conversation(self, conversation_id: str) -> dict[str, Any] | None:
        try:
            _service, conversation = self._conversation_owner(conversation_id)
        except LookupError:
            return None
        return conversation

    def _trust_store(self) -> WorkspaceTrustStore:
        store = type(self).trust_store
        if store is None:
            store = WorkspaceTrustStore()
            type(self).trust_store = store
        return store

    def _require_trusted_workspace(self, workspace: Path) -> None:
        if not self._trust_store().is_trusted(workspace):
            raise PermissionError("workspace must be trusted before browsing files or starting tasks")

    @staticmethod
    def _task_payload(service: AgentService, record: Any) -> dict[str, Any]:
        payload = record.snapshot()
        payload["workspace"] = str(service.config.workspace)
        return payload

    @staticmethod
    def _page_arguments(parsed, *, default_limit: int, maximum_limit: int) -> tuple[int, int]:
        query = parse_qs(parsed.query)
        try:
            limit = int(query.get("limit", [str(default_limit)])[0])
            offset = int(query.get("offset", ["0"])[0])
        except (TypeError, ValueError) as exc:
            raise ValueError("limit and offset must be integers") from exc
        if not 1 <= limit <= maximum_limit:
            raise ValueError(f"limit must be between 1 and {maximum_limit}")
        if offset < 0:
            raise ValueError("offset must not be negative")
        return limit, offset

    @staticmethod
    def _limit_argument(parsed, *, default_limit: int, maximum_limit: int) -> int:
        query = parse_qs(parsed.query)
        try:
            limit = int(query.get("limit", [str(default_limit)])[0])
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if not 1 <= limit <= maximum_limit:
            raise ValueError(f"limit must be between 1 and {maximum_limit}")
        return limit

    @staticmethod
    def _boolean_query_argument(parsed, name: str, *, default: bool) -> bool:
        raw = parse_qs(parsed.query).get(name)
        if raw is None:
            return default
        value = raw[0].strip().lower()
        if value in {"1", "true", "yes"}:
            return True
        if value in {"0", "false", "no"}:
            return False
        raise ValueError(f"{name} must be a boolean")

    def _event_log(self, task_id: str, parsed) -> None:
        service = self._service_for_task(task_id)
        if service is None or not service.get_task(task_id):
            self._send_json(404, {"error": "task not found"})
            return
        query = parse_qs(parsed.query)
        try:
            after = int(query.get("after", ["0"])[0])
            if after < 0:
                raise ValueError("after must not be negative")
            limit, _ = self._page_arguments(parsed, default_limit=100, maximum_limit=500)
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        events = service.events(task_id, after, limit=limit)
        self._send_json(200, {"events": [event.to_dict() for event in events], "after": after, "limit": limit, "next_after": events[-1].sequence if len(events) == limit else None})

    def _stream_events(self, task_id: str) -> None:
        service = self._service_for_task(task_id)
        record = service.get_task(task_id) if service else None
        if not record:
            self._send_json(404, {"error": "task not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        query = parse_qs(urlparse(self.path).query)
        try:
            sent = max(0, int(query.get("after", ["0"])[0]))
        except (TypeError, ValueError):
            sent = 0
        while True:
            events = service.events(task_id, sent)
            for event in events:
                payload = json.dumps(event.to_dict(), ensure_ascii=False)
                self.wfile.write(f"id: {event.id}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                sent = event.sequence
            if record.status in {"completed", "failed", "cancelled"} and not service.events(task_id, sent):
                break
            self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()
            if record.cancelled.wait(0.25):
                continue

    def _serve_file(self, name: str, content_type: str) -> None:
        # The established single-page workbench remains the production UI
        # until its full workflow has been migrated. Built Vite assets are
        # intentionally not selected here: a partial migration must not hide
        # workspace selection, file browsing, and the original composer.
        target = self.frontend_root / name
        if not target.is_file():
            self._send_json(404, {"error": "frontend is not built"})
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # The UI is a single local HTML file. Do not let a browser keep a
        # pre-fix copy after the server has been restarted during development.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if parsed.path == "/api/workspaces/select-directory":
            # The browser cannot reveal an absolute directory path from a
            # regular file input. This local-only server can instead open the
            # operating system picker and return the selected folder for the
            # existing workspace validation/trust flow.
            try:
                self._body()
                selected = _select_directory()
            except Exception as exc:
                self._send_json(503, {"error": f"unable to open the local folder picker: {exc}"})
                return
            self._send_json(200, {"cancelled": not bool(selected), "workspace": str(selected) if selected else None})
            return
        if parsed.path == "/api/permissions":
            try:
                body = self._body()
                service = self._service_for_workspace(body.get("workspace"))
                mode = body.get("mode")
                if not isinstance(mode, str):
                    raise ValueError("mode must be a string")
                payload = service.set_permission_mode(mode)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(409 if "while a task is active" in str(exc) else 400, {"error": str(exc)})
                return
            self._send_json(200, payload)
            return
        if len(parts) == 5 and parts[:2] == ["api", "tasks"] and parts[3] == "change-approvals":
            try:
                body = self._body()
                approved = body.get("approved")
                if not isinstance(approved, bool):
                    raise ValueError("approved must be a boolean")
                service = self._service_for_task(parts[2])
                accepted = service.approve_changeset(parts[2], parts[4], approved) if service else False
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if not accepted:
                self._send_json(404, {"error": "change approval not found or already resolved"})
                return
            self._send_json(202, {"ok": True, "status": "change approval recorded"})
            return
        if len(parts) == 4 and parts[:2] == ["api", "changesets"] and parts[3] == "undo" and parts[2]:
            try:
                body = self._body()
                service = self._service_for_workspace(body.get("workspace"))
                changeset = service.undo_changeset(parts[2])
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if not changeset:
                self._send_json(404, {"error": "changeset not found"})
                return
            self._send_json(409 if changeset["status"] == "conflict" else 200, changeset)
            return
        if len(parts) == 5 and parts[:2] == ["api", "tasks"] and parts[3] == "approvals":
            try:
                body = self._body()
                approved = body.get("approved")
                scope = body.get("scope", "once")
                if not isinstance(approved, bool):
                    raise ValueError("approved must be a boolean")
                if scope not in {"once", "always"}:
                    raise ValueError("scope must be 'once' or 'always'")
                service = self._service_for_task(parts[2])
                accepted = service.approve_command(parts[2], parts[4], approved, scope=scope) if service else False
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if not accepted:
                self._send_json(404, {"error": "approval not found or already resolved"})
                return
            self._send_json(202, {"ok": True, "status": "approval recorded"})
            return
        if parsed.path == "/api/workspaces/trust":
            try:
                body = self._body()
                if body.get("trusted") is not True:
                    raise ValueError("trusted must be true when recording workspace trust")
                workspace = body.get("workspace")
                diagnostic = self._inspect_workspace(workspace)
                store = self._trust_store()
                trusted = store.trust(Path(str(diagnostic["workspace"])))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if not trusted:
                error = store.last_error or "unable to save workspace trust"
                diagnostic.update({"error": error, "trust_error": error})
                if isinstance(diagnostic.get("trust"), dict):
                    diagnostic["trust"]["error"] = error
                self._send_json(500, diagnostic)
                return
            self._send_json(200, self._inspect_workspace(workspace))
            return
        if parsed.path == "/api/workspaces/inspect":
            try:
                body = self._body()
                workspace = body.get("workspace")
                if not isinstance(workspace, str) or not workspace.strip():
                    raise ValueError("workspace must be a non-empty path")
                self._send_json(200, self._inspect_workspace(workspace))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/workspace/trust":
            try:
                body = self._body()
                if "trusted" in body and body["trusted"] is not True:
                    raise ValueError("trusted must be true when recording workspace trust")
                store = self._trust_store()
                trusted = store.trust(self.service.config.workspace)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if not trusted:
                # Keep the diagnostic payload on failure as well.  A browser
                # can show the actual workspace/store paths without having
                # to issue a second request (and non-browser clients get the
                # same actionable error context).
                error = store.last_error or "unable to save workspace trust"
                diagnostic = self._runtime_status()
                diagnostic["error"] = error
                diagnostic["trust_error"] = error
                if isinstance(diagnostic.get("trust"), dict):
                    diagnostic["trust"]["error"] = error
                self._send_json(500, diagnostic)
                return
            # Return the same diagnostic payload as /api/status so the browser
            # immediately reflects a user-store or workspace-fallback save.
            self._send_json(200, self._runtime_status())
            return
        if parsed.path == "/api/models/select":
            try:
                body = self._body()
                model = body.get("model")
                if not isinstance(model, str):
                    raise ValueError("model must be a string")
                service = self._service_for_workspace(body.get("workspace"))
                config = ModelManager(service.config).switch(model)
                try:
                    service.update_config(config)
                except ValueError as exc:
                    if "while a task is active" in str(exc):
                        raise ApiConflictError(str(exc)) from exc
                    raise
            except ApiConflictError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except sqlite3.Error as exc:
                self._send_json(503, {"error": f"workspace model state is unavailable: {exc}"})
                return
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, self._models_payload(service))
            return
        if parsed.path == "/api/skills/select":
            try:
                body = self._body()
                service = self._service_for_workspace(body.get("workspace"))
                names = self._skill_names_from_body(body)
                service.set_selected_skills(names)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except sqlite3.Error as exc:
                self._send_json(503, {"error": f"workspace skill state is unavailable: {exc}"})
                return
            self._send_json(200, self._skills_payload(service))
            return
        if parsed.path == "/api/skills/reload":
            try:
                body = self._body()
                service = self._service_for_workspace(body.get("workspace"))
                service.reload_skills()
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except sqlite3.Error as exc:
                self._send_json(503, {"error": f"workspace skill state is unavailable: {exc}"})
                return
            self._send_json(200, self._skills_payload(service))
            return
        if parsed.path == "/api/memory":
            try:
                body = self._body()
                content = body.get("content")
                if not isinstance(content, str):
                    raise ValueError("content must be a string")
                service = self._service_for_workspace(body.get("workspace"))
                item = service.add_memory(content)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError as exc:
                self._send_json(503, {"error": str(exc)})
                return
            except sqlite3.Error as exc:
                self._send_json(503, {"error": f"workspace memory is unavailable: {exc}"})
                return
            self._send_json(201, self._memory_payload(item))
            return
        if len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "tasks" and parts[2]:
            try:
                body = self._body()
                query_workspace = self._workspace_from_query(parsed)
                supplied_workspace = body.get("workspace")
                if supplied_workspace is not None and not isinstance(supplied_workspace, (str, Path)):
                    raise ValueError("workspace must be a non-empty path")
                if query_workspace is not None and supplied_workspace is not None:
                    registry = self._registry()
                    if registry.resolve_workspace(query_workspace) != registry.resolve_workspace(supplied_workspace):
                        raise ApiConflictError("workspace query and request body must match")
                if query_workspace is not None:
                    service, conversation = self._conversation_owner(parts[2], workspace=query_workspace)
                elif supplied_workspace is not None:
                    # Preserve the established 409 for a continuation that
                    # names the wrong workspace, while still being able to
                    # restore an external project after a server restart.
                    try:
                        service, conversation = self._conversation_owner(parts[2])
                    except LookupError:
                        service, conversation = self._conversation_owner(parts[2], workspace=supplied_workspace)
                else:
                    service, conversation = self._conversation_owner(parts[2])
                if supplied_workspace is not None:
                    supplied_path = self._registry().resolve_workspace(supplied_workspace)
                    if supplied_path != service.config.workspace:
                        raise ApiConflictError("a continued conversation must use its original workspace")
                    self._require_trusted_workspace(supplied_path)
                resume_from = conversation.get("continuation_task_id")
                if not isinstance(resume_from, str) or not resume_from:
                    raise ApiConflictError("conversation context is unavailable for continuation")
                if "resume_from" in body:
                    supplied_resume_from = body["resume_from"]
                    if supplied_resume_from is not None and not isinstance(supplied_resume_from, str):
                        raise ValueError("resume_from must be a string")
                    if supplied_resume_from not in {None, resume_from}:
                        raise ValueError("resume_from must match the conversation continuation task")
                record = self._create_task(body, resume_from=resume_from, service=service)
            except LookupError:
                self._send_json(404, {"error": "conversation not found"})
                return
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
                return
            except ApiConflictError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except sqlite3.Error as exc:
                self._send_json(503, {"error": f"workspace state is unavailable: {exc}"})
                return
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(202, self._task_payload(service, record))
            return
        if len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "compact" and parts[2]:
            try:
                self._body()
                service, conversation = self._conversation_owner(
                    parts[2],
                    workspace=self._workspace_from_query(parsed),
                )
                if any(item.status in {"queued", "running"} for item in service.conversation_tasks(parts[2])):
                    raise ApiConflictError("cannot compact a conversation while one of its tasks is active")
                result = service.compact_conversation(parts[2])
                if not result:
                    raise ApiConflictError("conversation context is unavailable for compaction")
            except LookupError:
                self._send_json(404, {"error": "conversation not found"})
                return
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
                return
            except ApiConflictError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(
                200,
                {
                    "conversation_id": parts[2],
                    "compacted": result.compacted,
                    "before_tokens": result.before_tokens,
                    "after_tokens": result.after_tokens,
                    "summary_chars": len(result.summary),
                },
            )
            return
        if parsed.path != "/api/tasks":
            self._send_json(404, {"error": "not found"})
            return
        try:
            body = self._body()
            service = self._service_for_workspace(body.get("workspace"))
            # Existing API clients started before workspace selection existed
            # retain their default-service behavior. Browser-created sessions
            # always supply an explicit workspace and are trust-gated here.
            if "workspace" in body:
                self._require_trusted_workspace(service.config.workspace)
            record = self._create_task(body, service=service)
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        except sqlite3.Error as exc:
            self._send_json(503, {"error": f"workspace state is unavailable: {exc}"})
            return
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(202, self._task_payload(service, record))

    def _create_task(
        self,
        body: dict[str, Any],
        *,
        resume_from: str | None = None,
        service: AgentService | None = None,
    ):
        demo = body.get("demo")
        if demo is not None and not isinstance(demo, bool):
            raise ValueError("demo must be a boolean")
        if resume_from is None and "resume_from" in body:
            supplied = body["resume_from"]
            if supplied is not None and not isinstance(supplied, str):
                raise ValueError("resume_from must be a string")
            resume_from = supplied.strip() if isinstance(supplied, str) else None
            if supplied is not None and not resume_from:
                raise ValueError("resume_from must not be empty")
        return (service or self.service).create_task(body.get("task", ""), demo=demo, resume_from=resume_from)

    @staticmethod
    def _skill_names_from_body(body: dict[str, Any]) -> tuple[str, ...]:
        if body.get("mode") == "auto" and "skills" not in body:
            return ()
        names = body.get("skills")
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("skills must be a list of non-empty strings")
        # Preserve ordering for prompt construction while avoiding duplicate
        # instructions when a client retries a selection request.
        return tuple(dict.fromkeys(name.strip() for name in names))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "command-approval-rules"] and parts[2]:
            try:
                service = self._service_for_workspace(self._workspace_from_query(parsed))
                removed = service.remove_command_approval_rule(parts[2])
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if not removed:
                self._send_json(404, {"error": "command approval rule not found"})
                return
            self._send_json(200, {"ok": True, "rules": service.command_approval_rules()})
            return
        if len(parts) == 3 and parts[:2] == ["api", "conversations"] and parts[2]:
            try:
                workspace = self._workspace_from_query(parsed)
                service, _conversation = self._conversation_owner(parts[2], workspace=workspace)
                deleted = service.delete_conversation(parts[2])
            except LookupError:
                self._send_json(404, {"error": "conversation not found"})
                return
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
                return
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error) as exc:
                self._send_json(503, {"error": f"workspace state is unavailable: {exc}"})
                return
            if not deleted:
                self._send_json(404, {"error": "conversation not found"})
                return
            self._send_json(200, {"ok": True, "id": parts[2]})
            return
        if len(parts) == 3 and parts[:2] == ["api", "memory"] and parts[2]:
            try:
                item_id = int(parts[2])
            except ValueError:
                self._send_json(400, {"error": "memory id must be an integer"})
                return
            try:
                workspace = self._workspace_from_query(parsed)
                deleted = self._service_for_workspace(workspace).delete_memory(item_id)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError as exc:
                self._send_json(503, {"error": str(exc)})
                return
            if not deleted:
                self._send_json(404, {"error": "memory not found"})
                return
            self._send_json(200, {"ok": True, "id": item_id})
            return
        if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
            service = self._service_for_task(parts[2])
            if service and service.cancel_task(parts[2]):
                self._send_json(202, {"ok": True, "status": "cancelling"})
            else:
                self._send_json(404, {"error": "task not found or already finished"})
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


class LimoCodeHTTPServer(ThreadingHTTPServer):
    """HTTP server with exclusive port ownership.

    Address reuse is convenient for short-lived test servers but dangerous for
    a local UI: two Web processes can otherwise answer the same browser with
    different in-memory workspace registries. Keep one owner per port.
    """

    allow_reuse_address = False
    allow_reuse_port = False
    daemon_threads = True


def serve(config: Config, host: str = "127.0.0.1", port: int = 8765, *, demo: bool = False) -> LimoCodeHTTPServer:
    service = AgentService(config)
    service.demo_default = bool(demo)
    ApiHandler.service = service
    ApiHandler.workspace_registry = WorkspaceServiceRegistry(config, demo=demo, service=service)
    ApiHandler.trust_store = WorkspaceTrustStore()
    server = LimoCodeHTTPServer((host, port), ApiHandler)
    print(f"Coding Agent web UI: http://{host}:{port}/ (demo={demo})")
    return server
