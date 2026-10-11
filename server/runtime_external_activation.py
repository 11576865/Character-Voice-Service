"""Durable single-flight activation requests for external Runtime Supervisors.

The external System Supervisor owns the process; CVS only publishes an
activation request. A pending request is not an acknowledgement of startup.
Both files and OS locks live in the configured shared request directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from server.runtime_slot_lease import acquire_runtime_slot, RuntimeSlotLease


class ExternalActivationUncertain(RuntimeError):
    pass


def activation_scope(request_dir: str | Path, service_key: str) -> tuple[Path, str, Path]:
    if not service_key or not service_key.strip():
        raise ValueError("external activation requires nonempty service_key")
    root = Path(request_dir).expanduser().resolve()
    # Stable per-service OS lock and pending receipt. Do not interpret
    # service_key as a path or permit special characters in file names.
    digest = hashlib.sha256(service_key.encode("utf-8")).hexdigest()[:32]
    return root, "external-" + digest, root / ".cvs-activation-state" / (digest + ".pending.json")


def acquire_activation_lease(request_dir: str | Path, service_key: str) -> RuntimeSlotLease | None:
    root, group, _ = activation_scope(request_dir, service_key)
    return acquire_runtime_slot(group, root)


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with stage.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
    finally:
        stage.unlink(missing_ok=True)


def pending_activation(request_dir: str | Path, service_key: str) -> dict | None:
    _, _, path = activation_scope(request_dir, service_key)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExternalActivationUncertain(f"unable to read activation receipt: {path}") from exc
    try:
        record = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ExternalActivationUncertain(f"corrupt activation receipt: {path}") from exc
    if (
        not isinstance(record, dict) or record.get("schema_version") != 1
        or record.get("service_key") != service_key
        or record.get("phase") != "pending"
        or not isinstance(record.get("request_id"), str)
        or not record["request_id"]
        or not isinstance(record.get("engine_id"), str)
        or not record["engine_id"]
    ):
        raise ExternalActivationUncertain(f"invalid activation receipt: {path}")
    return record


def publish_activation(request_dir: str | Path, service_key: str, engine_id: str) -> dict:
    """Call with the per-service lease held; persist intent BEFORE command.

    If interrupted anywhere after recording intent, future activation
    attempts refuse to issue a duplicate until health succeeds or an
    operator verifies the external supervisor's true state.
    """
    root, _, receipt = activation_scope(request_dir, service_key)
    if pending_activation(root, service_key) is not None:
        raise ExternalActivationUncertain(
            f"{service_key}: an earlier activation is unresolved: {receipt}"
        )
    request_id = uuid.uuid4().hex
    created_unix = time.time()
    record = {
        "schema_version": 1, "phase": "pending",
        "service_key": service_key, "engine_id": engine_id,
        "request_id": request_id, "created_unix": created_unix,
    }
    _atomic_write(receipt, record)
    # System Supervisor's existing file bridge accepts this payload.
    # Command becomes visible only after the full staged file is fsynced.
    command = {
        "schema_version": 1, "action": "activate-engine",
        "engine_id": engine_id, "service_key": service_key,
        "request_id": request_id, "created_unix": created_unix,
    }
    _atomic_write(root / (request_id + ".json"), command)
    return record


def acknowledge_activation(request_dir: str | Path, service_key: str, request_id: str) -> None:
    """Only after a live health check, and only under that service's lease."""
    old = pending_activation(request_dir, service_key)
    if old is None:
        return
    if old["request_id"] != request_id:
        raise ExternalActivationUncertain(
            f"{service_key}: pending activation identity changed; refusing receipt deletion"
        )
    _, _, path = activation_scope(request_dir, service_key)
    path.unlink()
