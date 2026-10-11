"""Disposable external System Supervisor file-bridge consumer (test-only).

Run as an independent OS process. Consume the existing top-level
activate-engine request schema and host a minimal HTTP /health endpoint.
This is NOT the actual external System Supervisor implementation.
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def _wait_for_command(root: Path, service_key: str, engine_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in root.glob("*.json"):
            try:
                command = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                command.get("schema_version") == 1
                and command.get("action") == "activate-engine"
                and command.get("engine_id") == engine_id
                and command.get("service_key") == service_key
                and path.name == command.get("request_id", "") + ".json"
            ):
                return command
        time.sleep(0.025)
    raise TimeoutError("no valid CVS activate-engine command was observed")


def main(argv: list[str]) -> int:
    if len(argv) != 8:
        raise ValueError("expected request_dir, service_key, engine_id, port, ready, seen, release, status_file")
    root, service_key, engine_id, port, ready, seen, release, status_file = argv
    root = Path(root)
    Path(ready).write_text("listening-for-file", encoding="utf-8")
    command = _wait_for_command(root, service_key, engine_id, timeout=30)
    Path(seen).write_text(json.dumps(command), encoding="utf-8")
    while not Path(release).exists():
        time.sleep(0.025)

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            payload = b'{"status":"ready"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", int(port)), HealthHandler)
    # Mock the existing status-file shape, including a nonauthoritative
    # request ID to help the test compare the consumer's observed command.
    Path(status_file).write_text(json.dumps({
        "session_id": "test-only-external-consumer",
        "services": [{
            "key": service_key, "state": "ready",
            "detail": "test-only file bridge",
            "last_request_id": command["request_id"],
        }],
    }), encoding="utf-8")
    print("consumer serving health request_id=" + command["request_id"], flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
