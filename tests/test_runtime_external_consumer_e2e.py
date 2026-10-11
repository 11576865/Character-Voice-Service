"""Real multiprocess producer/consumer contract tests for external activation.

CVS publishes a file to a **separately running test-only consumer**, which
then starts an HTTP health endpoint. This verifies protocol interoperability
with the fixture, NOT integration with the actual System Supervisor.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from server.runtime_external_activation import pending_activation
from server.runtime_supervisor import (
    RuntimeConflictError, RuntimeSupervisor, RuntimeSupervisorError,
)
from test_runtime_supervisor import free_port


_REPO = Path(__file__).resolve().parents[1]
_CONSUMER = _REPO / "tests" / "fixtures" / "external_file_consumer.py"
_CONTROLLER_SOURCE = """
import sys
from pathlib import Path
from server.runtime_supervisor import RuntimeSupervisor
registry, log_dir, marker = sys.argv[1:]
supervisor = RuntimeSupervisor(registry_path=registry, log_dir=log_dir)
Path(marker).write_text("entered", encoding="utf-8")
supervisor.ensure_ready("index-tts")
"""


def _prepare(tmp_path: Path, *, startup_timeout: float = 7):
    port = free_port()
    request_dir = tmp_path / "control" / "requests"
    status_file = tmp_path / "control" / "status.json"
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "schema_version": 1,
        "engines": {
            "index-tts": {
                "enabled": True,
                "mode": "external",
                "lifecycle_owner": "system-supervisor",
                "runtime_id": "mock-consumer-only",
                "endpoint": f"http://127.0.0.1:{port}",
                "health_url": f"http://127.0.0.1:{port}/health",
                "start_on_demand": True,
                "startup_timeout": startup_timeout,
                "shutdown_timeout": 1,
                "exclusive_group": "gpu-0",
                "external_control": {
                    "mode": "file",
                    "request_dir": str(request_dir),
                    "status_file": str(status_file),
                    "service_key": "IndexTTS",
                },
            }
        },
    }), encoding="utf-8")
    return {
        "registry": registry, "port": port,
        "request_dir": request_dir, "status_file": status_file,
        "supervisor": RuntimeSupervisor(
            registry_path=registry, log_dir=tmp_path / "cvs-logs",
        ),
    }


def _spawn_consumer(tmp_path: Path, fixture: dict, *, released: bool):
    ready = tmp_path / "consumer-inbox-ready"
    seen = tmp_path / "consumer-command-seen.json"
    release = tmp_path / "consumer-permit-health"
    if released:
        release.touch()
    log_path = tmp_path / "consumer.log"
    log = log_path.open("wb")
    try:
        proc = subprocess.Popen(
            [
                sys.executable, str(_CONSUMER),
                str(fixture["request_dir"]), "IndexTTS", "index-tts",
                str(fixture["port"]), str(ready), str(seen),
                str(release), str(fixture["status_file"]),
            ],
            cwd=str(_REPO), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
        )
    finally:
        log.close()
    return proc, ready, seen, release, log_path


def _wait_file(path: Path, proc: subprocess.Popen, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        if proc.poll() is not None:
            raise AssertionError(f"consumer/controller exited before {path}: {proc.returncode}")
        time.sleep(0.03)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_healthy(port: int, proc: subprocess.Popen, timeout: float = 10):
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError("external fixture terminated before health became ready")
        try:
            with urllib.request.urlopen(url, timeout=0.2) as resp:
                if resp.status == 200 and json.load(resp)["status"] == "ready":
                    return
        except (OSError, TimeoutError, ValueError, KeyError):
            pass
        time.sleep(0.05)
    raise AssertionError("external fixture never became healthy")


def _commands(root: Path):
    return sorted(root.glob("*.json"))


def _close(proc: subprocess.Popen):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=8)


def test_independent_consumer_reads_exact_command_and_becomes_healthy(tmp_path):
    fixture = _prepare(tmp_path)
    consumer, ready, seen, release, log = _spawn_consumer(tmp_path, fixture, released=True)
    try:
        _wait_file(ready, consumer)
        first = fixture["supervisor"].ensure_ready("index-tts")
        command = json.loads(_wait_file(seen, consumer))
        assert first["ready"] is True
        assert first["status"] == "external-ready"
        assert command["action"] == "activate-engine"
        assert command["engine_id"] == "index-tts"
        assert command["service_key"] == "IndexTTS"
        assert (fixture["request_dir"] / (command["request_id"] + ".json")).is_file()
        assert pending_activation(fixture["request_dir"], "IndexTTS") is None
        assert len(_commands(fixture["request_dir"])) == 1

        # A new CVS instance recognizes the live endpoint rather than
        # issuing a second activation command.
        another = RuntimeSupervisor(
            registry_path=fixture["registry"], log_dir=tmp_path / "second-cvs",
        )
        again = another.ensure_ready("index-tts")
        assert again["ready"] is True
        assert len(_commands(fixture["request_dir"])) == 1
        ext = again["external_supervisor"]
        assert ext["supervisor_state"] == "ready"
        assert ext["supervisor_session"] == "test-only-external-consumer"
    finally:
        _close(consumer)


def test_unresolved_external_command_is_not_replayed_then_late_health_reconciles(tmp_path):
    fixture = _prepare(tmp_path, startup_timeout=1)
    consumer, ready, seen, release, log = _spawn_consumer(tmp_path, fixture, released=False)
    try:
        _wait_file(ready, consumer)
        with pytest.raises(RuntimeSupervisorError, match="timed out"):
            fixture["supervisor"].ensure_ready("index-tts")
        command = json.loads(_wait_file(seen, consumer))
        pending = pending_activation(fixture["request_dir"], "IndexTTS")
        assert pending["request_id"] == command["request_id"]

        another = RuntimeSupervisor(registry_path=fixture["registry"])
        with pytest.raises(RuntimeConflictError, match="unresolved"):
            another.ensure_ready("index-tts")
        assert len(_commands(fixture["request_dir"])) == 1
        assert another.status("index-tts")["status"] == "external-activation-pending"

        release.touch()
        _wait_healthy(fixture["port"], consumer)
        result = another.ensure_ready("index-tts")
        assert result["ready"] is True
        assert pending_activation(fixture["request_dir"], "IndexTTS") is None
        assert len(_commands(fixture["request_dir"])) == 1
    finally:
        _close(consumer)


def test_killed_cvs_controller_cannot_cause_duplicate_external_command(tmp_path):
    fixture = _prepare(tmp_path, startup_timeout=25)
    consumer, ready, seen, release, log = _spawn_consumer(tmp_path, fixture, released=False)
    parent = None
    try:
        _wait_file(ready, consumer)
        marker = tmp_path / "cvs-controller-entered"
        controller_log = (tmp_path / "controller.log").open("wb")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_REPO) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            parent = subprocess.Popen(
                [sys.executable, "-c", _CONTROLLER_SOURCE,
                 str(fixture["registry"]), str(tmp_path / "controller-logs"), str(marker)],
                cwd=str(_REPO), env=env, stdin=subprocess.DEVNULL,
                stdout=controller_log, stderr=subprocess.STDOUT,
            )
        finally:
            controller_log.close()
        _wait_file(marker, parent)
        command = json.loads(_wait_file(seen, consumer))
        if parent.poll() is None:
            parent.kill()  # crash: no finally block, lease auto-released
        parent.wait(timeout=8)
        assert pending_activation(fixture["request_dir"], "IndexTTS")["request_id"] == command["request_id"]

        replacement = RuntimeSupervisor(
            registry_path=fixture["registry"], log_dir=tmp_path / "replacement-logs",
        )
        with pytest.raises(RuntimeConflictError, match="unresolved"):
            replacement.ensure_ready("index-tts")
        assert len(_commands(fixture["request_dir"])) == 1

        release.touch()
        _wait_healthy(fixture["port"], consumer)
        recovered = replacement.ensure_ready("index-tts")
        assert recovered["ready"] is True
        assert pending_activation(fixture["request_dir"], "IndexTTS") is None
        assert len(_commands(fixture["request_dir"])) == 1
    finally:
        if parent is not None:
            _close(parent)
        _close(consumer)
