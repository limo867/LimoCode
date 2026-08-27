import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from coding_agent.config import Config
from coding_agent.llm_client import LLMRequestError, LLMResponseError, OpenAICompatibleClient


class FakeResponse:
    def __init__(self, body: dict):
        self.data = json.dumps(body).encode("utf-8")

    def read(self, size: int) -> bytes:
        return self.data[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class OpenAICompatibleClientTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(workspace=Path.cwd(), api_key="test-key", base_url="https://example.test/v1", model="test-model")
        self.client = OpenAICompatibleClient(self.config)

    @patch("coding_agent.llm_client.urlopen")
    def test_sends_messages_and_parses_tool_call(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-1", "function": {"name": "read_file", "arguments": "{\"path\": \"README.md\"}"}}]}}]}
        )
        result = self.client.complete(
            [{"role": "user", "content": "Read the readme"}],
            [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        )
        self.assertEqual(result["tool_calls"][0], {"id": "call-1", "name": "read_file", "arguments": '{"path": "README.md"}'})
        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["messages"][0]["content"], "Read the readme")
        self.assertEqual(payload["tools"][0]["function"]["name"], "read_file")
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")

    @patch("coding_agent.llm_client.urlopen")
    def test_rejects_invalid_response_shape(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse({"choices": []})
        with self.assertRaises(LLMResponseError):
            self.client.complete([], [])

    @patch("coding_agent.llm_client.urlopen")
    def test_wraps_http_error_without_key(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError("https://example.test", 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}'))
        with self.assertRaises(LLMRequestError) as captured:
            self.client.complete([], [])
        self.assertIn("HTTP 401", str(captured.exception))
        self.assertNotIn("test-key", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
