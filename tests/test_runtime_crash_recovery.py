"""Runtime crash-loop, diagnostic provenance and restart backoff regressions.

No model weights or GPU are required. Child engines are disposable Python processes.
"""
import json
import socket
import sys
import time
from pathlib import Path

import pytest

from server.runtime_supervisor import (
    CRASH_BACKOFF_BASE_SECONDS,
    CRASH_BACKOFF_MAX_SECONDS,
    CRASH_HISTORY_WINDOW_SECONDS,
    RuntimeSupervisor,
    RuntimeSupervisorError,
)


def _free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def _runtime(tmp_path, script_source: str):
    script = tmp_path / "engine.py"
    script.write_text(script_source, encoding="utf-8")
    attempts = tmp_path / "attempts.log"
    registry_path = tmp_path / "registry.json"
    port = _free_port()
    registry_path.write_text(json.dumps({
        "schema_version": 1,
        "engines": {
            "test-engine": {
                "enabled": True,
                "mode": "managed",
                "executable": sys.executable,
                "cwd": str(tmp_path),
                "args": [str(script)],
                "health_url": f"http://127.0.0.1:{port}/health",
                "endpoint": f"http://127.0.0.1:{port}",
                "start_on_demand": True,
                "startup_timeout": 3,
                "shutdown_timeout": 1,
                "env": {"TEST_PORT": str(port), "TEST_ATTEMPTS": str(attempts)},
            },
        },
    }), encoding="utf-8")
    return RuntimeSupervisor(registry_path=registry_path, log_dir=tmp_path / "logs"), script, attempts


_CRASH = """
import os
import sys
from pathlib import Path
with Path(os.environ["TEST_ATTEMPTS"]).open("a", encoding="utf-8") as log:
    log.write("start\\n")
print("deliberate failure", flush=True)
sys.exit(17)
"""

_HEALTHY = """
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
with Path(os.environ["TEST_ATTEMPTS"]).open("a", encoding="utf-8") as log:
    log.write("start\\n")
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = b'{"status":"ready"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args):
        pass
HTTPServer(("127.0.0.1", int(os.environ["TEST_PORT"])), Handler).serve_forever()
"""


def test_crashed_engine_is_observable_and_does_not_restart_for_each_request(tmp_path, monkeypatch):
    # This test checks *refusal while in cooldown*, not the five-second
    # passage of real Windows CI wall time. A separate pure policy test
    # verifies the actual default 5/10/20/40/60s backoff sequence.
    monkeypatch.setattr("server.runtime_supervisor.CRASH_BACKOFF_BASE_SECONDS", 60.0)
    supervisor, script, attempts = _runtime(tmp_path, _CRASH)
    with pytest.raises(RuntimeSupervisorError, match="exited during startup with code 17"):
        supervisor.ensure_ready("test-engine")

    status = supervisor.status("test-engine")
    assert status["status"] == "restart-backoff"
    assert status["ready"] is False
    assert status["last_exit"]["returncode"] == 17
    assert status["last_exit"]["pid"] > 0
    assert status["last_exit"]["uptime_seconds"] >= 0
    assert Path(status["last_exit"]["log_path"]).is_file()
    assert status["restart_failures"] == 1
    assert status["restart_backoff_seconds"] > 0
    assert status["last_start_failure"] == "unexpected-exit"

    for _ in range(3):
        with pytest.raises(RuntimeSupervisorError, match="restart backoff active"):
            supervisor.ensure_ready("test-engine")
    assert attempts.read_text(encoding="utf-8").splitlines() == ["start"]


def test_admin_explicit_restart_bypasses_cooldown_and_preserves_crash_record(tmp_path):
    supervisor, script, attempts = _runtime(tmp_path, _CRASH)
    with pytest.raises(RuntimeSupervisorError):
        supervisor.ensure_ready("test-engine")
    script.write_text(_HEALTHY, encoding="utf-8")
    try:
        recovery = supervisor.restart("test-engine")
        assert recovery["ready"] is True
        assert recovery["status"] == "ready"
        assert recovery["restart_backoff_seconds"] == 0
        assert recovery["last_exit"]["returncode"] == 17
        assert attempts.read_text(encoding="utf-8").splitlines() == ["start", "start"]
    finally:
        supervisor.stop("test-engine")


def test_normal_stop_never_creates_crash_backoff(tmp_path):
    supervisor, _, _ = _runtime(tmp_path, _HEALTHY)
    supervisor.start("test-engine")
    stopped = supervisor.stop("test-engine")
    assert stopped["status"] == "stopped"
    assert stopped["last_exit"] is None
    assert stopped["restart_failures"] == 0
    assert stopped["restart_backoff_seconds"] == 0


def test_repeated_failures_back_off_exponentially_and_reset_after_history_window(tmp_path):
    supervisor = RuntimeSupervisor(registry_path=tmp_path / "registry.json")
    for expected_count, expected_delay in [(1, 5), (2, 10), (3, 20), (4, 40), (5, 60), (6, 60)]:
        record = supervisor._record_failure("engine", "unexpected-exit")
        assert record["count"] == expected_count
        assert 0 < supervisor._restart_cooldown("engine") <= expected_delay
        assert expected_delay - supervisor._restart_cooldown("engine") < 1.0
    supervisor._failures["engine"]["last_failure_monotonic"] -= CRASH_HISTORY_WINDOW_SECONDS + 1
    reset = supervisor._record_failure("engine", "unexpected-exit")
    assert reset["count"] == 1
    assert 0 < supervisor._restart_cooldown("engine") <= CRASH_BACKOFF_BASE_SECONDS
    assert CRASH_BACKOFF_MAX_SECONDS == 60


def test_process_exiting_after_successful_start_is_recorded_once(tmp_path):
    source = _HEALTHY.replace(
        'HTTPServer(("127.0.0.1", int(os.environ["TEST_PORT"])), Handler).serve_forever()',
        'import threading, time, sys\n'
        'server = HTTPServer(("127.0.0.1", int(os.environ["TEST_PORT"])), Handler)\n'
        'threading.Thread(target=server.serve_forever, daemon=True).start()\n'
        'time.sleep(2.0)\n'
        'sys.exit(19)'
    )
    supervisor, script, attempts = _runtime(tmp_path, source)
    try:
        started = supervisor.start("test-engine")
        assert started["ready"] is True
        deadline = time.monotonic() + 4
        result = None
        while time.monotonic() < deadline:
            result = supervisor.status("test-engine")
            if result["last_exit"]:
                break
            time.sleep(0.05)
        assert result is not None and result["last_exit"]["returncode"] == 19
        assert result["status"] == "restart-backoff"
        assert result["restart_failures"] == 1
        again = supervisor.status("test-engine")
        assert again["restart_failures"] == 1
        assert attempts.read_text(encoding="utf-8").splitlines() == ["start"]
    finally:
        supervisor.shutdown_all()


def test_startup_timeout_creates_backoff_without_falsely_claiming_unexpected_exit(tmp_path):
    supervisor, script, attempts = _runtime(tmp_path, """
import os, time
from pathlib import Path
with Path(os.environ["TEST_ATTEMPTS"]).open("a", encoding="utf-8") as log:
    log.write("start\\n")
time.sleep(15)
""")
    try:
        with pytest.raises(RuntimeSupervisorError, match="did not become healthy"):
            supervisor.start("test-engine")
        state = supervisor.status("test-engine")
        assert state["status"] == "restart-backoff"
        assert state["last_start_failure"] == "startup-timeout"
        assert state["restart_failures"] == 1
        assert state["last_exit"] is None
        with pytest.raises(RuntimeSupervisorError, match="restart backoff active"):
            supervisor.ensure_ready("test-engine")
        assert attempts.read_text(encoding="utf-8").splitlines() == ["start"]
    finally:
        supervisor.shutdown_all()


def test_spawn_oserror_is_classified_and_backed_off(tmp_path, monkeypatch):
    supervisor, _, _ = _runtime(tmp_path, _HEALTHY)

    def reject_spawn(*args, **kwargs):
        raise OSError("injected executable launch failure")

    monkeypatch.setattr("server.runtime_supervisor.subprocess.Popen", reject_spawn)
    with pytest.raises(RuntimeSupervisorError, match="unable to spawn managed runtime"):
        supervisor.start("test-engine")
    state = supervisor.status("test-engine")
    assert state["status"] == "restart-backoff"
    assert state["last_start_failure"] == "spawn-failed"
    assert state["last_exit"] is None
    with pytest.raises(RuntimeSupervisorError, match="restart backoff active"):
        supervisor.start("test-engine")


def test_cooldown_request_uses_bounded_probe_without_full_health_timeout(tmp_path, monkeypatch):
    supervisor, script, attempts = _runtime(tmp_path, _CRASH)
    monkeypatch.setattr("server.runtime_supervisor.CRASH_BACKOFF_BASE_SECONDS", 60.0)
    with pytest.raises(RuntimeSupervisorError, match="exited during startup"):
        supervisor.ensure_ready("test-engine")

    probe_timeouts = []

    def fake_health(spec, timeout=1.5):
        probe_timeouts.append(timeout)
        return False, "offline"

    monkeypatch.setattr(supervisor, "_health", fake_health)
    with pytest.raises(RuntimeSupervisorError, match="restart backoff active"):
        supervisor.ensure_ready("test-engine")
    assert probe_timeouts == [0.2]
    assert attempts.read_text(encoding="utf-8").splitlines() == ["start"]


def test_cooldown_still_recognizes_healthy_external_endpoint(tmp_path, monkeypatch):
    supervisor, script, attempts = _runtime(tmp_path, _CRASH)
    with pytest.raises(RuntimeSupervisorError, match="exited during startup"):
        supervisor.ensure_ready("test-engine")
    # A healthy endpoint may have been taken over by a separately managed
    # server. Circuit breaker must not misreport that existing server offline.
    timeouts = []
    def healthy(spec, timeout=1.5):
        timeouts.append(timeout)
        return True, None
    monkeypatch.setattr(supervisor, "_health", healthy)
    result = supervisor.ensure_ready("test-engine")
    assert result["ready"] is True
    assert timeouts[0] == 0.2
    assert attempts.read_text(encoding="utf-8").splitlines() == ["start"]
