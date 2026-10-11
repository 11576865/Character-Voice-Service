"""Fail-closed launch receipts for parent-held managed runtime GPU leases.

A stable OS slot lock serializes all cooperating CVS parents. Receipts
cover the gap after a parent dies but its engine process may survive.
All mutation APIs must be called while holding that group's slot lock.
"""
from __future__ import annotations

import ctypes
import json
import os
import time
import uuid
from pathlib import Path

from server.runtime_process_tree import _live_posix_group_members

class UncertainRuntimeOwner(RuntimeError):
    """A possible former child is still alive or cannot be disproved."""


def receipt_path(group: str, directory: Path) -> Path:
    # Reuse the authoritative group validator rather than trusting filenames.
    # acquire_runtime_slot already validates the group; here reject unsafe
    # names without acquiring a second, potentially re-entrant OS lock.
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", group):
        raise ValueError("invalid runtime exclusive group")
    return Path(directory).expanduser().resolve() / (group + ".owner.json")


def _atomic_json(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with stage.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
    finally:
        stage.unlink(missing_ok=True)


def read_receipt(group: str, directory: Path) -> dict | None:
    path = receipt_path(group, directory)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UncertainRuntimeOwner(
            f"{group}: cannot inspect runtime owner receipt: {path}"
        ) from exc
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise UncertainRuntimeOwner(f"{group}: corrupt runtime owner receipt: {path}") from exc
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise UncertainRuntimeOwner(f"{group}: unsupported runtime owner receipt: {path}")
    if record.get("group") != group or record.get("state") not in {"starting", "running"}:
        raise UncertainRuntimeOwner(f"{group}: invalid runtime owner receipt: {path}")
    if not isinstance(record.get("launch_id"), str) or not record["launch_id"]:
        raise UncertainRuntimeOwner(f"{group}: owner receipt lacks launch identity: {path}")
    return record


def _linux_start_ticks(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # Following the final ')' of comm, the first field is #3 (state).
        return raw.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return None


def _windows_process_identity(pid: int) -> tuple[str, str | None]:
    """Return (alive|dead|unknown, Windows process creation FILETIME)."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE; read-only access.
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return ("dead", None) if ctypes.get_last_error() == 87 else ("unknown", None)
    try:
        waiting = kernel.WaitForSingleObject(handle, 0)
        if waiting == 0:  # WAIT_OBJECT_0
            return "dead", None
        if waiting != 258:  # WAIT_TIMEOUT
            return "unknown", None
        created = ctypes.c_ulonglong()
        exited = ctypes.c_ulonglong()
        kernel_time = ctypes.c_ulonglong()
        user_time = ctypes.c_ulonglong()
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return "unknown", None
        return "alive", str(created.value)
    finally:
        kernel.CloseHandle(handle)


def process_identity(pid: int) -> tuple[str, str | None]:
    """Never use os.kill(pid, 0) on Windows: it is not a portable probe."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown", None
    if os.name == "nt":
        return _windows_process_identity(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", None
    except PermissionError:
        return "unknown", None
    except OSError:
        return "unknown", None
    if Path("/proc").is_dir():
        token = _linux_start_ticks(pid)
        return ("alive", token) if token is not None else ("unknown", None)
    # On unsupported POSIX platforms, a live PID is not sufficient to
    # disprove PID reuse. Fail closed instead of granting the GPU slot.
    return "unknown", None


def reconcile_prior_owner(group: str, directory: Path) -> None:
    """Call ONLY after acquiring the OS slot lease and before spawning.

    A pending receipt means the previous parent may have died between
    subprocess creation and publishing the child identity. Never assume
    that an expired age/timestamp proves the GPU workload is gone.
    """
    record = read_receipt(group, directory)
    if record is None:
        return
    path = receipt_path(group, directory)
    if record["state"] != "running":
        raise UncertainRuntimeOwner(
            f"{group}: interrupted startup receipt {path}; manually verify orphaned engine before clearing"
        )
    pid, expected = record.get("pid"), record.get("process_identity")
    state, actual = process_identity(pid)
    if state == "dead" or (state == "alive" and expected and actual and expected != actual):
        # Old receipts predate process-tree containment. A missing tree
        # identity cannot prove the engine's GPU-owning descendants exited.
        tree_kind = record.get("tree_kind")
        if tree_kind == "posix-session" and os.name != "nt":
            remaining = _live_posix_group_members(pid)
            if remaining is not False:
                raise UncertainRuntimeOwner(
                    f"{group}: prior engine PID exited but POSIX session "
                    f"{pid} still has or may have descendants"
                )
        elif tree_kind != "windows-job-wrapper" or os.name != "nt":
            raise UncertainRuntimeOwner(
                f"{group}: legacy or incompatible launch receipt has no "
                "verifiable process-tree containment; inspect before clearing"
            )
        path.unlink()
        return
    raise UncertainRuntimeOwner(
        f"{group}: previous engine ownership unresolved (pid={pid}, state={state}); "
        f"inspect the process and {path} before retrying"
    )


def publish_starting(group: str, directory: Path, engine_id: str) -> str:
    launch = uuid.uuid4().hex
    _atomic_json(receipt_path(group, directory), {
        "schema_version": 1,
        "group": group,
        "engine_id": engine_id,
        "launch_id": launch,
        "state": "starting",
        "created_unix": time.time(),
    })
    return launch


def publish_running(group: str, directory: Path, launch_id: str, pid: int) -> None:
    state, identity = process_identity(pid)
    if state != "alive" or not identity:
        raise UncertainRuntimeOwner(
            f"{group}: cannot confirm launched child process identity, pid={pid}"
        )
    current = read_receipt(group, directory)
    if not current or current["launch_id"] != launch_id or current["state"] != "starting":
        raise UncertainRuntimeOwner(f"{group}: launch intent changed during process creation")
    _atomic_json(receipt_path(group, directory), {
        **current,
        "state": "running",
        "pid": pid,
        "process_identity": identity,
        "tree_kind": "windows-job-wrapper" if os.name == "nt" else "posix-session",
        "confirmed_unix": time.time(),
    })


def clear_launch_receipt(group: str, directory: Path, launch_id: str) -> None:
    """Only after a confirmed terminated child (or before any spawn)."""
    current = read_receipt(group, directory)
    if current is None:
        return
    if current["launch_id"] != launch_id:
        raise UncertainRuntimeOwner(
            f"{group}: refusing to clear a different owner's receipt"
        )
    receipt_path(group, directory).unlink()
