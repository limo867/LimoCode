import unittest
from pathlib import Path

from coding_agent.config import Config
from coding_agent.models import ModelManager


class ModelManagerTests(unittest.TestCase):
    def test_lists_current_model_and_switches_to_configured_model(self):
        config = Config(
            workspace=Path("workspace"),
            model="model-a",
            base_url="https://gateway.example/v1",
            max_context_tokens=64000,
            available_models=("model-a", "model-b"),
        )
        manager = ModelManager(config)
        self.assertEqual(manager.current().provider, "gateway.example")
        self.assertEqual(manager.current().context_window, 64000)
        self.assertEqual([model.name for model in manager.available()], ["model-a", "model-b"])
        self.assertEqual(manager.switch("model-b").model, "model-b")

    def test_rejects_model_outside_explicit_list(self):
        manager = ModelManager(Config(workspace=Path("workspace"), available_models=("allowed",)))
        with self.assertRaises(ValueError):
            manager.switch("other")


if __name__ == "__main__":
    unittest.main()
