from dataclasses import dataclass, replace
from pathlib import Path
import os


def _dotenv_values(path: Path) -> dict[str, str]:
    """Read a small .env file without adding a runtime dependency."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not name.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[1:-1]
        values[name] = value
    return values


@dataclass(frozen=True)
class Config:
    """Runtime settings. Environment loading is kept in one place."""

    workspace: Path
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    model_timeout: int = 60
    model_retries: int = 1
    model_retry_base_delay_ms: int = 250
    model_min_request_interval_ms: int = 0
    max_turns: int = 12
    command_timeout: int = 30
    command_approval_timeout: int = 120
    max_output_chars: int = 12000
    max_file_chars: int = 200000
    max_history_messages: int = 30
    max_history_chars: int = 8000
    history_db: Path | None = None
    approved_commands: frozenset[str] = frozenset()

    def with_overrides(
        self,
        *,
        workspace: Path | None = None,
        model: str | None = None,
        model_timeout: int | None = None,
        max_turns: int | None = None,
        command_timeout: int | None = None,
        command_approval_timeout: int | None = None,
        model_min_request_interval_ms: int | None = None,
    ) -> "Config":
        """Apply optional CLI/UI overrides while preserving environment defaults."""
        values = {
            "workspace": workspace,
            "model": model,
            "model_timeout": model_timeout,
            "max_turns": max_turns,
            "command_timeout": command_timeout,
            "command_approval_timeout": command_approval_timeout,
            "model_min_request_interval_ms": model_min_request_interval_ms,
        }
        return replace(self, **{key: value for key, value in values.items() if value is not None})

    @classmethod
    def from_env(cls, workspace: str | None = None) -> "Config":
        initial_root = Path(workspace or os.getenv("AGENT_WORKSPACE", os.getcwd())).expanduser().resolve()
        try:
            user_dotenv_path = Path.home() / ".local-codex" / ".env"
        except RuntimeError:
            user_dotenv_path = None
        global_dotenv = _dotenv_values(user_dotenv_path) if user_dotenv_path else {}
        workspace_dotenv = _dotenv_values(initial_root / ".env")

        def get(name: str, default: str | None = None) -> str | None:
            return os.getenv(name, workspace_dotenv.get(name, global_dotenv.get(name, default)))

        root = Path(workspace or get("AGENT_WORKSPACE", os.getcwd()) or os.getcwd()).expanduser().resolve()
        history_value = Path(get("AGENT_HISTORY_DB", ".coding-agent/tasks.sqlite3") or ".coding-agent/tasks.sqlite3").expanduser()
        history_db = (root / history_value).resolve() if not history_value.is_absolute() else history_value.resolve()
        return cls(
            workspace=root,
            model=get("LLM_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
            base_url=(get("LLM_BASE_URL", "https://api.openai.com/v1") or "https://api.openai.com/v1").rstrip("/"),
            api_key=get("LLM_API_KEY"),
            model_timeout=int(get("LLM_TIMEOUT", "60") or "60"),
            model_retries=int(get("LLM_RETRIES", "1") or "1"),
            model_retry_base_delay_ms=int(get("LLM_RETRY_BASE_DELAY_MS", "250") or "250"),
            model_min_request_interval_ms=int(get("LLM_MIN_REQUEST_INTERVAL_MS", "0") or "0"),
            max_turns=int(get("AGENT_MAX_TURNS", "12") or "12"),
            command_timeout=int(get("AGENT_COMMAND_TIMEOUT", "30") or "30"),
            command_approval_timeout=int(get("AGENT_COMMAND_APPROVAL_TIMEOUT", "120") or "120"),
            max_output_chars=int(get("AGENT_MAX_OUTPUT_CHARS", "12000") or "12000"),
            max_file_chars=int(get("AGENT_MAX_FILE_CHARS", "200000") or "200000"),
            max_history_messages=int(get("AGENT_MAX_HISTORY_MESSAGES", "30") or "30"),
            max_history_chars=int(get("AGENT_MAX_HISTORY_CHARS", "8000") or "8000"),
            history_db=history_db,
            approved_commands=frozenset(item.strip() for item in (get("AGENT_APPROVED_COMMANDS", "") or "").split(";;") if item.strip()),
        )
