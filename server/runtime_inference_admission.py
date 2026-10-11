"""Cooperative in-flight GPU group guard for CVS inference and lifecycle.

A managed runtime's lifetime lock is held for minutes/hours and is NOT
an inference activity lease. This independent, nonblocking OS lock
serializes model-serving admission with same-group start/stop/restart.

The durable pending intent covers the case where the CVS controller dies
and its OS lock releases while the HTTP backend keeps synthesizing.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from server.runtime_slot_lease import RuntimeSlotLease, acquire_runtime_slot


class RuntimeGroupAdmissionError(RuntimeError):
    pass


class RuntimeGroupBusy(RuntimeGroupAdmissionError):
    pass


class RuntimeGroupUnresolved(RuntimeGroupAdmissionError):
    pass


_local = threading.local()


def _scope(group: str, directory: Path) -> tuple[tuple[str, str], str, Path]:
    root = Path(directory).expanduser().resolve()
    # acquire_runtime_slot validates the actual group before this method
    # derives the auxiliary lease identifier.
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", group):
        raise ValueError("invalid runtime exclusive group")
    digest = hashlib.sha256(group.encode("utf-8")).hexdigest()[:32]
    return (str(root), group), "activity-" + digest, root / ("activity-" + digest + ".pending.json")


def _read_pending(path: Path, group: str) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeGroupUnresolved(f"{group}: cannot read prior inference receipt") from exc
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeGroupUnresolved(f"{group}: corrupt prior inference receipt") from exc
    if (not isinstance(result, dict) or result.get("schema_version") != 1
        or result.get("phase") != "inference-pending"
        or result.get("group") != group
        or not isinstance(result.get("request_id"), str)
        or not result["request_id"]):
        raise RuntimeGroupUnresolved(f"{group}: incompatible prior inference receipt")
    return result


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with stage.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
    finally:
        stage.unlink(missing_ok=True)


class RuntimeGroupGuard:
    def __init__(self, group: str, receipt: Path, lease: RuntimeSlotLease):
        self.group = group
        self.receipt = receipt
        self.lease = lease
        self._active_request: str | None = None

    def begin_inference(self, engine_id: str) -> str:
        if self._active_request:
            raise RuntimeGroupUnresolved(f"{self.group}: nested inference is not allowed")
        if _read_pending(self.receipt, self.group) is not None:
            raise RuntimeGroupUnresolved(
                f"{self.group}: previous inference outcome unresolved; inspect backend before retry"
            )
        request_id = uuid.uuid4().hex
        _atomic_write(self.receipt, {
            "schema_version": 1, "phase": "inference-pending",
            "group": self.group, "engine_id": engine_id,
            "request_id": request_id, "started_unix": time.time(),
        })
        self._active_request = request_id
        return request_id

    def finish_inference(self, request_id: str) -> None:
        if request_id != self._active_request:
            raise RuntimeGroupUnresolved(f"{self.group}: inference identity changed")
        receipt = _read_pending(self.receipt, self.group)
        if receipt is None or receipt["request_id"] != request_id:
            raise RuntimeGroupUnresolved(f"{self.group}: inference receipt changed unexpectedly")
        self.receipt.unlink()
        self._active_request = None


@contextmanager
def runtime_group_guard(group: str | None, directory: Path) -> Iterator[RuntimeGroupGuard | None]:
    """Admission guard shared by **all cooperating CVS instances**.

    Nested calls on the *same thread* are allowed, for inference ->
    ensure_ready -> start and restart -> start. Different threads and OS
    processes never get reentrant rights. Lock file must never be deleted.
    """
    if not group:
        yield None
        return

    key, lock_id, receipt = _scope(group, directory)
    held = getattr(_local, "held", None)
    if held is None:
        held = {}
        _local.held = held
    current = held.get(key)
    if current is not None:
        # Nested managed start during ensure_ready is legal BEFORE
        # begin_inference. Once backend work has been admitted, not even
        # a same-thread callback may stop/switch that engine.
        if current._active_request is not None:
            raise RuntimeGroupBusy(
                f"{group}: active inference cannot be interrupted by a nested lifecycle operation"
            )
        yield current
        return

    try:
        lease = acquire_runtime_slot(lock_id, directory)
    except OSError as exc:
        raise RuntimeGroupAdmissionError(
            f"{group}: cannot acquire inference/lifecycle coordination lock"
        ) from exc
    if lease is None:
        raise RuntimeGroupBusy(
            f"{group}: active inference or runtime lifecycle operation; try later"
        )
    try:
        # Even when the OS lock is available after a controller crash,
        # the old HTTP inference may still be running.
        if _read_pending(receipt, group) is not None:
            raise RuntimeGroupUnresolved(
                f"{group}: previous inference outcome unresolved; "
                "verify the backend idle before manually reconciling"
            )
        guard = RuntimeGroupGuard(group, receipt, lease)
        held[key] = guard
        try:
            yield guard
        finally:
            held.pop(key, None)
    finally:
        lease.release()
