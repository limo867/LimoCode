import tempfile
import unittest
from pathlib import Path

from coding_agent.config import Config
from coding_agent.events import AgentEvent
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

    def test_all_persisted_events_remain_available_after_memory_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(workspace=root, history_db=root / "state.sqlite3")
            first = AgentService(config)
            record = TaskRecord(id="long-history", task="demo", status="completed")
            first.store.save_task(record.snapshot())
            for sequence in range(1, 506):
                first.store.save_event(AgentEvent("tool_finished", record.id, {"index": sequence}, sequence=sequence))
            first.store.close()

            second = AgentService(config)
            self.assertEqual(second.get_task(record.id).snapshot()["event_count"], 505)
            self.assertEqual(len(second.events(record.id)), 505)
            self.assertEqual([event.sequence for event in second.events(record.id, after=499, limit=3)], [500, 501, 502])
            second.store.close()

    def test_concurrent_persisted_tasks_survive_restart(self):
        class FastModel:
            def complete(self, messages, tools):
                return {"content": "done"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(workspace=root, history_db=root / "state.sqlite3")
            first = AgentService(config, model_factory=lambda _config, _demo: FastModel())
            records = [first.create_task(f"stress task {index}") for index in range(20)]
            for record in records:
                record.thread.join(timeout=5)
                self.assertEqual(record.status, "completed")
                events = first.events(record.id)
                self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
            task_ids = {record.id for record in records}
            first.store.close()

            second = AgentService(config)
            self.assertEqual({item["id"] for item in second.list_tasks()}, task_ids)
            self.assertTrue(all(second.get_task(task_id).status == "completed" for task_id in task_ids))
            second.store.close()

    def test_persisted_session_can_continue_after_service_restart(self):
        requests = []

        class NamedModel:
            def __init__(self, name):
                self.name = name

            def complete(self, messages, tools):
                requests.append((self.name, messages))
                return {"content": f"response from {self.name}"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(workspace=root, history_db=root / "state.sqlite3", model="first")
            first_service = AgentService(config, model_factory=lambda active, _demo: NamedModel(active.model))
            first = first_service.create_task("Inspect the project")
            first.thread.join(timeout=3)
            first_service.store.close()

            resumed_config = config.with_overrides(model="second")
            second_service = AgentService(resumed_config, model_factory=lambda active, _demo: NamedModel(active.model))
            continued = second_service.create_task("Continue with verification", resume_from=first.id)
            continued.thread.join(timeout=3)
            self.assertEqual(continued.result, "response from second")
            self.assertTrue(any(message.get("content") == "response from first" for message in requests[-1][1]))
            self.assertTrue(any(message.get("content") == "Continue with verification" for message in requests[-1][1]))
            second_service.store.close()


if __name__ == "__main__":
    unittest.main()
