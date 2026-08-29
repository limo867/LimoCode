import copy
import tempfile
import unittest
from pathlib import Path

from coding_agent.agent import Agent
from coding_agent.config import Config


class SequencedModel:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        return self.responses.pop(0)


class ContextCompactionTests(unittest.TestCase):
    def test_automatically_compacts_long_tool_history_with_structured_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = SequencedModel(
                [
                    {"content": "", "tool_calls": [{"name": "write_file", "arguments": '{"path":"notes.txt","content":"' + "x" * 600 + '"}'}]},
                    {"content": "", "tool_calls": [{"name": "read_file", "arguments": '{"path":"notes.txt"}'}]},
                    {"content": "finished"},
                ]
            )
            events = []
            agent = Agent(
                Config(workspace=root, max_context_tokens=160, compaction_threshold=0.45),
                model=model,
                event_callback=lambda event_type, data: events.append((event_type, data)),
            )
            self.assertEqual(agent.run("Update notes and preserve the progress"), "finished")
            compacted_requests = [request for request in model.requests if any("## Compacted Task Context" in str(message.get("content", "")) for message in request)]
            self.assertTrue(compacted_requests)
            compaction_events = [data for event_type, data in events if event_type == "context_compacted"]
            self.assertTrue(compaction_events)
            self.assertLessEqual(compaction_events[-1]["after_tokens"], 160)
            self.assertTrue(any("## Task" in str(message.get("content", "")) for message in compacted_requests[0]))

    def test_manual_compaction_preserves_task_goal(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(Config(workspace=Path(directory), max_context_tokens=200), model=SequencedModel([{"content": "done"}]))
            agent.run("Keep the authentication API unchanged")
            agent.messages.extend([{"role": "assistant", "content": "reviewed auth.py"}, {"role": "tool", "content": {"ok": True, "path": "auth.py"}}])
            result = agent.compact_context(force=True)
            self.assertTrue(result.compacted)
            self.assertIn("Keep the authentication API unchanged", result.summary)


if __name__ == "__main__":
    unittest.main()
