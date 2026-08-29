import tempfile
import unittest
from pathlib import Path

from coding_agent.config import Config
from coding_agent.tools import ToolRegistry


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        config = Config(workspace=Path(self.temp_dir.name))
        self.registry = ToolRegistry(config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_write_and_read_file(self):
        written = self.registry.execute("write_file", {"path": "src/demo.txt", "content": "hello"})
        self.assertTrue(written["ok"])
        read = self.registry.execute("read_file", {"path": "src/demo.txt"})
        self.assertEqual(read["content"], "hello")

    def test_write_reports_bounded_change_preview(self):
        self.registry.execute("write_file", {"path": "change.txt", "content": "before"})
        result = self.registry.execute("write_file", {"path": "change.txt", "content": "after"})
        self.assertTrue(result["changed"])
        self.assertEqual(result["previous_preview"], "before")
        self.assertEqual(result["content_preview"], "after")

    def test_rejects_path_traversal(self):
        result = self.registry.execute("read_file", {"path": "../outside.txt"})
        self.assertFalse(result["ok"])
        self.assertIn("outside", result["error"])

    def test_unknown_tool_is_structured_error(self):
        result = self.registry.execute("missing", {})
        self.assertEqual(result, {"ok": False, "error": "unknown tool: missing"})

    def test_validates_arguments_and_file_types(self):
        self.assertFalse(self.registry.execute("read_file", {})["ok"])
        self.assertFalse(self.registry.execute("read_file", {"path": "missing.txt"})["ok"])
        (Path(self.temp_dir.name) / "folder").mkdir()
        self.assertFalse(self.registry.execute("read_file", {"path": "folder"})["ok"])
        self.assertFalse(self.registry.execute("list_files", {"max_entries": 0})["ok"])

    def test_rejects_symlink_escape_when_supported(self):
        outside = Path(self.temp_dir.name).parent / "coding-agent-outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = Path(self.temp_dir.name) / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        result = self.registry.execute("read_file", {"path": "link.txt"})
        self.assertFalse(result["ok"])

    def test_command_safety_timeout_and_output_limit(self):
        self.assertFalse(self.registry.execute("run_command", {"command": "rm -rf /"})["ok"])
        self.assertIn("requires local approval", self.registry.execute("run_command", {"command": "rm -rf /"})["error"])
        self.registry.config = Config(workspace=Path(self.temp_dir.name), command_timeout=0.1)
        timeout_result = self.registry.execute("run_command", {"command": "python -c \"import time; time.sleep(1)\""})
        self.assertTrue(timeout_result["timeout"])
        self.registry.config = Config(workspace=Path(self.temp_dir.name), max_output_chars=5, command_timeout=30)
        output_result = self.registry.execute("run_command", {"command": "python -c \"print('123456789')\""})
        self.assertTrue(output_result["truncated"])
        self.assertEqual(len(output_result["output"]), 5)

    def test_command_can_be_cancelled(self):
        result = self.registry.execute("run_command", {"command": "python -c \"import time; time.sleep(1)\""}, is_cancelled=lambda: True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])

    def test_dangerous_command_requires_explicit_approval(self):
        result = self.registry.execute("run_command", {"command": "shutdown /?"})
        self.assertTrue(result["requires_approval"])
        self.assertEqual(result["error"], "command requires local approval")


if __name__ == "__main__":
    unittest.main()
