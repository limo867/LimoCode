"""Small OpenAI-compatible Chat Completions client implemented with stdlib HTTP."""

from http.client import HTTPException
import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from .config import Config


class LLMError(RuntimeError):
    """Base class for model request and response failures."""


class LLMConfigurationError(LLMError):
    pass


class LLMRequestError(LLMError):
    """A transport/provider failure with retry guidance for the agent loop."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


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
        self._opener = self._build_proxy_opener(config.llm_proxy)

    @staticmethod
    def _build_proxy_opener(proxy: str | None):
        """Return an explicit proxy opener when the operator configured one."""
        if not proxy:
            return None
        parsed = urlparse(proxy)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LLMConfigurationError("LLM_PROXY must be an absolute http:// or https:// proxy URL")
        return build_opener(ProxyHandler({"http": proxy, "https": proxy}))

    def _blocked_socket_error(self, error: BaseException) -> LLMRequestError | None:
        """Describe Windows outbound socket policy denials without exposing credentials."""
        candidate = error.reason if isinstance(error, URLError) else error
        if not isinstance(candidate, OSError):
            return None
        # Windows socket errors returned by urllib normally set ``winerror``;
        # manually constructed OSErrors and some wrappers only retain errno.
        if getattr(candidate, "winerror", None) != 10013 and getattr(candidate, "errno", None) != 10013:
            return None
        host = urlparse(self.endpoint).netloc or "the configured model endpoint"
        return LLMRequestError(
            "model API connection was blocked by Windows while connecting to "
            f"{host} (WinError 10013). Allow Python outbound HTTPS in your firewall/security software, "
            "or configure LLM_PROXY, then restart LimoCode.",
            retryable=False,
        )

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(messages, tools, stream=False)

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Complete a request through OpenAI-compatible Server-Sent Events."""
        return self._request(messages, tools, stream=True, on_delta=on_delta)

    def _request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool,
        on_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": serialize_messages(messages),
            "tool_choice": "auto",
            "stream": stream,
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
            open_request = self._opener.open if self._opener is not None else urlopen
            with open_request(request, timeout=self.config.model_timeout) as response:
                if stream:
                    return self._read_stream(response, on_delta)
                raw = response.read(self.config.max_output_chars * 4).decode("utf-8")
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise LLMRequestError(f"model API returned HTTP {exc.code}: {detail}") from exc
        except HTTPException as exc:
            # Chunked/SSE responses can end abruptly. http.client reports
            # those transport failures as HTTPException rather than URLError.
            detail = str(exc) or exc.__class__.__name__
            raise LLMRequestError(f"model API connection was interrupted ({exc.__class__.__name__}): {detail}") from exc
        except URLError as exc:
            blocked = self._blocked_socket_error(exc)
            if blocked:
                raise blocked from exc
            raise LLMRequestError(f"model API request failed: {exc}") from exc
        except (TimeoutError, OSError) as exc:
            blocked = self._blocked_socket_error(exc)
            if blocked:
                raise blocked from exc
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

    @staticmethod
    def _read_stream(response: Any, on_delta: Callable[[str], None] | None) -> dict[str, Any]:
        content: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        raw_chunks: list[str] = []
        saw_sse = False
        lines = response if hasattr(response, "__iter__") else iter(response.readline, b"")
        for raw_line in lines:
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="replace").strip()
            else:
                line = str(raw_line).strip()
            raw_chunks.append(line)
            if not line or not line.startswith("data:"):
                continue
            saw_sse = True
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except (ValueError, TypeError):
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = (choices[0] or {}).get("delta") or {}
            text = delta.get("content") or ""
            if text:
                content.append(text)
                if on_delta:
                    on_delta(text)
            for position, call_delta in enumerate(delta.get("tool_calls") or []):
                index = call_delta.get("index", position)
                if not isinstance(index, int):
                    index = position
                item = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                item["id"] += str(call_delta.get("id") or "")
                function = call_delta.get("function") or {}
                item["name"] += str(function.get("name") or "")
                item["arguments"] += str(function.get("arguments") or "")
        if not saw_sse:
            # Some OpenAI-compatible gateways ignore stream=true and return a
            # regular Chat Completions JSON body. Preserve compatibility.
            try:
                body = json.loads("".join(raw_chunks))
                message = body["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMResponseError("model API returned an invalid streaming response") from exc
            normalized: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
            calls = message.get("tool_calls") or []
            if calls:
                normalized["tool_calls"] = []
                for call in calls:
                    function = call.get("function") or {}
                    normalized["tool_calls"].append(
                        {"id": call.get("id", ""), "name": function.get("name", ""), "arguments": function.get("arguments")}
                    )
            if on_delta and normalized["content"]:
                on_delta(normalized["content"])
            return normalized
        normalized: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if tool_calls:
            normalized["tool_calls"] = list(tool_calls.values())
        return normalized
