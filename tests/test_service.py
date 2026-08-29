import time
from threading import Barrier, Lock
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

    def test_task_status_transitions_are_explicit(self):
        service = AgentService(self.config)
        record = service.create_task("demo", demo=True)
        record.thread.join(timeout=3)
        self.assertEqual(record.status, "completed")
        with self.assertRaises(ValueError):
            record.transition("running")

    def test_concurrent_tasks_share_model_request_rate_limit(self):
        class TimestampModel:
            def __init__(self):
                self.calls = []
                self.lock = Lock()

            def complete(self, messages, tools):
                with self.lock:
                    self.calls.append(time.monotonic())
                return {"content": "done"}

        model = TimestampModel()
        config = Config(workspace=self.config.workspace, model_min_request_interval_ms=100)
        barrier = Barrier(2)

        def model_factory(_config, _demo):
            barrier.wait(timeout=1)
            return model

        service = AgentService(config, model_factory=model_factory)
        records = [service.create_task(f"task {index}") for index in range(2)]
        for record in records:
            record.thread.join(timeout=3)
            self.assertEqual(record.status, "completed")
        self.assertEqual(len(model.calls), 2)
        events = [event for record in records for event in service.events(record.id)]
        waits = [event.data["waited_ms"] for event in events if event.type == "model_rate_limited"]
        self.assertTrue(waits)
        self.assertGreaterEqual(max(waits), 80)

    def test_command_approval_can_be_rejected_without_execution(self):
        class ApprovalModel:
            def __init__(self):
                self.turn = 0

            def complete(self, messages, tools):
                self.turn += 1
                if self.turn == 1:
                    return {"content": "", "tool_calls": [{"name": "run_command", "arguments": '{"command": "shutdown /?"}'}]}
                return {"content": "handled approval result"}

        service = AgentService(Config(workspace=self.config.workspace, command_approval_timeout=3), model_factory=lambda _config, _demo: ApprovalModel())
        record = service.create_task("request approval")
        approval = None
        for _ in range(30):
            approval_events = [event for event in service.events(record.id) if event.type == "command_approval_requested"]
            if approval_events:
                approval = approval_events[0]
                break
            time.sleep(0.05)
        self.assertIsNotNone(approval)
        self.assertTrue(service.approve_command(record.id, approval.data["approval_id"], False))
        record.thread.join(timeout=3)
        self.assertEqual(record.status, "completed")
        finished = [event for event in service.events(record.id) if event.type == "tool_finished"]
        self.assertEqual(finished[0].data["result"]["approval_status"], "rejected")
