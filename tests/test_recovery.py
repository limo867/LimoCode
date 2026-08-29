import tempfile
import unittest
from pathlib import Path

from coding_agent.agent import Agent
from coding_agent.config import Config
from coding_agent.llm_client import LLMRequestError


class RetryModel:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            raise LLMRequestError("temporary connection failure")
        return {"content": "Recovered after retry."}


class FailingModel:
    def complete(self, messages, tools):
        raise LLMRequestError("service unavailable")


class HistoryModel:
    def __init__(self):
        self.lengths = []
        self.turn = 0

    def complete(self, messages, tools):
        self.lengths.append(len(messages))
        self.turn += 1
        if self.turn < 4:
            return {"content": "", "tool_calls": [{"name": "list_files", "arguments": "{}"}]}
        return {"content": "Finished."}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_retries_temporary_model_failure(self):
        model = RetryModel()
        agent = Agent(Config(workspace=self.workspace, model_retries=1), model)
        self.assertEqual(agent.run("retry"), "Recovered after retry.")
        self.assertEqual(model.calls, 2)
        self.assertEqual(agent.last_status, "completed")

    def test_retries_with_exponential_backoff(self):
        sleeps = []
        model = RetryModel()
        agent = Agent(Config(workspace=self.workspace, model_retries=1, model_retry_base_delay_ms=50), model, sleeper=sleeps.append)
        agent.run("retry")
        self.assertEqual(sleeps, [0.05])

    def test_returns_summary_for_persistent_model_failure(self):
        agent = Agent(Config(workspace=self.workspace, model_retries=0), FailingModel())
        self.assertIn("Agent failed during model request", agent.run("fail"))
        self.assertEqual(agent.last_status, "failed")

    def test_bounds_history_passed_to_model(self):
        model = HistoryModel()
        agent = Agent(Config(workspace=self.workspace, max_turns=5, max_history_messages=2), model)
        self.assertEqual(agent.run("list"), "Finished.")
        self.assertLessEqual(max(model.lengths), 4)


if __name__ == "__main__":
    unittest.main()
