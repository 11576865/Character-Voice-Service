"""Real subprocess regression tests for CVS Model Registry transactions."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from server import model_registry


def _child(code: str, *args: Path) -> subprocess.Popen:
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-c", code, *(str(arg) for arg in args)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env, cwd=root,
    )


def _wait_for_marker(marker: Path, process: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"child failed before marker: {stdout} {stderr}")
        time.sleep(0.025)
    raise AssertionError("child did not reach locked critical section")


def _finish(process: subprocess.Popen, timeout: float = 20.0) -> None:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=3)
        raise AssertionError(f"child did not finish: {stdout} {stderr}")
    assert process.returncode == 0, (stdout, stderr)


_HOLD_LOCK = """
import sys
import time
from pathlib import Path
from server.model_registry import _registry_write_lock
path, marker = map(Path, sys.argv[1:])
with _registry_write_lock(path):
    marker.write_text("owned", encoding="utf-8")
    time.sleep(1.0)
"""


def test_other_process_times_out_and_release_keeps_stable_lock_file(tmp_path):
    registry = tmp_path / "model-registry.json"
    marker = tmp_path / "locked.marker"
    holder = _child(_HOLD_LOCK, registry, marker)
    try:
        _wait_for_marker(marker, holder)
        with pytest.raises(TimeoutError, match="Model Registry lock"):
            with model_registry._registry_write_lock(registry, timeout=0.1):
                raise AssertionError("two owners acquired the same lock")
    finally:
        _finish(holder)
    with model_registry._registry_write_lock(registry, timeout=0.5):
        pass
    assert registry.with_name("model-registry.json.lock").is_file()


_SLOW_FIRST_WRITER = """
import sys
import time
from pathlib import Path
from server import model_registry as registry
path, marker = map(Path, sys.argv[1:])
original = registry._save_registry_unlocked

def delayed(data, path):
    marker.write_text("inside-transaction", encoding="utf-8")
    time.sleep(0.7)
    return original(data, path)

registry._save_registry_unlocked = delayed
registry.set_status("first", "validated", registry_path=path)
"""


def test_two_processes_preserve_both_model_status_changes(tmp_path):
    registry = tmp_path / "registry.json"
    model_registry.save_registry({
        "schema_version": 1,
        "models": {
            "first": {"status": "candidate"},
            "second": {"status": "candidate"},
        },
        "defaults": {},
    }, registry)
    marker = tmp_path / "first-before-commit.marker"
    first = _child(_SLOW_FIRST_WRITER, registry, marker)
    try:
        _wait_for_marker(marker, first)
        second = _child("""
import sys
from pathlib import Path
from server import model_registry
model_registry.set_status("second", "retired", registry_path=Path(sys.argv[1]))
""", registry)
        _finish(second)
    finally:
        _finish(first)

    stored = model_registry.load_registry(registry)["models"]
    assert stored["first"]["status"] == "validated"
    assert stored["second"]["status"] == "retired"


def test_lock_released_when_mutation_raises(tmp_path):
    registry = tmp_path / "registry.json"
    with pytest.raises(RuntimeError, match="crash"):
        with model_registry._registry_write_lock(registry, timeout=0.5):
            raise RuntimeError("crash in registry critical section")
    with model_registry._registry_write_lock(registry, timeout=0.5):
        model_registry._save_registry_unlocked(
            {"schema_version": 1, "models": {}, "defaults": {}}, registry,
        )
    assert model_registry.load_registry(registry)["defaults"] == {}


def test_killed_process_does_not_leave_stale_lock_ownership(tmp_path):
    registry = tmp_path / "registry.json"
    marker = tmp_path / "claimed.marker"
    holder = _child(_HOLD_LOCK, registry, marker)
    _wait_for_marker(marker, holder)
    holder.kill()
    holder.communicate(timeout=8)
    with model_registry._registry_write_lock(registry, timeout=1.0):
        pass


def test_failed_atomic_replacement_retains_old_snapshot_and_unlocks(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    old = {"schema_version": 1, "models": {"old": {}}, "defaults": {}}
    model_registry.save_registry(old, registry)
    previous_replace = model_registry.os.replace

    def injected_failure(source, target):
        raise OSError("injected failed publication")

    monkeypatch.setattr(model_registry.os, "replace", injected_failure)
    with pytest.raises(OSError, match="publication"):
        model_registry.save_registry(
            {"schema_version": 1, "models": {"new": {}}, "defaults": {}}, registry,
        )
    monkeypatch.setattr(model_registry.os, "replace", previous_replace)
    assert model_registry.load_registry(registry) == old
    with model_registry._registry_write_lock(registry, timeout=0.5):
        pass
    assert list(tmp_path.glob("registry.json.*.tmp")) == []


def test_registry_file_symlink_alias_shares_lock_and_preserves_alias(tmp_path):
    registry = tmp_path / "registry.json"
    model_registry.save_registry(
        {"schema_version": 1, "models": {}, "defaults": {}}, registry,
    )
    alias = tmp_path / "alias.json"
    try:
        alias.symlink_to(registry)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with model_registry._registry_write_lock(registry):
        with pytest.raises(TimeoutError):
            with model_registry._registry_write_lock(alias, timeout=0.08):
                pass
    model_registry.save_registry(
        {"schema_version": 1, "models": {"added": {}}, "defaults": {}}, alias,
    )
    assert alias.is_symlink()
    assert model_registry.load_registry(registry)["models"] == {"added": {}}


def test_unrelated_registries_are_not_globally_serialized(tmp_path):
    with model_registry._registry_write_lock(tmp_path / "one.json"):
        with model_registry._registry_write_lock(tmp_path / "two.json", timeout=0.1):
            pass


_SLOW_SCAN = """
import sys
import time
from pathlib import Path
from server import model_registry
root, registry, marker = map(Path, sys.argv[1:])
original = model_registry.validate_manifest

def slow(path):
    marker.write_text("scanning", encoding="utf-8")
    time.sleep(0.7)
    return original(path)

model_registry.validate_manifest = slow
model_registry.scan_model_root(model_root=root, registry_path=registry)
"""


def test_scan_owns_transaction_lock_before_asset_discovery(tmp_path):
    from test_model_registry import make_manifest

    model_root = tmp_path / "models"
    make_manifest(model_root)
    registry = tmp_path / "model-registry.json"
    marker = tmp_path / "scanning.marker"
    scan = _child(_SLOW_SCAN, model_root, registry, marker)
    try:
        _wait_for_marker(marker, scan)
        with pytest.raises(TimeoutError):
            with model_registry._registry_write_lock(registry, timeout=0.08):
                pass
    finally:
        _finish(scan)
    assert model_registry.load_registry(registry)["models"]["march7-gsv-v4-a"]["present"] is True
