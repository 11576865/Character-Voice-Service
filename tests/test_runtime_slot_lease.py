"""Cross-process GPU slot lease and supervisor child-lifetime integration.

Uses only disposable Python/HTTP engines. No GPU or user model data required.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from server.runtime_slot_lease import acquire_runtime_slot
from server.runtime_supervisor import RuntimeConflictError, RuntimeSupervisor, RuntimeSupervisorError
from test_runtime_supervisor import free_port, write_http_server, write_registry


def _lock_holder_script() -> str:
    return """
import sys, time
from pathlib import Path
from server.runtime_slot_lease import acquire_runtime_slot
root, group, marker, release = sys.argv[1:]
lease = acquire_runtime_slot(group, Path(root))
assert lease is not None
Path(marker).write_text("acquired", encoding="utf-8")
try:
    while not Path(release).exists():
        time.sleep(0.025)
finally:
    lease.release()
"""


def _spawn_holder(root: Path, group: str, marker: Path, release: Path):
    repo = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-c", _lock_holder_script(),
         str(root), group, str(marker), str(release)],
        cwd=repo, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _wait_marker(marker: Path, process: subprocess.Popen, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"slot owner failed: {stdout} {stderr}")
        time.sleep(0.03)
    raise AssertionError("slot owner did not publish acquisition marker")


def _finish(process: subprocess.Popen):
    try:
        stdout, stderr = process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=4)
        raise AssertionError(f"slot owner stuck: {stdout} {stderr}")
    assert process.returncode == 0, (stdout, stderr)


def test_two_independent_python_processes_cannot_acquire_same_slot(tmp_path):
    root = tmp_path / "runtime-slots"
    marker, release = tmp_path / "held", tmp_path / "release"
    owner = _spawn_holder(root, "gpu-0", marker, release)
    try:
        _wait_marker(marker, owner)
        assert acquire_runtime_slot("gpu-0", root) is None
        other = acquire_runtime_slot("gpu-1", root)
        assert other is not None
        other.release()
        assert (root / "gpu-0.lock").is_file()
    finally:
        release.touch()
        _finish(owner)
    recovered = acquire_runtime_slot("gpu-0", root)
    assert recovered is not None
    recovered.release()
    assert (root / "gpu-0.lock").is_file()  # Never unlink a lock inode.


def test_killing_owner_releases_os_slot_lock(tmp_path):
    root, marker, release = tmp_path / "locks", tmp_path / "held", tmp_path / "release"
    owner = _spawn_holder(root, "gpu-0", marker, release)
    try:
        _wait_marker(marker, owner)
        assert acquire_runtime_slot("gpu-0", root) is None
    finally:
        owner.kill()
        owner.communicate(timeout=6)
    recovered = acquire_runtime_slot("gpu-0", root)
    assert recovered is not None
    recovered.release()


def test_slot_identity_canonicalizes_configured_directory_alias(tmp_path):
    root = tmp_path / "locks"
    held = acquire_runtime_slot("gpu-0", root)
    assert held is not None
    try:
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(root, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("directory symlink unavailable")
        assert acquire_runtime_slot("gpu-0", alias) is None
    finally:
        held.release()


def test_managed_runtime_holds_lease_until_confirmed_stop(tmp_path):
    root = tmp_path / "runtime-slots"
    script = tmp_path / "serve.py"
    write_http_server(script)
    registry_a, registry_b = tmp_path / "a.json", tmp_path / "b.json"
    write_registry(registry_a, "alpha-engine", free_port(), script, group="gpu0")
    write_registry(registry_b, "beta-engine", free_port(), script, group="gpu0")
    first = RuntimeSupervisor(registry_path=registry_a, log_dir=tmp_path / "logs-a", slot_dir=root)
    second = RuntimeSupervisor(registry_path=registry_b, log_dir=tmp_path / "logs-b", slot_dir=root)
    try:
        started = first.start("alpha-engine")
        assert started["ready"] is True
        assert acquire_runtime_slot("gpu0", root) is None
        with pytest.raises(RuntimeConflictError, match="held by another CVS process"):
            second.start("beta-engine")
        assert second.status("beta-engine")["managed_by_supervisor"] is False

        first.stop("alpha-engine")
        beta = second.start("beta-engine")
        assert beta["ready"] is True
        assert acquire_runtime_slot("gpu0", root) is None
    finally:
        first.shutdown_all()
        second.shutdown_all()
    recovered = acquire_runtime_slot("gpu0", root)
    assert recovered is not None
    recovered.release()


def test_spawn_failure_does_not_leak_gpu_slot_lease(tmp_path, monkeypatch):
    root = tmp_path / "slots"
    script = tmp_path / "serve.py"
    write_http_server(script)
    registry = tmp_path / "registry.json"
    write_registry(registry, "test-engine", free_port(), script)
    supervisor = RuntimeSupervisor(registry_path=registry, log_dir=tmp_path / "logs", slot_dir=root)

    def reject_spawn(*args, **kwargs):
        raise OSError("injected spawn failure")

    monkeypatch.setattr("server.runtime_supervisor.subprocess.Popen", reject_spawn)
    with pytest.raises((OSError, RuntimeSupervisorError), match="spawn failure"):
        supervisor.start("test-engine")
    lease = acquire_runtime_slot("gpu0", root)
    assert lease is not None
    lease.release()


@pytest.mark.parametrize("group", ["../gpu", "gpu/a", "", "group with spaces", "GPU-UPPER"])
def test_unsafe_group_names_cannot_escape_lock_directory(tmp_path, group):
    with pytest.raises(ValueError, match="invalid runtime exclusive group"):
        acquire_runtime_slot(group, tmp_path / "slots")
