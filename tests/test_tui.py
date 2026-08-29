import unittest
from pathlib import Path

from coding_agent.config import Config
from coding_agent.events import AgentEvent
from coding_agent.tui import apply_config_change, event_summary, parse_command


class TerminalUiTests(unittest.TestCase):
    def test_parses_tasks_and_quoted_commands(self):
        self.assertEqual(parse_command("inspect tests"), ("task", ["inspect tests"]))
        self.assertEqual(parse_command('/config workspace "C:/demo workspace"'), ("config", ["workspace", "C:/demo workspace"]))

    def test_applies_configuration_and_preserves_zero_request_gap(self):
        config = Config(workspace=Path("workspace"), model_min_request_interval_ms=100)
        updated, demo = apply_config_change(config, False, "request-gap", "0")
        self.assertEqual(updated.model_min_request_interval_ms, 0)
        self.assertFalse(demo)
        _, demo = apply_config_change(updated, demo, "demo", "on")
        self.assertTrue(demo)

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            apply_config_change(Config(workspace=Path("workspace")), False, "max-turns", "0")

    def test_summarizes_tool_and_approval_events(self):
        tool = AgentEvent("tool_finished", "task", {"tool": "write_file", "result": {"ok": True, "path": "app.py", "changed": True}})
        approval = AgentEvent("command_approval_requested", "task", {"command": "shutdown /?"})
        self.assertEqual(event_summary(tool), "tool write_file app.py (changed)")
        self.assertIn("shutdown", event_summary(approval))


if __name__ == "__main__":
    unittest.main()
