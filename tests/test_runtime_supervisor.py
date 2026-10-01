import json
import os
import socket
import sys
from pathlib import Path

from server.runtime_registry import load_runtime_registry
from server.runtime_supervisor import RuntimeSupervisor


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_http_server(path: Path) -> None:
    path.write_text(
        """
from http.server import BaseHTTPRequestHandler, HTTPServer
import os

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (os.environ.get("SUPERVISOR_TEST_VALUE", "") + "|" + os.environ.get("CONDA_PREFIX", "")).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", int(os.environ["SUPERVISOR_TEST_PORT"])), Handler).serve_forever()
""".strip(),
        encoding="utf-8",
    )


def write_registry(path: Path, engine_id: str, port: int, script: Path, group="gpu0") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engines": {
                    engine_id: {
                        "enabled": True,
                        "mode": "managed",
                        "executable": sys.executable,
                        "cwd": str(script.parent),
                        "args": [str(script)],
                        "health_url": f"http://127.0.0.1:{port}/health",
                        "endpoint": f"http://127.0.0.1:{port}",
                        "start_on_demand": True,
                        "exclusive_group": group,
                        "startup_timeout": 10,
                        "shutdown_timeout": 5,
                        "env": {
                            "SUPERVISOR_TEST_PORT": str(port),
                            "SUPERVISOR_TEST_VALUE": "isolated",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_environment_removes_parent_conda_markers(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    script = tmp_path / "serve.py"
    write_http_server(script)
    port = free_port()
    write_registry(path, "test-engine", port, script)

    spec = load_runtime_registry(path).get("test-engine")
    monkeypatch.setenv("CONDA_PREFIX", r"C:\Users\test\miniconda3")
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    env = RuntimeSupervisor._environment(spec)

    assert "CONDA_PREFIX" not in env
    assert "CONDA_DEFAULT_ENV" not in env
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["SUPERVISOR_TEST_VALUE"] == "isolated"


def test_supervisor_starts_health_checks_and_stops_owned_runtime(tmp_path):
    path = tmp_path / "registry.json"
    script = tmp_path / "serve.py"
    write_http_server(script)
    port = free_port()
    write_registry(path, "test-engine", port, script)

    supervisor = RuntimeSupervisor(registry_path=path, log_dir=tmp_path / "logs")
    started = supervisor.start("test-engine")
    try:
        assert started["ready"] is True
        assert started["managed_by_supervisor"] is True
        assert started["pid"]
        assert supervisor.ensure_ready("test-engine")["ready"] is True
    finally:
        stopped = supervisor.stop("test-engine")
    assert stopped["status"] == "stopped"
    assert stopped["managed_by_supervisor"] is False
