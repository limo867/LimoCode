import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
import tempfile
import time

from coding_agent.config import Config
from coding_agent.service import AgentService
from coding_agent.web import ApiHandler
from http.server import ThreadingHTTPServer


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        ApiHandler.service = AgentService(Config(workspace=Path(self.temp_dir.name)))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    def request(self, method, path, body=None):
        connection = HTTPConnection(self.host, self.port, timeout=3)
        payload = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_health_and_demo_task(self):
        status, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        status, body = self.request("POST", "/api/tasks", {"task": "demo", "demo": True})
        self.assertEqual(status, 202)
        task_id = json.loads(body)["id"]
        for _ in range(20):
            status, body = self.request("GET", f"/api/tasks/{task_id}")
            if json.loads(body)["status"] == "completed":
                break
            time.sleep(0.05)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "completed")

    def test_tasks_list_and_event_sequence(self):
        status, body = self.request("POST", "/api/tasks", {"task": "demo", "demo": True})
        task_id = json.loads(body)["id"]
        for _ in range(20):
            time.sleep(0.05)
            record = ApiHandler.service.get_task(task_id)
            if record.status == "completed":
                break
        status, body = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["id"] == task_id for item in json.loads(body)["tasks"]))
        events = ApiHandler.service.events(task_id)
        self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))

    def test_task_and_event_history_pagination(self):
        task_ids = []
        for _ in range(2):
            status, body = self.request("POST", "/api/tasks", {"task": "demo", "demo": True})
            self.assertEqual(status, 202)
            task_ids.append(json.loads(body)["id"])
        for task_id in task_ids:
            for _ in range(20):
                if ApiHandler.service.get_task(task_id).status == "completed":
                    break
                time.sleep(0.05)

        status, body = self.request("GET", "/api/tasks?limit=1&offset=0")
        page = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(page["tasks"]), 1)
        self.assertEqual(page["next_offset"], 1)

        task_id = task_ids[0]
        status, body = self.request("GET", f"/api/tasks/{task_id}/event-log?after=0&limit=1")
        page = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["next_after"], 1)

        status, body = self.request("GET", "/api/tasks?limit=0")
        self.assertEqual(status, 400)
        self.assertIn("limit", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
