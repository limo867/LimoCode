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

    def test_loads_user_dotenv_when_workspace_has_no_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            (home / ".local-codex").mkdir(parents=True)
            workspace.mkdir()
            (home / ".local-codex" / ".env").write_text("LLM_API_KEY=user-key\nLLM_MODEL=user-model\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch("coding_agent.config.Path.home", return_value=home):
                config = Config.from_env(str(workspace))
        self.assertEqual(config.api_key, "user-key")
        self.assertEqual(config.model, "user-model")


if __name__ == "__main__":
    unittest.main()
