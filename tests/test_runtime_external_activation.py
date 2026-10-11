"""External System Supervisor activation single-flight and stale-state regressions.

All tests use a mocked external engine and local request directory. They
never start user TTS model processes or claim System Supervisor support.
"""
import json
import threading
import time
from pathlib import Path

import pytest

from server.runtime_external_activation import (
    ExternalActivationUncertain,
    acknowledge_activation,
    acquire_activation_lease,
    pending_activation,
    publish_activation,
)
from server.runtime_registry import load_runtime_registry
from server.runtime_supervisor import (
    RuntimeConflictError,
    RuntimeSupervisor,
    RuntimeSupervisorError,
)


def _external(tmp_path, *, service_key="IndexTTS", status_file=None):
    request_dir = tmp_path / "control" / "requests"
    registry = tmp_path / ("registry-" + service_key + ".json")
    control = {"mode": "file", "request_dir": str(request_dir), "service_key": service_key}
    if status_file:
        control["status_file"] = str(status_file)
    registry.write_text(json.dumps({
        "schema_version": 1,
        "engines": {"index-tts": {
            "enabled": True,
            "mode": "external",
            "runtime_id": "external-indextts-local",
            "endpoint": "http://127.0.0.1:9882",
            "health_url": "http://127.0.0.1:9882/health",
            "start_on_demand": True,
            "startup_timeout": 1,
            "shutdown_timeout": 1,
            "exclusive_group": "gpu-0",
            "lifecycle_owner": "system-supervisor",
            "external_control": control,
        }},
    }), encoding="utf-8")
    sup = RuntimeSupervisor(registry_path=registry, log_dir=tmp_path / "logs")
    return sup, sup.registry().get("index-tts"), request_dir


def _commands(root):
    return sorted(
        p for p in root.glob("*.json") if not p.name.startswith(".")
    )


def test_activation_receipt_created_before_one_atomic_command(tmp_path):
    _, _, root = _external(tmp_path)
    lease = acquire_activation_lease(root, "IndexTTS")
    assert lease is not None
    try:
        record = publish_activation(root, "IndexTTS", "index-tts")
        assert record["phase"] == "pending"
        assert record["request_id"] == pending_activation(root, "IndexTTS")["request_id"]
        commands = _commands(root)
        assert len(commands) == 1
        payload = json.loads(commands[0].read_text(encoding="utf-8"))
        assert payload["action"] == "activate-engine"
        assert payload["request_id"] == record["request_id"]
        assert payload["service_key"] == "IndexTTS"
        assert not list(root.glob("*.tmp"))
        with pytest.raises(ExternalActivationUncertain, match="unresolved"):
            publish_activation(root, "IndexTTS", "index-tts")
    finally:
        lease.release()


def test_cross_thread_activation_has_single_writer_and_acknowledges_only_live_health(tmp_path, monkeypatch):
    first, spec, root = _external(tmp_path)
    second, _, _ = _external(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    outcome = {}
    calls = 0

    def live_health(spec, timeout=1.5):
        nonlocal calls
        calls += 1
        if calls == 1:
            return False, "offline"  # first probe under lease
        entered.set()                # after command publication
        assert release.wait(5)
        return True, None

    monkeypatch.setattr(first, "_health", live_health)
    monkeypatch.setattr(second, "_health", lambda spec, timeout=1.5: (False, "offline"))

    def activate_first():
        try:
            outcome["status"] = first._request_external_activation(spec)
        except BaseException as exc:
            outcome["error"] = repr(exc)

    worker = threading.Thread(target=activate_first)
    worker.start()
    try:
        assert entered.wait(3)
        assert len(_commands(root)) == 1
        with pytest.raises(RuntimeConflictError, match="already in progress"):
            second._request_external_activation(spec)
        assert len(_commands(root)) == 1
    finally:
        release.set()
        worker.join(timeout=6)
    assert not worker.is_alive()
    assert "error" not in outcome, outcome
    assert outcome["status"]["ready"] is True
    assert pending_activation(root, "IndexTTS") is None
    assert len(_commands(root)) == 1


def test_timeout_leaves_receipt_and_followup_never_replays_command(tmp_path, monkeypatch):
    sup, spec, root = _external(tmp_path)
    monkeypatch.setattr(sup, "_health", lambda spec, timeout=1.5: (False, "offline"))
    with pytest.raises(RuntimeSupervisorError, match="timed out"):
        sup._request_external_activation(spec)
    old = pending_activation(root, "IndexTTS")
    assert old is not None
    assert len(_commands(root)) == 1
    with pytest.raises(RuntimeConflictError, match=old["request_id"]):
        sup._request_external_activation(spec)
    assert len(_commands(root)) == 1


def test_later_live_health_reconciles_pending_without_new_command(tmp_path, monkeypatch):
    sup, spec, root = _external(tmp_path)
    lease = acquire_activation_lease(root, "IndexTTS")
    try:
        old = publish_activation(root, "IndexTTS", "index-tts")
    finally:
        lease.release()
    monkeypatch.setattr(sup, "_health", lambda spec, timeout=1.5: (True, None))
    state = sup._request_external_activation(spec)
    assert state["ready"] is True
    assert pending_activation(root, "IndexTTS") is None
    assert len(_commands(root)) == 1


def test_corrupt_pending_receipt_fails_closed_without_new_command(tmp_path, monkeypatch):
    sup, spec, root = _external(tmp_path)
    lease = acquire_activation_lease(root, "IndexTTS")
    try:
        record = publish_activation(root, "IndexTTS", "index-tts")
    finally:
        lease.release()
    receipt_file = next(root.glob(".external-*.pending.json"))
    receipt_file.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(sup, "_health", lambda spec, timeout=1.5: (False, "offline"))
    with pytest.raises(RuntimeConflictError, match="corrupt activation receipt"):
        sup._request_external_activation(spec)
    assert len(_commands(root)) == 1


def test_distinct_service_keys_have_independent_activation_leases(tmp_path):
    root = tmp_path / "requests"
    first = acquire_activation_lease(root, "IndexTTS")
    assert first is not None
    try:
        assert acquire_activation_lease(root, "IndexTTS") is None
        other = acquire_activation_lease(root, "GPT-SoVITS")
        assert other is not None
        other.release()
    finally:
        first.release()


def test_missing_external_control_request_directory_is_rejected(tmp_path, monkeypatch):
    sup, spec, _ = _external(tmp_path)
    from dataclasses import replace
    misconfigured = replace(spec, external_control={"mode": "file", "service_key": "IndexTTS"})
    monkeypatch.setattr(sup, "_health", lambda spec, timeout=1.5: (False, "offline"))
    with pytest.raises(RuntimeSupervisorError, match="requires request_dir"):
        sup._request_external_activation(misconfigured)


def test_stale_external_status_never_overrides_live_ready_or_busy(tmp_path, monkeypatch):
    status_file = tmp_path / "supervisor.json"
    status_file.write_text(json.dumps({
        "session_id": "old-session",
        "services": [{"key": "IndexTTS", "state": "stopped"}]
    }), encoding="utf-8")
    sup, spec, _ = _external(tmp_path, status_file=status_file)
    monkeypatch.setattr(sup, "_health", lambda spec, timeout=1.5: (True, None))
    current = sup.status("index-tts")
    assert current["ready"] is True
    assert current["status"] == "external-ready"
    assert current["external_supervisor"]["supervisor_state"] == "stopped"

    monkeypatch.setattr(sup, "_health", lambda spec, timeout=1.5: (True, "busy"))
    busy = sup.status("index-tts")
    assert busy["status"] == "busy"
    assert busy["ready"] is True

    status_file.write_text(json.dumps({
        "services": [{"key": "IndexTTS", "state": "ready"}]
    }), encoding="utf-8")
    monkeypatch.setattr(sup, "_health", lambda spec, timeout=1.5: (False, "timeout"))
    offline = sup.status("index-tts")
    assert offline["ready"] is False
    assert offline["status"] == "external-unverified"


def test_receipt_can_only_be_cleared_by_matching_request_identity(tmp_path):
    root = tmp_path / "requests"
    lock = acquire_activation_lease(root, "IndexTTS")
    assert lock is not None
    try:
        req = publish_activation(root, "IndexTTS", "index-tts")
        with pytest.raises(ExternalActivationUncertain, match="identity changed"):
            acknowledge_activation(root, "IndexTTS", "wrong")
        assert pending_activation(root, "IndexTTS") == req
        acknowledge_activation(root, "IndexTTS", req["request_id"])
        assert pending_activation(root, "IndexTTS") is None
    finally:
        lock.release()
