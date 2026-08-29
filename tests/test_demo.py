import tempfile
import unittest
from pathlib import Path

from coding_agent.agent import Agent, DemoModel
from coding_agent.config import Config


class DemoModelTests(unittest.TestCase):
    def test_demo_completes_real_tool_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.txt").write_text("demo input", encoding="utf-8")
            agent = Agent(Config(workspace=root), DemoModel())
            result = agent.run("demonstrate local editing")
            self.assertIn("离线演示已完成", result)
            self.assertEqual(agent.last_status, "completed")
            self.assertEqual(len(agent.execution_log), 4)
            self.assertTrue((root / ".coding-agent-demo/result.txt").is_file())


if __name__ == "__main__":
    unittest.main()
