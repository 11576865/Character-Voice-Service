"""Cross-process Model Registry transaction regressions (Linux and Windows).

Every child uses a fresh Python interpreter: thread-only locks cannot pass.
"""
import json
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
        [sys.executable, "-c", code, *(str(x) for x in args)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env, cwd=root,
    )


def _wait_for_marker(marker: Path, process: subprocess.Popen, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"child exited before marker: {stdout} {stderr}")
        time.sleep(0.025)
    raise AssertionError("child failed to claim registry lock before timeout")


def _finish(process: subprocess.Popen, *, timeout: float = 20.0) -> None:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=3)
        raise AssertionError(f"registry child stuck: {stdout} {stderr}")
    assert process.returncode == 0, (stdout, stderr)


def test_second_process_times_out_while_first_owns_registry_lock(tmp_path):
    registry = tmp_path / "model-registry.json"
    marker = tmp_path / "locked.marker"
    holder = _child(
        "import sys,time; from pathlib import Path; "
        "from server.model_registry import _registry_write_lock; "
        "p,m=map(Path,sys.argv[1:]); "
        "withcode=''; "
        "exec('with _registry_write_lock(p):\\n m.write_text(\\\"ready\\\")\\n time.sleep(1.0)')",
        registry, marker,
    )
    try:
        _wait_for_marker(marker, holder)
        with pytest.raises(TimeoutError, match="Model Registry lock"):
            with model_registry._registry_write_lock(registry, timeout=0.10):
                raise AssertionError("second process acquired an owned lock")
    finally:
        _finish(holder)
    with model_registry._registry_write_lock(registry, timeout=1.0):
        pass
    assert registry.with_name("model-registry.json.lock").is_file()


def test_two_processes_cannot_lose_independent_model_updates(tmp_path):
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
    # First process deliberately pauses AFTER loading the registry under
    # the lock, just before its durable save. The second process attempts
    # a separate lifecycle update while this transaction is incomplete.
    first = _child(
        "import sys,time; from pathlib import Path; "
        "from server import model_registry as m; "
        "p,marker=map(Path,sys.argv[1:]); "
        "original=m._save_registry_unlocked; "
        "defcode=''; "
        "exec('def delayed(data,path):\\n marker.write_text(\\\"held\\\")\\n time.sleep(0.7)\\n return original(data,path)'); "
        "m._save_registry_unlocked=delayed; "
        "m.set_status('first','validated',registry_path=p)",
        registry, marker,
    )
    try:
        _wait_for_marker(marker, first)
        second = _child(
            "import sys; from pathlib import Path; "
            "from server import model_registry as m; "
            "m.set_status('second','retired',registry_path=Path(sys.argv[1]))",
            registry,
        )
        _finish(second)
    finally:
        _finish(first)
    values = model_registry.load_registry(registry)["models"]
    assert values["first"]["status"] == "validated"
    assert values["second"]["status"] == "retired"


def test_lock_is_released_when_mutation_raises(tmp_path):
    registry = tmp_path / "registry.json"
    with pytest.raises(RuntimeError, match="crash"):
        with model_registry._registry_write_lock(registry, timeout=0.5):
            raise RuntimeError("crash in registry critical section")
    with model_registry._registry_write_lock(registry, timeout=0.5):
        model_registry._save_registry_unlocked(
            {"schema_version": 1, "models": {}, "defaults": {}}, registry,
        )
    assert model_registry.load_registry(registry)["defaults"] == {}


def test_failed_staging_replace_leaves_old_snapshot_and_unlocks(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    old = {"schema_version": 1, "models": {"original": {}}, "defaults": {}}
    model_registry.save_registry(old, registry)
    replace = model_registry.os.replace

    def fail_once(source, target):
        raise OSError("injected failed registry publication")

    monkeypatch.setattr(model_registry.os, "replace", fail_once)
    with pytest.raises(OSError, match="publication"):
        model_registry.save_registry(
            {"schema_version": 1, "models": {"new": {}}, "defaults": {}}, registry,
        )
    monkeypatch.setattr(model_registry.os, "replace", replace)
    assert model_registry.load_registry(registry) == old
    with model_registry._registry_write_lock(registry, timeout=0.5):
        pass
    assert list(tmp_path.glob("registry.json.*.tmp")) == []


def test_same_physical_registry_symlink_alias_shares_the_lock(tmp_path):
    registry = tmp_path / "registry.json"
    model_registry.save_registry(
        {"schema_version": 1, "models": {}, "defaults": {}}, registry,
    )
    alias = tmp_path / "alias.json"
    try:
        alias.symlink_to(registry)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires elevated permissions on this platform")
    with model_registry._registry_write_lock(registry):
        with pytest.raises(TimeoutError):
            with model_registry._registry_write_lock(alias, timeout=0.08):
                pass


def test_independent_registries_do_not_share_one_global_lock(tmp_path):
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    with model_registry._registry_write_lock(first):
        with model_registry._registry_write_lock(second, timeout=0.1):
            pass
