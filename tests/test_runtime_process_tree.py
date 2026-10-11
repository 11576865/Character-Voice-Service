"""Managed GPU process-tree lifecycle regressions (real disposable children).

Windows tests exercise a Job Object wrapper and POSIX tests exercise
an isolated process session. No TTS model or GPU is required.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from server.runtime_process_tree import _live_posix_group_members
from server.runtime_orphan_receipt import read_receipt, reconcile_prior_owner, UncertainRuntimeOwner
from server.runtime_slot_lease import acquire_runtime_slot
from server.runtime_supervisor import RuntimeSupervisor, RuntimeConflictError
from test_runtime_supervisor import free_port, write_registry


def _alive(pid):
    if os.name != "nt":
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            try:
                return stat.read_text().rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
            except OSError:
                return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    from server.runtime_orphan_receipt import process_identity
    return process_identity(pid)[0] == "alive"


def _wait_dead(pid, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"managed descendant still alive: pid={pid}")


def _setup(tmp_path, *, root_exits=False):
    script = tmp_path / "managed_engine.py"
    marker = tmp_path / "child-pid.txt"
    script.write_text("""
import os, subprocess, sys, threading, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(70)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path(os.environ["TEST_CHILD_MARKER"]).write_text(str(child.pid), encoding="utf-8")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ready"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

server = HTTPServer(("127.0.0.1", int(os.environ["TEST_PORT"])), Handler)
if os.environ["TEST_ROOT_EXITS"] == "1":
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(2.0)
    os._exit(23)
else:
    server.serve_forever()
""", encoding="utf-8")
    registry = tmp_path / "registry.json"
    port = free_port()
    write_registry(registry, "engine", port, script, group="gpu0")
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["engines"]["engine"]["env"].update({
        "TEST_CHILD_MARKER": str(marker),
        "TEST_ROOT_EXITS": "1" if root_exits else "0",
    })
    registry.write_text(json.dumps(data), encoding="utf-8")
    root = tmp_path / "slots"
    sup = RuntimeSupervisor(registry_path=registry, log_dir=tmp_path / "logs", slot_dir=root)
    return sup, marker, root


def _child_pid(marker):
    end = time.monotonic() + 6
    while time.monotonic() < end:
        if marker.exists():
            return int(marker.read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError("engine never wrote descendant PID")


def test_stop_terminates_live_descendants_not_only_direct_engine_pid(tmp_path):
    sup, marker, root = _setup(tmp_path)
    pid = None
    try:
        started = sup.start("engine")
        assert started["ready"] is True
        pid = _child_pid(marker)
        assert _alive(pid)
        stopped = sup.stop("engine")
        assert stopped["status"] == "stopped"
        _wait_dead(pid)
        assert read_receipt("gpu0", root) is None
        lease = acquire_runtime_slot("gpu0", root)
        assert lease is not None
        lease.release()
    finally:
        try:
            sup.shutdown_all()
        finally:
            if pid and _alive(pid) and os.name != "nt":
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX session-only scenario")
def test_exited_leader_with_surviving_descendant_preserves_gpu_slot_and_receipt(tmp_path):
    sup, marker, root = _setup(tmp_path, root_exits=True)
    pid = None
    try:
        started = sup.start("engine")
        assert started["ready"] is True
        pid = _child_pid(marker)
        leader_pid = started["pid"]
        end = time.monotonic() + 6
        while sup._owned["engine"].process.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        assert sup._owned["engine"].process.poll() == 23
        assert _alive(pid)
        state = sup.status("engine")
        assert state["status"] == "process-tree-unresolved"
        assert state["process_tree_unresolved"]
        assert read_receipt("gpu0", root)["pid"] == leader_pid
        assert acquire_runtime_slot("gpu0", root) is None
        with pytest.raises(RuntimeConflictError, match="previous managed process tree unresolved"):
            sup.start("engine")
        sup.stop("engine")
        _wait_dead(pid)
        assert read_receipt("gpu0", root) is None
    finally:
        try:
            sup.shutdown_all()
        finally:
            if pid and _alive(pid):
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX session-only scenario")
def test_new_cvs_cannot_reconcile_dead_leader_receipt_with_live_group_member(tmp_path):
    sup, marker, root = _setup(tmp_path, root_exits=True)
    pid = None
    try:
        started = sup.start("engine")
        pid = _child_pid(marker)
        leader = started["pid"]
        end = time.monotonic() + 5
        while sup._owned["engine"].process.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        assert sup._owned["engine"].process.poll() == 23
        assert _alive(pid)
        # Simulate the loss of the CVS parent's *lock* while keeping the
        # durable receipt, as occurs on parent death. The old supervisor
        # object is never used for another start in this test.
        owned = sup._owned.pop("engine")
        owned.slot_lease.release()
        owned.log_handle.close()
        lease = acquire_runtime_slot("gpu0", root)
        assert lease is not None
        try:
            with pytest.raises(UncertainRuntimeOwner, match="still has or may have descendants"):
                reconcile_prior_owner("gpu0", root)
        finally:
            lease.release()
        # Manually stop ONLY the owned session for cleanup.
        os.killpg(leader, signal.SIGKILL)
        _wait_dead(pid)
        lease = acquire_runtime_slot("gpu0", root)
        try:
            reconcile_prior_owner("gpu0", root)
            assert read_receipt("gpu0", root) is None
        finally:
            lease.release()
    finally:
        if pid and _alive(pid):
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass


def test_managed_engine_launch_receipt_records_tree_containment(tmp_path):
    sup, marker, root = _setup(tmp_path)
    try:
        started = sup.start("engine")
        old = read_receipt("gpu0", root)
        assert old is not None
        assert old["pid"] == started["pid"]
        assert old["tree_kind"] == (
            "windows-job-wrapper" if os.name == "nt" else "posix-session"
        )
        if os.name != "nt":
            assert os.getsid(started["pid"]) == started["pid"]
    finally:
        sup.stop("engine")


@pytest.mark.skipif(os.name == "nt", reason="legacy receipt policy also tested on Windows elsewhere")
def test_legacy_receipt_cannot_be_auto_cleared_after_direct_pid_exit(tmp_path):
    root = tmp_path / "slots"
    lease = acquire_runtime_slot("gpu0", root)
    assert lease is not None
    try:
        from server.runtime_orphan_receipt import publish_starting, publish_running, receipt_path
        sleeper = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            launch_id = publish_starting("gpu0", root, "engine")
            # Test a pre-upgrade receipt with no verified tree containment.
            sleeper.wait(timeout=4)
            path = receipt_path("gpu0", root)
            data = json.loads(path.read_text(encoding="utf-8"))
            data.update({"state":"running","pid":sleeper.pid,"process_identity":"legacy-token"})
            path.write_text(json.dumps(data), encoding="utf-8")
            with pytest.raises(UncertainRuntimeOwner, match="legacy or incompatible"):
                reconcile_prior_owner("gpu0", root)
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=4)
    finally:
        lease.release()
