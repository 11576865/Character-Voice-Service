"""Crash-injection acceptance tests for CVS parent versus managed engine ownership.

Uses independent Python interpreters, a real process tree and abrupt
parent termination. No TTS weights, GPU or external Supervisor required.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from server.runtime_orphan_receipt import (
    UncertainRuntimeOwner,
    clear_launch_receipt,
    process_identity,
    read_receipt,
)
from server.runtime_process_tree import _live_posix_group_members
from server.runtime_slot_lease import acquire_runtime_slot
from server.runtime_supervisor import RuntimeConflictError, RuntimeSupervisor
from test_runtime_process_tree import _alive, _setup, _wait_dead
from test_runtime_supervisor import free_port


_REPO = Path(__file__).resolve().parents[1]
_RUN_PARENT = """
import sys, time
from pathlib import Path
from server.runtime_supervisor import RuntimeSupervisor
registry, slot_dir, log_dir, marker = sys.argv[1:]
sup = RuntimeSupervisor(registry_path=registry, slot_dir=slot_dir, log_dir=log_dir)
started = sup.start("engine")
Path(marker).write_text(str(started["pid"]), encoding="utf-8")
while True:
    time.sleep(0.2)
"""

_BLOCK_BEFORE_POPEN = """
import sys, time
from pathlib import Path
from server import runtime_supervisor as mod
registry, slot_dir, log_dir, marker = sys.argv[1:]
original = mod.subprocess.Popen
def interrupt_after_receipt(*args, **kwargs):
    Path(marker).write_text("intent-published", encoding="utf-8")
    while True:
        time.sleep(0.2)
mod.subprocess.Popen = interrupt_after_receipt
sup = mod.RuntimeSupervisor(registry_path=registry, slot_dir=slot_dir, log_dir=log_dir)
sup.start("engine")
"""


def _parent(tmp_path, registry, root, source):
    log = (tmp_path / "controller.log").open("wb")
    signal_file = tmp_path / "controller-ready.txt"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        parent = subprocess.Popen(
            [sys.executable, "-c", source,
             str(registry), str(root), str(tmp_path / "logs-controller"), str(signal_file)],
            cwd=str(_REPO), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
    finally:
        log.close()
    return parent, signal_file


def _wait_file(path, process, *, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        if process.poll() is not None:
            data = (path.parent / "controller.log").read_text(encoding="utf-8", errors="replace")
            raise AssertionError(f"CVS parent failed before marker: {data}")
        time.sleep(0.04)
    raise AssertionError(f"CVS parent did not publish marker: {path}")


def _kill_parent(parent):
    if parent.poll() is None:
        parent.kill()  # abrupt: no shutdown hooks, finally blocks, or cleanup
    parent.wait(timeout=8)


def _verify_owner_dead_and_cleanup(root_pid, descendant, identity):
    # Never signal an unrelated process if the short-lived test PID was reused.
    if root_pid:
        state, current = process_identity(root_pid)
        if state == "alive" and current == identity:
            try:
                if os.name == "nt":
                    os.kill(root_pid, signal.SIGTERM)
                elif _live_posix_group_members(root_pid):
                    os.killpg(root_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if descendant:
        try:
            _wait_dead(descendant, timeout=8)
        except AssertionError:
            # Test cleanup must not leave a runaway sleep/GPU-like worker.
            if os.name == "nt":
                try:
                    os.kill(descendant, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                try:
                    os.kill(descendant, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            _wait_dead(descendant, timeout=5)


def _replacement_registry(tmp_path, previous):
    new_path = tmp_path / "replacement.json"
    payload = json.loads(Path(previous).read_text(encoding="utf-8"))
    record = payload["engines"].pop("engine")
    other_port = free_port()
    record["endpoint"] = f"http://127.0.0.1:{other_port}"
    record["health_url"] = f"http://127.0.0.1:{other_port}/health"
    record.setdefault("env", {})["TEST_PORT"] = str(other_port)
    payload["engines"]["replacement"] = record
    new_path.write_text(json.dumps(payload), encoding="utf-8")
    return new_path


def test_actual_cvs_parent_kill_retains_live_engine_and_denies_second_owner(tmp_path):
    fixture, descendant_marker, slot_dir = _setup(tmp_path)
    registry = fixture._registry_path
    controller, ready_marker = _parent(tmp_path, registry, slot_dir, _RUN_PARENT)
    leader, descendant, identity = None, None, None
    try:
        leader = int(_wait_file(ready_marker, controller))
        descendant = int(_wait_file(descendant_marker, controller))
        record = read_receipt("gpu0", slot_dir)
        assert record["pid"] == leader
        identity = record["process_identity"]
        assert _alive(descendant)
        assert acquire_runtime_slot("gpu0", slot_dir) is None

        _kill_parent(controller)
        # The dead CVS parent's *lease* is automatically released; its
        # actual engine and descendant are still running.
        lease = acquire_runtime_slot("gpu0", slot_dir)
        assert lease is not None
        lease.release()
        assert process_identity(leader) == ("alive", identity)
        assert _alive(descendant)

        next_supervisor = RuntimeSupervisor(
            registry_path=_replacement_registry(tmp_path, registry),
            slot_dir=slot_dir, log_dir=tmp_path / "replacement-logs",
        )
        with pytest.raises(RuntimeConflictError, match="ownership unresolved"):
            next_supervisor.start("replacement")
        assert read_receipt("gpu0", slot_dir)["launch_id"] == record["launch_id"]
        assert "replacement" not in next_supervisor._owned
    finally:
        _kill_parent(controller)
        _verify_owner_dead_and_cleanup(leader, descendant, identity)


def test_actual_parent_kill_during_popen_preserves_unresolved_start_intent(tmp_path):
    fixture, _, slot_dir = _setup(tmp_path)
    controller, marker = _parent(
        tmp_path, fixture._registry_path, slot_dir, _BLOCK_BEFORE_POPEN,
    )
    try:
        assert _wait_file(marker, controller) == "intent-published"
        pending = read_receipt("gpu0", slot_dir)
        assert pending["state"] == "starting"

        _kill_parent(controller)
        recovered = acquire_runtime_slot("gpu0", slot_dir)
        assert recovered is not None
        recovered.release()

        second = RuntimeSupervisor(
            registry_path=fixture._registry_path,
            slot_dir=slot_dir, log_dir=tmp_path / "new-controller-logs",
        )
        with pytest.raises(RuntimeConflictError, match="interrupted startup receipt"):
            second.start("engine")
        assert read_receipt("gpu0", slot_dir)["launch_id"] == pending["launch_id"]
        # The fixture deliberately stops before Popen, so the test can
        # safely clear this *known-empty* intent under its original scope.
        lease = acquire_runtime_slot("gpu0", slot_dir)
        assert lease is not None
        try:
            clear_launch_receipt("gpu0", slot_dir, pending["launch_id"])
        finally:
            lease.release()
    finally:
        _kill_parent(controller)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs visibility contract")
def test_unreadable_procfs_entry_does_not_certify_gpu_group_as_empty(monkeypatch):
    original_iterdir = Path.iterdir
    original_read = Path.read_text

    def one_hidden_entry(path):
        if str(path) == "/proc":
            return iter([Path("/proc/99999999")])
        return original_iterdir(path)

    def unreadable_stat(path, *args, **kwargs):
        if str(path) == "/proc/99999999/stat":
            raise PermissionError("injected hidepid process")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", one_hidden_entry)
    monkeypatch.setattr(Path, "read_text", unreadable_stat)
    assert _live_posix_group_members(99999999) is None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs visibility contract")
def test_disappearing_procfs_entry_is_not_a_false_occupant(monkeypatch):
    original_iterdir = Path.iterdir
    original_read = Path.read_text

    def one_vanished_entry(path):
        if str(path) == "/proc":
            return iter([Path("/proc/99999999")])
        return original_iterdir(path)

    def missing_stat(path, *args, **kwargs):
        if str(path) == "/proc/99999999/stat":
            raise FileNotFoundError("process vanished between enumeration and inspection")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", one_vanished_entry)
    monkeypatch.setattr(Path, "read_text", missing_stat)
    assert _live_posix_group_members(99999999) is False
