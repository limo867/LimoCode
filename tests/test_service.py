import time
import unittest
from pathlib import Path
import tempfile

from coding_agent.config import Config
from coding_agent.service import AgentService
from coding_agent.events import AgentEvent


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = Config(workspace=Path(self.temp_dir.name))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_task_lifecycle_emits_public_events(self):
        service = AgentService(self.config)
        record = service.create_task("list files", demo=True)
        record.thread.join(timeout=3)
        self.assertEqual(record.status, "completed")
        event_types = [event.type for event in service.events(record.id)]
        self.assertIn("task_started", event_types)
        self.assertIn("assistant_message", event_types)
        self.assertIn("task_finished", event_types)

    def test_invalid_task_is_rejected(self):
        with self.assertRaises(ValueError):
            AgentService(self.config).create_task(" ", demo=True)

    def test_event_serialization_is_json_ready(self):
        event = AgentEvent("task_started", "abc", {"task": "demo"})
        payload = event.to_dict()
        self.assertEqual(payload["type"], "task_started")
        self.assertEqual(payload["task_id"], "abc")

    def test_events_can_resume_from_sequence(self):
        service = AgentService(self.config)
        record = service.create_task("list files", demo=True)
        record.thread.join(timeout=3)
        events = service.events(record.id)
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(service.events(record.id, after=2)[0].sequence, 3)
