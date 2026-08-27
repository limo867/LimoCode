"""Small OpenAI-compatible Chat Completions client implemented with stdlib HTTP."""

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Config


class LLMError(RuntimeError):
    """Base class for model request and response failures."""


class LLMConfigurationError(LLMError):
    pass


class LLMRequestError(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


def serialize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the agent's normalized messages to Chat Completions messages."""
    result: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        calls = item.pop("tool_calls", None)
        if calls:
            item["tool_calls"] = [
                {
                    "id": call.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": call.get("arguments") if isinstance(call.get("arguments"), str) else json.dumps(call.get("arguments", {}), ensure_ascii=False),
                    },
                }
                for call in calls
            ]
        if item.get("role") == "tool" and not isinstance(item.get("content"), str):
            item["content"] = json.dumps(item["content"], ensure_ascii=False)
        result.append(item)
    return result


class OpenAICompatibleClient:
    def __init__(self, config: Config):
        if not config.api_key:
            raise LLMConfigurationError("LLM_API_KEY is not configured")
        self.config = config
        self.endpoint = f"{config.base_url}/chat/completions"

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": serialize_messages(messages),
            "tool_choice": "auto",
        }
        if tools:
            payload["tools"] = tools
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.model_timeout) as response:
                raw = response.read(self.config.max_output_chars * 4).decode("utf-8")
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise LLMRequestError(f"model API returned HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise LLMRequestError(f"model API request failed: {exc}") from exc
        try:
            body = json.loads(raw)
            message = body["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("model API returned an invalid response") from exc
        normalized: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        calls = message.get("tool_calls") or []
        if calls:
            normalized["tool_calls"] = []
            for call in calls:
                function = call.get("function") or {}
                normalized["tool_calls"].append(
                    {"id": call.get("id", ""), "name": function.get("name", ""), "arguments": function.get("arguments")}
                )
        return normalized
