from datetime import datetime, timezone
import mimetypes
from pathlib import Path
from typing import Any


# These folders are implementation details, dependency caches, or metadata
# rather than source the operator normally wants to inspect in the workbench.
WORKSPACE_TREE_IGNORED_NAMES = frozenset(
    {
        ".coding-agent",
        ".coding-agent-demo",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)


class Workspace:
    """Resolves paths while enforcing a workspace boundary."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("path is outside the configured workspace") from exc
        return candidate


def workspace_tree(
    root: Path,
    relative_path: str = "",
    *,
    limit: int = 200,
) -> dict[str, Any]:
    """Return one safe, bounded directory level for a local file explorer.

    This is deliberately separate from the Agent's ``list_files`` tool.  The
    tool is optimized for model context and returns a recursive flat list;
    the browser needs a lazy tree whose path resolution and symlink handling
    are explicit at every expansion.
    """
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")

    try:
        resolved_root = Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"workspace is unavailable: {exc}") from exc
    if not resolved_root.is_dir():
        raise ValueError("workspace must be a directory")

    requested = Path(relative_path or "")
    if requested.is_absolute():
        raise ValueError("path must be relative to the workspace")
    unresolved_target = resolved_root / requested
    if relative_path and unresolved_target.is_symlink():
        raise ValueError("symbolic links cannot be opened in the workspace tree")
    try:
        target = unresolved_target.resolve(strict=True)
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("path is outside the configured workspace") from exc
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"path is unavailable: {exc}") from exc
    if not target.is_dir():
        raise ValueError("path must be a directory")

    entries: list[dict[str, Any]] = []
    truncated = False
    # Directory scans are intentionally bounded as well as response bodies.
    # A very large generated directory must not freeze the local Web UI.
    scan_limit = max(2_000, limit + 1)
    try:
        candidates = target.iterdir()
        for child in candidates:
            if child.name in WORKSPACE_TREE_IGNORED_NAMES or child.is_symlink():
                continue
            try:
                resolved_child = child.resolve(strict=True)
                resolved_child.relative_to(resolved_root)
                is_directory = resolved_child.is_dir()
                if not is_directory and not resolved_child.is_file():
                    continue
                stat = resolved_child.stat()
            except (OSError, RuntimeError, ValueError):
                # Files can disappear while an agent is editing a workspace.
                # Ignore that one entry rather than failing the whole tree.
                continue
            relative = resolved_child.relative_to(resolved_root).as_posix()
            entries.append(
                {
                    "name": child.name,
                    "path": relative,
                    "kind": "directory" if is_directory else "file",
                    "has_children": bool(is_directory),
                    "size": None if is_directory else stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=timezone.utc,
                    ).isoformat(),
                }
            )
            if len(entries) >= scan_limit:
                truncated = True
                break
    except OSError as exc:
        raise ValueError(f"cannot read workspace directory: {exc}") from exc

    entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold()))
    if len(entries) > limit:
        truncated = True
        entries = entries[:limit]
    return {
        "path": target.relative_to(resolved_root).as_posix(),
        "entries": entries,
        "truncated": truncated,
    }


_IMAGE_MIMES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/x-icon",
})
_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".java": "java", ".c": "c", ".h": "c", ".cc": "cpp",
    ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".rs": "rust", ".go": "go",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
    ".sh": "bash", ".ps1": "powershell", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".sql": "sql", ".html": "html",
    ".htm": "html", ".css": "css", ".md": "markdown", ".txt": "text",
    ".xml": "xml", ".vue": "vue", ".svelte": "svelte", ".diff": "diff",
    ".patch": "diff",
}


def _workspace_file(root: Path, relative_path: str) -> tuple[Path, Path]:
    """Resolve a browser file path without following links or leaving root."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("path must be a non-empty relative file path")
    requested = Path(relative_path)
    if requested.is_absolute():
        raise ValueError("path must be relative to the workspace")
    resolved_root = Path(root).expanduser().resolve(strict=True)
    unresolved = resolved_root / requested
    # Check every component before resolving so an internal link cannot be
    # used to access a file through an alternate path.
    cursor = resolved_root
    for component in requested.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError("symbolic links cannot be opened in the workspace")
    try:
        target = unresolved.resolve(strict=True)
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("path is outside the configured workspace") from exc
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"file is unavailable: {exc}") from exc
    if not target.is_file():
        raise ValueError("path must be a file")
    return resolved_root, target


def workspace_file_preview(root: Path, relative_path: str, *, max_bytes: int = 256_000) -> dict[str, Any]:
    """Return bounded, browser-friendly metadata/content for one workspace file."""
    resolved_root, target = _workspace_file(root, relative_path)
    stat = target.stat()
    path = target.relative_to(resolved_root).as_posix()
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    payload: dict[str, Any] = {
        "path": path,
        "name": target.name,
        "size": stat.st_size,
        "mime": mime,
        "language": _LANGUAGE_BY_SUFFIX.get(target.suffix.casefold(), "text"),
        "truncated": False,
    }
    if mime in _IMAGE_MIMES:
        payload.update({"kind": "image"})
        return payload
    if stat.st_size > max_bytes:
        payload.update({"kind": "too_large", "content": "", "truncated": True})
        return payload
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read file: {exc}") from exc
    if b"\x00" in data:
        payload.update({"kind": "binary", "content": ""})
        return payload
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        payload.update({"kind": "binary", "content": ""})
        return payload
    payload.update({"kind": "text", "content": content, "lines": len(content.splitlines())})
    return payload


def workspace_image_bytes(root: Path, relative_path: str, *, max_bytes: int = 8_000_000) -> tuple[bytes, str]:
    """Read a bounded raster image for the dedicated preview endpoint."""
    _resolved_root, target = _workspace_file(root, relative_path)
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if mime not in _IMAGE_MIMES:
        raise ValueError("only raster image previews are supported")
    if target.stat().st_size > max_bytes:
        raise ValueError("image is too large to preview")
    try:
        return target.read_bytes(), mime
    except OSError as exc:
        raise ValueError(f"cannot read image: {exc}") from exc
