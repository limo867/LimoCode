import tempfile
import unittest
from pathlib import Path

from coding_agent.agent import Agent
from coding_agent.config import Config
from coding_agent.skills import SkillManager


class CapturingModel:
    def __init__(self):
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(messages)
        return {"content": "done"}


class SkillManagerTests(unittest.TestCase):
    def test_discovers_loads_and_selects_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_path = root / "debugging" / "SKILL.md"
            skill_path.parent.mkdir()
            skill_path.write_text(
                "---\nname: debugging\ndescription: Fix software bugs\n---\n\n# Workflow\nReproduce before editing.",
                encoding="utf-8",
            )
            (root / "bad" / "SKILL.md").parent.mkdir()
            (root / "bad" / "SKILL.md").write_text("---\nname: broken", encoding="utf-8")
            manager = SkillManager([root])
            self.assertEqual([skill.name for skill in manager.metadata()], ["debugging"])
            self.assertIn("Reproduce", manager.load("debugging").instructions)
            self.assertEqual([skill.name for skill in manager.select("请帮我修复这个报错")], ["debugging"])

    def test_selected_skill_is_injected_into_the_model_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "testing" / "SKILL.md"
            path.parent.mkdir()
            path.write_text(
                "---\nname: testing\ndescription: Test workflows\n---\n\n# Tests\nRun focused tests first.",
                encoding="utf-8",
            )
            model = CapturingModel()
            events = []
            agent = Agent(
                Config(workspace=root),
                model=model,
                event_callback=lambda event_type, data: events.append((event_type, data)),
                skill_manager=SkillManager([root]),
                selected_skills=("testing",),
            )
            self.assertEqual(agent.run("change code"), "done")
            self.assertIn("Run focused tests first", model.requests[0][0]["content"])
            self.assertIn(("skills_loaded", {"skills": ["testing"], "automatic": False}), events)


if __name__ == "__main__":
    unittest.main()
