import unittest
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

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

    def test_loads_workspace_dotenv_without_overriding_process_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / ".env").write_text(
                "LLM_API_KEY=local-key\nLLM_MODEL=dotenv-model\nLLM_TIMEOUT=90\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"LLM_MODEL": "process-model"}, clear=True):
                config = Config.from_env(str(workspace))
        self.assertEqual(config.api_key, "local-key")
        self.assertEqual(config.model, "process-model")
        self.assertEqual(config.model_timeout, 90)


if __name__ == "__main__":
    unittest.main()
