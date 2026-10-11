"""Fail-closed runtime owner receipts after cross-process parent exit.

Mock engines use disposable Python HTTP servers; no GPU is required.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from server import runtime_orphan_receipt as receipt
from server.runtime_slot_lease import acquire_runtime_slot
from server.runtime_supervisor import RuntimeConflictError, RuntimeSupervisor, RuntimeSupervisorError
from test_runtime_supervisor import free_port, write_http_server, write_registry


def _supervisor(tmp_path):
    root = tmp_path / "runtime-slots"
    script = tmp_path / "serve.py"
    write_http_server(script)
    registry = tmp_path / "runtime.json"
    write_registry(registry, "engine", free_port(), script, group="gpu0")
    return RuntimeSupervisor(registry_path=registry, log_dir=tmp_path / "logs", slot_dir=root), root


def _with_group_lease(root, fn):
    lease = acquire_runtime_slot("gpu0", root)
    assert lease is not None
    try:
        return fn()
    finally:
        lease.release()


def test_managed_runtime_receipt_tracks_running_pid_and_clears_on_normal_stop(tmp_path):
    supervisor, root = _supervisor(tmp_path)
    try:
        result = supervisor.start("engine")
        assert result["ready"]
        record = receipt.read_receipt("gpu0", root)
        assert record["state"] == "running"
        assert record["engine_id"] == "engine"
        assert record["pid"] == result["pid"]
        assert record["process_identity"]
        assert receipt.process_identity(record["pid"]) == ("alive", record["process_identity"])
    finally:
        supervisor.stop("engine")
    assert receipt.read_receipt("gpu0", root) is None


def test_live_engine_receipt_blocks_new_parent_after_lock_owner_exits(tmp_path):
    supervisor, root = _supervisor(tmp_path)
    # Represent an old parent's child whose OS lease is no longer owned:
    # the process identity is real; only the launch receipt remains.
    def publish():
        launch = receipt.publish_starting("gpu0", root, "prior-engine")
        receipt.publish_running("gpu0", root, launch, os.getpid())
        return launch
    launch = _with_group_lease(root, publish)
    try:
        # The former parent's slot lease is no longer held, despite the
        # surviving child identity in its durable receipt.
        recovered_lease = acquire_runtime_slot("gpu0", root)
        assert recovered_lease is not None
        recovered_lease.release()
        with pytest.raises(RuntimeConflictError, match="ownership unresolved"):
            supervisor.start("engine")
        status = supervisor.status("engine")
        assert status["status"] == "ownership-unresolved"
        assert status["owner_receipt"]["pid"] == os.getpid()
        record = receipt.read_receipt("gpu0", root)
        assert record["launch_id"] == launch
        assert record["pid"] == os.getpid()
        assert "engine" not in supervisor._owned
    finally:
        _with_group_lease(root, lambda: receipt.clear_launch_receipt("gpu0", root, launch))


def test_interrupted_starting_receipt_fails_closed_without_guessing_no_child(tmp_path):
    supervisor, root = _supervisor(tmp_path)
    launch = _with_group_lease(
        root, lambda: receipt.publish_starting("gpu0", root, "prior-engine")
    )
    try:
        with pytest.raises(RuntimeConflictError, match="interrupted startup receipt"):
            supervisor.start("engine")
        assert receipt.read_receipt("gpu0", root)["state"] == "starting"
    finally:
        _with_group_lease(root, lambda: receipt.clear_launch_receipt("gpu0", root, launch))


def test_corrupt_or_unrecognized_receipt_never_permits_launch(tmp_path):
    supervisor, root = _supervisor(tmp_path)
    root.mkdir()
    path = receipt.receipt_path("gpu0", root)
    path.write_text('{"schema_version": 99}', encoding="utf-8")
    with pytest.raises(RuntimeConflictError, match="unsupported runtime owner receipt"):
        supervisor.start("engine")
    state = supervisor.status("engine")
    assert state["status"] == "ownership-unresolved"
    assert state["owner_receipt"]["state"] == "unreadable"
    assert path.exists()


def test_confirmed_dead_child_receipt_is_reconciled_before_new_start(tmp_path):
    supervisor, root = _supervisor(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(5)"])
    try:
        launch = _with_group_lease(root, lambda: receipt.publish_starting("gpu0", root, "prior-engine"))
        _with_group_lease(root, lambda: receipt.publish_running("gpu0", root, launch, child.pid))
        child.kill()
        child.wait(timeout=5)
        assert receipt.read_receipt("gpu0", root) is not None
        try:
            started = supervisor.start("engine")
            assert started["ready"] is True
            current = receipt.read_receipt("gpu0", root)
            assert current["launch_id"] != launch
            assert current["pid"] == started["pid"]
        finally:
            supervisor.stop("engine")
        assert receipt.read_receipt("gpu0", root) is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_pid_reuse_identity_mismatch_is_reconciled_under_lock(tmp_path, monkeypatch):
    root = tmp_path / "runtime-slots"
    launch = _with_group_lease(root, lambda: receipt.publish_starting("gpu0", root, "old"))
    _with_group_lease(root, lambda: receipt.publish_running("gpu0", root, launch, os.getpid()))
    monkeypatch.setattr(receipt, "process_identity", lambda pid: ("alive", "definitely-different-token"))
    _with_group_lease(root, lambda: receipt.reconcile_prior_owner("gpu0", root))
    assert receipt.read_receipt("gpu0", root) is None


def test_startup_spawn_error_leaves_ambiguous_intent_for_next_supervisor(tmp_path, monkeypatch):
    supervisor, root = _supervisor(tmp_path)
    def no_process(*args, **kwargs):
        raise OSError("injected spawn failure")
    monkeypatch.setattr("server.runtime_supervisor.subprocess.Popen", no_process)
    with pytest.raises(RuntimeSupervisorError, match="unable to spawn managed runtime"):
        supervisor.start("engine")
    record = receipt.read_receipt("gpu0", root)
    assert record["state"] == "starting"
    recovered_lease = acquire_runtime_slot("gpu0", root)
    assert recovered_lease is not None
    recovered_lease.release()
    second, _ = _supervisor(tmp_path)
    with pytest.raises(RuntimeConflictError, match="interrupted startup receipt"):
        second.start("engine")
    _with_group_lease(root, lambda: receipt.clear_launch_receipt("gpu0", root, record["launch_id"]))


def test_unexpected_managed_exit_clears_receipt_only_after_poll(tmp_path):
    supervisor, root = _supervisor(tmp_path)
    started = supervisor.start("engine")
    process = supervisor._owned["engine"].process
    process.kill()
    process.wait(timeout=5)
    assert receipt.read_receipt("gpu0", root)["pid"] == started["pid"]
    status = supervisor.status("engine")
    assert status["last_exit"]["pid"] == started["pid"]
    assert receipt.read_receipt("gpu0", root) is None


def test_receipt_clear_rejects_another_launch_id(tmp_path):
    root = tmp_path / "slots"
    launch = _with_group_lease(root, lambda: receipt.publish_starting("gpu0", root, "engine"))
    try:
        with pytest.raises(receipt.UncertainRuntimeOwner, match="different owner's receipt"):
            _with_group_lease(root, lambda: receipt.clear_launch_receipt("gpu0", root, "wrong-launch"))
        assert receipt.read_receipt("gpu0", root)["launch_id"] == launch
    finally:
        _with_group_lease(root, lambda: receipt.clear_launch_receipt("gpu0", root, launch))
