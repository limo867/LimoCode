import unittest
from pathlib import Path

from coding_agent.config import Config


class ConfigOverrideTests(unittest.TestCase):
    def test_optional_overrides_preserve_defaults_and_accept_zero(self):
        original = Config(workspace=Path("workspace"), model="base", model_min_request_interval_ms=25)
        updated = original.with_overrides(model_timeout=12, model_min_request_interval_ms=0)
        self.assertEqual(updated.workspace, original.workspace)
        self.assertEqual(updated.model, "base")
        self.assertEqual(updated.model_timeout, 12)
        self.assertEqual(updated.model_min_request_interval_ms, 0)

    def test_workspace_override_is_supported_for_gui(self):
        original = Config(workspace=Path("old"))
        updated = original.with_overrides(workspace=Path("new"))
        self.assertEqual(updated.workspace, Path("new"))


if __name__ == "__main__":
    unittest.main()
