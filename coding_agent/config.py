from dataclasses import dataclass, replace
from pathlib import Path
import os


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
        root = Path(workspace or os.getenv("AGENT_WORKSPACE", os.getcwd())).expanduser().resolve()
        history_value = Path(os.getenv("AGENT_HISTORY_DB", ".coding-agent/tasks.sqlite3")).expanduser()
        history_db = (root / history_value).resolve() if not history_value.is_absolute() else history_value.resolve()
        return cls(
            workspace=root,
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=os.getenv("LLM_API_KEY"),
            model_timeout=int(os.getenv("LLM_TIMEOUT", "60")),
            model_retries=int(os.getenv("LLM_RETRIES", "1")),
            model_retry_base_delay_ms=int(os.getenv("LLM_RETRY_BASE_DELAY_MS", "250")),
            model_min_request_interval_ms=int(os.getenv("LLM_MIN_REQUEST_INTERVAL_MS", "0")),
            max_turns=int(os.getenv("AGENT_MAX_TURNS", "12")),
            command_timeout=int(os.getenv("AGENT_COMMAND_TIMEOUT", "30")),
            command_approval_timeout=int(os.getenv("AGENT_COMMAND_APPROVAL_TIMEOUT", "120")),
            max_output_chars=int(os.getenv("AGENT_MAX_OUTPUT_CHARS", "12000")),
            max_file_chars=int(os.getenv("AGENT_MAX_FILE_CHARS", "200000")),
            max_history_messages=int(os.getenv("AGENT_MAX_HISTORY_MESSAGES", "30")),
            max_history_chars=int(os.getenv("AGENT_MAX_HISTORY_CHARS", "8000")),
            history_db=history_db,
            approved_commands=frozenset(item.strip() for item in os.getenv("AGENT_APPROVED_COMMANDS", "").split(";;") if item.strip()),
        )
