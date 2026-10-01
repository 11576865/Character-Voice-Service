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


def test_external_runtime_activation_writes_supervisor_request(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    request_dir = tmp_path / "control" / "requests"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engines": {
                    "index-tts": {
                        "enabled": True,
                        "mode": "external",
                        "runtime_id": "index-tts-2.5-local",
                        "runtime_version": "2.5",
                        "health_url": "http://127.0.0.1:9882/health",
                        "endpoint": "http://127.0.0.1:9882",
                        "start_on_demand": True,
                        "exclusive_group": "gpu-0",
                        "startup_timeout": 5,
                        "shutdown_timeout": 5,
                        "lifecycle_owner": "system-supervisor",
                        "external_control": {
                            "mode": "file",
                            "request_dir": str(request_dir),
                            "service_key": "IndexTTS",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    supervisor = RuntimeSupervisor(registry_path=registry_path)
    health_results = iter([
        (False, "offline"),
        (False, "starting"),
        (True, None),
        (True, None),
    ])
    monkeypatch.setattr(
        supervisor,
        "_health",
        lambda spec, timeout=1.5: next(health_results, (True, None)),
    )

    result = supervisor.ensure_ready("index-tts")
    assert result is not None

    requests = list(request_dir.glob("*.json"))
    assert len(requests) == 1
    payload = json.loads(requests[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["action"] == "activate-engine"
    assert payload["engine_id"] == "index-tts"
    assert payload["service_key"] == "IndexTTS"
