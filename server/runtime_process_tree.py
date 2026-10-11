"""Managed TTS process-tree containment for exclusive GPU groups.

On POSIX a child starts in a new session before exec, allowing its
process group to be stopped as a unit even after the leader exits.
Windows uses a dedicated process wrapper with a kill-on-close Job Object.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class ProcessTreeUncertain(RuntimeError):
    """A managed engine's descendants may still own GPU resources."""


WINDOWS_JOB_WRAPPER = Path(__file__).with_name("runtime_windows_job.py")


def managed_command(command: list[str], group: str | None) -> list[str]:
    if group and os.name == "nt":
        # This helper must install the job BEFORE spawning the actual engine.
        return [sys.executable, str(WINDOWS_JOB_WRAPPER), *command]
    return command


def spawn_options(group: str | None) -> dict:
    return {"start_new_session": True} if group and os.name != "nt" else {}


def _live_posix_group_members(pgid: int) -> bool | None:
    """Return True/False, or None when the group cannot be inspected.

    Linux procfs lists all nonzombie members even after group leader exit.
    We explicitly validate both pgrp and session ID to avoid confusing
    an unrelated process. On other POSIX kernels the group presence
    probe is conservative, including possible zombie-only groups.
    """
    if os.name == "nt" or pgid <= 0:
        return None
    proc = Path("/proc")
    if proc.is_dir() and sys.platform.startswith("linux"):
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                stats = (entry / "stat").read_text(encoding="ascii")
                fields = stats.rsplit(")", 1)[1].split()
                state, pgrp, session = fields[0], int(fields[2]), int(fields[3])
            except (OSError, IndexError, ValueError):
                continue  # Transient /proc entries can disappear mid-scan.
            if pgrp == pgid and session == pgid and state not in {"Z", "X", "x"}:
                return True
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None
    return True


def stop_managed_tree(process: subprocess.Popen, group: str | None, timeout: float) -> None:
    """Stop the verified scope and refuse release if descendants remain.

    Only managed, exclusive-group children are group-contained. External
    runtime children and unmanaged processes are never signalled here.
    """
    if not group or os.name == "nt":
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return

    pgid = process.pid  # Popen(start_new_session=True) creates this session.
    living = _live_posix_group_members(pgid)
    if living is None:
        raise ProcessTreeUncertain(
            f"managed process group {pgid} cannot be verified before stop"
        )
    if living:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise ProcessTreeUncertain(
                f"unable to terminate managed process group {pgid}: {exc}"
            ) from exc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        living = _live_posix_group_members(pgid)
        if living is False:
            break
        if living is None:
            raise ProcessTreeUncertain(f"cannot verify group {pgid} termination")
        time.sleep(0.05)
    if _live_posix_group_members(pgid) is not False:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise ProcessTreeUncertain(
                f"unable to kill managed process group {pgid}: {exc}"
            ) from exc
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if _live_posix_group_members(pgid) is False:
                break
            time.sleep(0.05)
    if process.poll() is None:
        process.wait(timeout=5)
    if _live_posix_group_members(pgid) is not False:
        raise ProcessTreeUncertain(
            f"managed process group {pgid} still has unverified descendants"
        )


def reconcile_exited_tree(process: subprocess.Popen, group: str | None) -> None:
    """After direct leader exits, retire remaining group members if present.

    Never release the group lease/receipt solely because poll() on the
    direct child returned a code.
    """
    if group and os.name != "nt":
        stop_managed_tree(process, group, 0.5)
    # On Windows the wrapper's own OS Job Object closes on wrapper exit,
    # initiating kill-on-job-close for its child and descendants. No
    # assertion is made here about live GPU driver state.
