import unittest

from coding_agent.gui import tool_result_view


class GuiPresentationTests(unittest.TestCase):
    def test_routes_command_output_to_command_view(self):
        view = tool_result_view("run_command", {"output": "2 passed", "returncode": 0})
        self.assertEqual(view[0], "commands")
        self.assertIn("2 passed", view[1])
        self.assertIn("Exit code: 0", view[1])

    def test_routes_write_preview_to_change_view(self):
        view = tool_result_view(
            "write_file",
            {"path": "app.py", "changed": True, "previous_preview": "old", "content_preview": "new"},
        )
        self.assertEqual(view[0], "changes")
        self.assertIn("--- before\nold", view[1])
        self.assertIn("+++ after\nnew", view[1])

    def test_ignores_non_display_tools(self):
        self.assertIsNone(tool_result_view("list_files", {"files": ["a.py"]}))


if __name__ == "__main__":
    unittest.main()
