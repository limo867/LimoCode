"""Runtime model selection without coupling the agent to a provider SDK."""

from dataclasses import dataclass
from urllib.parse import urlparse

from .config import Config


@dataclass(frozen=True)
class ModelInfo:
    name: str
    provider: str
    context_window: int


class ModelManager:
    """Tracks the configured model and validates interactive switches."""

    def __init__(self, config: Config):
        self.config = config

    def current(self) -> ModelInfo:
        host = urlparse(self.config.base_url).netloc
        return ModelInfo(self.config.model, host or "openai-compatible", self.config.max_context_tokens)

    def available(self) -> list[ModelInfo]:
        names = list(self.config.available_models)
        if self.config.model not in names:
            names.insert(0, self.config.model)
        current = self.current()
        return [ModelInfo(name, current.provider, current.context_window) for name in names]

    def switch(self, model: str) -> Config:
        name = model.strip()
        if not name:
            raise ValueError("model must not be empty")
        if self.config.available_models and name not in self.config.available_models:
            raise ValueError("model is not in LLM_MODELS")
        return self.config.with_overrides(model=name)
