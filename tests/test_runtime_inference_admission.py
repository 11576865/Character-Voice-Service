"""GPU group lifecycle and in-flight audio correctness contract.

Disposable live HTTP managed engine and independent Python worker;
no actual GPU or model weights are needed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from server import engines
from server.runtime_inference_admission import (
    RuntimeGroupBusy,
    RuntimeGroupUnresolved,
    _scope,
    runtime_group_guard,
)
from server.runtime_slot_lease import acquire_runtime_slot
from server.runtime_supervisor import RuntimeConflictError, RuntimeSupervisor
from test_runtime_process_tree import _setup
from test_runtime_supervisor import free_port


_ROOT = Path(__file__).resolve().parents[1]
_WORKER = _ROOT / "tests" / "fixtures" / "runtime_group_activity_worker.py"


def _spawn_activity(tmp_path, slot_dir, group="gpu0", engine="engine"):
    entered = tmp_path / "activity-entered"
    done = tmp_path / "activity-done"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.Popen(
        [sys.executable, str(_WORKER), str(slot_dir), group, engine, str(entered), str(done)],
        cwd=str(_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 10
    while not entered.exists() and p.poll() is None and time.monotonic() < deadline:
        time.sleep(0.03)
    assert entered.exists(), p.communicate(timeout=2) if p.poll() is not None else "activity worker timed out"
    return p, done


def _finish(p, done):
    done.touch()
    try:
        out, err = p.communicate(timeout=8)
        assert p.returncode == 0, (out, err)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate(timeout=5)
        raise


def test_inflight_other_process_blocks_admin_stop_and_preserves_managed_engine(tmp_path):
    supervisor, _, slot_dir = _setup(tmp_path)
    p = None
    try:
        started = supervisor.start("engine")
        assert started["ready"]
        p, done = _spawn_activity(tmp_path, slot_dir)
        with pytest.raises(RuntimeConflictError, match="lifecycle admission denied"):
            supervisor.stop("engine")
        assert supervisor._owned["engine"].process.poll() is None
        assert p.poll() is None
        _finish(p, done)
        assert supervisor.stop("engine")["status"] == "stopped"
    finally:
        if p is not None and p.poll() is None:
            p.kill()
            p.communicate(timeout=5)
        supervisor.shutdown_all()


def test_inflight_group_blocks_switch_to_different_managed_engine(tmp_path):
    supervisor, _, slot_dir = _setup(tmp_path)
    registry = Path(supervisor._registry_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    other = dict(payload["engines"]["engine"])
    other["env"] = dict(other.get("env") or {})
    other_port = free_port()
    other["endpoint"] = f"http://127.0.0.1:{other_port}"
    other["health_url"] = f"http://127.0.0.1:{other_port}/health"
    other["env"]["TEST_PORT"] = str(other_port)
    payload["engines"]["other"] = other
    registry.write_text(json.dumps(payload), encoding="utf-8")

    p = None
    try:
        supervisor.start("engine")
        p, done = _spawn_activity(tmp_path, slot_dir)
        with pytest.raises(RuntimeConflictError, match="lifecycle admission denied"):
            supervisor.start("other")
        assert supervisor._owned["engine"].process.poll() is None
        assert "other" not in supervisor._owned
        _finish(p, done)
        other_result = supervisor.start("other")
        assert other_result["ready"] is True
        assert "engine" not in supervisor._owned
    finally:
        if p is not None and p.poll() is None:
            p.kill()
            p.communicate(timeout=5)
        supervisor.shutdown_all()


def test_actual_controller_death_leaves_inference_receipt_blocking_stop(tmp_path):
    supervisor, _, slot_dir = _setup(tmp_path)
    p = None
    try:
        supervisor.start("engine")
        p, done = _spawn_activity(tmp_path, slot_dir)
        p.kill()
        p.communicate(timeout=5)
        _, activity_lock_id, receipt = _scope("gpu0", slot_dir)
        assert receipt.is_file()
        assert json.loads(receipt.read_text(encoding="utf-8"))["phase"] == "inference-pending"

        # The OS lock is released by parent death, but the old remote
        # inference may still be working. Both admin stop and starting
        # another managed runtime must be rejected.
        lock = acquire_runtime_slot(activity_lock_id, slot_dir)
        assert lock is not None
        lock.release()
        with pytest.raises(RuntimeConflictError, match="previous inference outcome unresolved"):
            supervisor.stop("engine")
        with pytest.raises(RuntimeConflictError, match="previous inference outcome unresolved"):
            supervisor.restart("engine")
        assert supervisor._owned["engine"].process.poll() is None
        # Test-only manual reconciliation is safe because this fixture
        # makes no real HTTP inference. Production operators must inspect
        # real GPU/backend evidence first.
        lock = acquire_runtime_slot(activity_lock_id, slot_dir)
        assert lock is not None
        try:
            receipt.unlink()
        finally:
            lock.release()
        supervisor.stop("engine")
    finally:
        if p is not None and p.poll() is None:
            p.kill()
            p.communicate(timeout=5)
        supervisor.shutdown_all()


def test_real_engines_synthesis_holds_group_admission_through_audio_return(tmp_path, monkeypatch):
    supervisor, _, slot_dir = _setup(tmp_path)
    monkeypatch.setattr(engines, "runtime_supervisor", supervisor)
    begun = threading.Event()
    release = threading.Event()
    results = {}

    class SlowAdapter:
        def synthesize(self, text, speed, profile):
            begun.set()
            assert release.wait(timeout=9)
            return b"simulated-audio"

    monkeypatch.setattr(engines, "get_adapter", lambda key: SlowAdapter())
    profile = {"selected_model": {"engine": "engine"}}
    try:
        supervisor.start("engine")

        def produce():
            try:
                results["audio"] = engines.synthesize("A", 1.0, profile)
            except BaseException as exc:
                results["error"] = repr(exc)

        thread = threading.Thread(target=produce)
        thread.start()
        try:
            assert begun.wait(6)
            _, _, receipt = _scope("gpu0", slot_dir)
            assert receipt.exists()
            with pytest.raises(RuntimeConflictError, match="lifecycle admission denied"):
                supervisor.stop("engine")
            assert supervisor._owned["engine"].process.poll() is None
        finally:
            release.set()
            thread.join(timeout=12)
        assert not thread.is_alive()
        assert "error" not in results, results
        assert results["audio"] == b"simulated-audio"
        assert not receipt.exists()
        supervisor.stop("engine")
    finally:
        release.set()
        supervisor.shutdown_all()


def test_inference_failure_persists_uncertain_group_receipt_until_operator_action(tmp_path):
    root = tmp_path / "slots"
    with runtime_group_guard("gpu0", root) as guard:
        request = guard.begin_inference("engine")
        assert request
        # Simulate network failure after a submitted inference without
        # finish_inference. Exiting the OS lock must not clear evidence.
    with pytest.raises(RuntimeGroupUnresolved, match="previous inference outcome unresolved"):
        with runtime_group_guard("gpu0", root):
            pass


def test_thread_local_nested_lifecycle_acquisition_is_allowed_but_other_thread_denied(tmp_path):
    root = tmp_path / "slots"
    seen = []

    with runtime_group_guard("gpu0", root) as first:
        with runtime_group_guard("gpu0", root) as second:
            assert second is first
            worker = threading.Thread(target=lambda: _check_refusal(root, seen))
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert seen == ["busy"]

    with runtime_group_guard("gpu0", root) as acquired:
        assert acquired is not None


def _check_refusal(root, seen):
    try:
        with runtime_group_guard("gpu0", root):
            seen.append("unexpected")
    except RuntimeGroupBusy:
        seen.append("busy")


def test_rejected_local_request_does_not_permanently_poison_gpu_group(tmp_path, monkeypatch):
    supervisor, _, slot_dir = _setup(tmp_path)
    monkeypatch.setattr(engines, "runtime_supervisor", supervisor)

    class InvalidInputAdapter:
        def synthesize(self, text, speed, profile):
            raise HTTPException(status_code=400, detail="invalid speed")

    monkeypatch.setattr(engines, "get_adapter", lambda key: InvalidInputAdapter())
    profile = {"selected_model": {"engine": "engine"}}
    try:
        supervisor.start("engine")
        with pytest.raises(HTTPException) as exc:
            engines.synthesize("A", 0, profile)
        assert exc.value.status_code == 400
        _, _, receipt = _scope("gpu0", slot_dir)
        assert not receipt.exists()
        # A locally rejected request must not disable future lifecycle
        # operations or require an operator to delete a stale marker.
        assert supervisor.stop("engine")["status"] == "stopped"
    finally:
        supervisor.shutdown_all()


def test_group_guard_allows_nested_start_before_synthesizing(tmp_path, monkeypatch):
    supervisor, _, slot_dir = _setup(tmp_path)
    monkeypatch.setattr(engines, "runtime_supervisor", supervisor)

    class ImmediateAdapter:
        def synthesize(self, text, speed, profile):
            return b"fixture-audio"

    monkeypatch.setattr(engines, "get_adapter", lambda key: ImmediateAdapter())
    try:
        assert engines.synthesize(
            "A", 1.0, {"selected_model": {"engine": "engine"}}
        ) == b"fixture-audio"
        assert "engine" in supervisor._owned
        _, _, receipt = _scope("gpu0", slot_dir)
        assert not receipt.exists()
    finally:
        supervisor.shutdown_all()
