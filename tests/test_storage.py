import tempfile
import unittest
from pathlib import Path

from coding_agent.config import Config
from coding_agent.service import AgentService, TaskRecord


class StorageTests(unittest.TestCase):
    def test_tasks_and_events_survive_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            config = Config(workspace=root, history_db=db)
            first = AgentService(config)
            record = first.create_task("demo", demo=True)
            record.thread.join(timeout=3)
            task_id = record.id
            first.store.close()
            second = AgentService(config)
            restored = second.get_task(task_id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.status, "completed")
            self.assertGreaterEqual(len(second.events(task_id)), 3)
            second.store.close()

    def test_interrupted_task_is_marked_failed_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            config = Config(workspace=root, history_db=db)
            first = AgentService(config)
            record = TaskRecord(id="interrupted", task="demo", status="running")
            first.store.save_task(record.snapshot())
            first.store.close()
            second = AgentService(config)
            restored = second.get_task(record.id)
            self.assertEqual(restored.status, "failed")
            self.assertIn("restarted", restored.error)
            second.store.close()


if __name__ == "__main__":
    unittest.main()
