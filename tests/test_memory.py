import tempfile
import unittest
from pathlib import Path

from coding_agent.agent import Agent
from coding_agent.config import Config
from coding_agent.memory import MemoryStore


class CapturingModel:
    def __init__(self):
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(messages)
        return {"content": "done"}


class MemoryStoreTests(unittest.TestCase):
    def test_memory_persists_searches_and_deletes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            first = MemoryStore(path)
            item = first.add("Project uses Java 21")
            first.close()
            second = MemoryStore(path)
            self.assertEqual(second.search("Java")[0].content, "Project uses Java 21")
            self.assertTrue(second.delete(item.id))
            self.assertEqual(second.list(), [])
            second.close()

    def test_agent_retrieves_memory_and_extracts_explicit_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MemoryStore(root / "memory.sqlite3")
            store.add("Project uses Java 21")
            model = CapturingModel()
            agent = Agent(Config(workspace=root), model=model, memory_store=store)
            agent.run("Implement a module. Project must never modify the database schema.")
            system_message = model.requests[0][0]["content"]
            self.assertIn("Project uses Java 21", system_message)
            self.assertTrue(any("database schema" in item.content for item in store.list()))
            store.close()


if __name__ == "__main__":
    unittest.main()
