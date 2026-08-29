"""Minimal local Web API and SSE server; no Agent framework is involved."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config
from .service import AgentService


class ApiHandler(BaseHTTPRequestHandler):
    service: AgentService
    frontend_root = Path(__file__).resolve().parent.parent / "frontend"

    def _send_json(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def _body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1_000_000:
            raise ValueError("request body is too large")
        body = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        return body

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json(200, {"ok": True, "service": "coding-agent"})
            return
        if parsed.path == "/api/tasks":
            self._send_json(200, {"tasks": self.service.list_tasks()})
            return
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "tasks"] and parts[2]:
            record = self.service.get_task(parts[2])
            if not record:
                self._send_json(404, {"error": "task not found"})
            else:
                self._send_json(200, record.snapshot())
            return
        if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "events":
            self._stream_events(parts[2])
            return
        if parsed.path in {"/", "/index.html"}:
            self._serve_file("index.html", "text/html; charset=utf-8")
            return
        self._send_json(404, {"error": "not found"})

    def _stream_events(self, task_id: str) -> None:
        record = self.service.get_task(task_id)
        if not record:
            self._send_json(404, {"error": "task not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        query = parse_qs(urlparse(self.path).query)
        try:
            sent = max(0, int(query.get("after", ["0"])[0]))
        except (TypeError, ValueError):
            sent = 0
        while True:
            events = self.service.events(task_id, sent)
            for event in events:
                payload = json.dumps(event.to_dict(), ensure_ascii=False)
                self.wfile.write(f"id: {event.id}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                sent = event.sequence
            if record.status in {"completed", "failed", "cancelled"} and not self.service.events(task_id, sent):
                break
            self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()
            if record.cancelled.wait(0.25):
                continue

    def _serve_file(self, name: str, content_type: str) -> None:
        target = self.frontend_root / name
        if not target.is_file():
            self._send_json(404, {"error": "frontend is not built"})
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path != "/api/tasks":
            self._send_json(404, {"error": "not found"})
            return
        try:
            body = self._body()
            demo = body.get("demo")
            if demo is not None and not isinstance(demo, bool):
                raise ValueError("demo must be a boolean")
            record = self.service.create_task(body.get("task", ""), demo=demo)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(202, record.snapshot())

    def do_DELETE(self) -> None:
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
            if self.service.cancel_task(parts[2]):
                self._send_json(202, {"ok": True, "status": "cancelling"})
            else:
                self._send_json(404, {"error": "task not found or already finished"})
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(config: Config, host: str = "127.0.0.1", port: int = 8765, *, demo: bool = False) -> ThreadingHTTPServer:
    service = AgentService(config)
    if demo:
        service.demo_default = True
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"Coding Agent web UI: http://{host}:{port}/ (demo={demo})")
    return server
