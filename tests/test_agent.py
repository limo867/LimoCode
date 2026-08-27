import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any

from coding_agent.agent import Agent
from coding_agent.config import Config


class SequencedModel:
    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = responses
        self.requests: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(messages))
        return self.responses.pop(0)


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "source.txt").write_text("before", encoding="utf-8")
        self.config = Config(workspace=self.workspace, max_turns=5)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_runs_multiple_tool_rounds_and_returns_final_answer(self):
        model = SequencedModel(
            [
                {"content": "", "tool_calls": [{"id": "read", "name": "read_file", "arguments": '{"path":"source.txt"}'}]},
                {"content": "", "tool_calls": [{"id": "write", "name": "write_file", "arguments": '{"path":"result.txt","content":"after"}'}]},
                {"content": "", "tool_calls": [{"id": "verify", "name": "run_command", "arguments": '{"command":"python -c \\"from pathlib import Path; print(Path(\'result.txt\').read_text())\\""}'}]},
                {"content": "Done: result.txt was updated and verified."},
            ]
        )
        agent = Agent(self.config, model)
        final = agent.run("Update the source")
        self.assertEqual(final, "Done: result.txt was updated and verified.")
        self.assertEqual((self.workspace / "result.txt").read_text(encoding="utf-8"), "after")
        self.assertEqual(len(agent.execution_log), 3)
        self.assertTrue(any(message["role"] == "tool" and "before" in str(message["content"]) for message in model.requests[1]))
        self.assertTrue(any(message["role"] == "tool" and message["content"].get("ok") for message in model.requests[3]))

    def test_returns_argument_error_to_model_and_continues(self):
        model = SequencedModel(
            [
                {"content": "", "tool_calls": [{"id": "bad", "name": "read_file", "arguments": "not-json"}]},
                {"content": "I could not read the file because the tool arguments were invalid."},
            ]
        )
        agent = Agent(self.config, model)
        final = agent.run("Read a file")
        self.assertIn("arguments were invalid", final)
        tool_messages = [message for message in model.requests[1] if message["role"] == "tool"]
        self.assertEqual(tool_messages[0]["content"], {"ok": False, "error": "tool arguments are not valid JSON"})

    def test_stops_after_max_turns(self):
        model = SequencedModel(
            [{"content": "", "tool_calls": [{"name": "list_files", "arguments": "{}"}]}] * 2
        )
        agent = Agent(Config(workspace=self.workspace, max_turns=2), model)
        final = agent.run("Keep listing files")
        self.assertIn("maximum turn limit (2)", final)
        self.assertEqual(len(agent.execution_log), 2)


if __name__ == "__main__":
    unittest.main()
