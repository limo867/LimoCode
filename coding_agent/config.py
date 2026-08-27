from dataclasses import dataclass
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
    max_turns: int = 12
    command_timeout: int = 30
    max_output_chars: int = 12000
    max_file_chars: int = 200000
    max_history_messages: int = 30
    max_history_chars: int = 8000

    @classmethod
    def from_env(cls, workspace: str | None = None) -> "Config":
        root = Path(workspace or os.getenv("AGENT_WORKSPACE", os.getcwd())).expanduser().resolve()
        return cls(
            workspace=root,
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=os.getenv("LLM_API_KEY"),
            model_timeout=int(os.getenv("LLM_TIMEOUT", "60")),
            model_retries=int(os.getenv("LLM_RETRIES", "1")),
            max_turns=int(os.getenv("AGENT_MAX_TURNS", "12")),
            command_timeout=int(os.getenv("AGENT_COMMAND_TIMEOUT", "30")),
            max_output_chars=int(os.getenv("AGENT_MAX_OUTPUT_CHARS", "12000")),
            max_file_chars=int(os.getenv("AGENT_MAX_FILE_CHARS", "200000")),
            max_history_messages=int(os.getenv("AGENT_MAX_HISTORY_MESSAGES", "30")),
            max_history_chars=int(os.getenv("AGENT_MAX_HISTORY_CHARS", "8000")),
        )
