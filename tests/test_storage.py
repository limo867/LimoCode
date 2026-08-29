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

    def test_pruned_tasks_are_removed_from_persistent_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(workspace=root, history_db=root / "state.sqlite3")
            first = AgentService(config)
            first.max_tasks = 1
            old = first.create_task("first", demo=True)
            old.thread.join(timeout=3)
            newest = first.create_task("second", demo=True)
            newest.thread.join(timeout=3)
            self.assertIsNone(first.get_task(old.id))
            self.assertIsNotNone(first.get_task(newest.id))
            first.store.close()

            second = AgentService(config)
            self.assertIsNone(second.get_task(old.id))
            self.assertIsNotNone(second.get_task(newest.id))
            second.store.close()

    def test_concurrent_demo_tasks_keep_independent_event_sequences(self):
        with tempfile.TemporaryDirectory() as directory:
            service = AgentService(Config(workspace=Path(directory)))
            records = [service.create_task(f"demo {index}", demo=True) for index in range(4)]
            for record in records:
                record.thread.join(timeout=5)
                self.assertEqual(record.status, "completed")
                sequences = [event.sequence for event in service.events(record.id)]
                self.assertEqual(sequences, list(range(1, len(sequences) + 1)))


if __name__ == "__main__":
    unittest.main()
