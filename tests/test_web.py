import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
import tempfile

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
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
