from __future__ import annotations

import atexit
from contextlib import contextmanager
import os
import subprocess
import json
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from server.config import DATA_DIR
from server.runtime_registry import RuntimeRegistry, RuntimeSpec, load_runtime_registry
from server.runtime_slot_lease import RuntimeSlotLease, acquire_runtime_slot
from server.runtime_inference_admission import RuntimeGroupAdmissionError, runtime_group_guard
from server.runtime_process_tree import (
    ProcessTreeUncertain, managed_command, spawn_options,
    reconcile_exited_tree, stop_managed_tree,
)
from server.runtime_external_activation import (
    ExternalActivationUncertain,
    acquire_activation_lease, pending_activation, publish_activation, acknowledge_activation,
)
from server.runtime_orphan_receipt import (
    UncertainRuntimeOwner, read_receipt, reconcile_prior_owner,
    publish_starting, publish_running, clear_launch_receipt,
)


class RuntimeSupervisorError(RuntimeError):
    pass


class RuntimeConflictError(RuntimeSupervisorError):
    pass


# Backoff is in-memory and scoped to this supervisor process. A process
# restart intentionally clears it; crash history is exported in diagnostics.
CRASH_BACKOFF_BASE_SECONDS = 5.0
CRASH_BACKOFF_MAX_SECONDS = 60.0
CRASH_HISTORY_WINDOW_SECONDS = 300.0


@dataclass
class _OwnedProcess:
    process: subprocess.Popen
    log_path: Path
    log_handle: BinaryIO
    started_at: float
    slot_lease: RuntimeSlotLease | None = None
    launch_id: str | None = None
    exit_observed: bool = False
    tree_uncertain: str | None = None


class RuntimeSupervisor:
    def __init__(
        self,
        *,
        registry_path: str | Path | None = None,
        log_dir: str | Path | None = None,
        slot_dir: str | Path | None = None,
    ):
        self._registry_path = registry_path
        self._log_dir = Path(log_dir) if log_dir else DATA_DIR / "runtime-logs"
        self._slot_dir = Path(slot_dir) if slot_dir else DATA_DIR / "runtime-slots"
        self._owned: dict[str, _OwnedProcess] = {}
        self._failures: dict[str, dict] = {}
        self._last_exit: dict[str, dict] = {}
        self._lock = threading.RLock()

    def registry(self) -> RuntimeRegistry:
        return load_runtime_registry(self._registry_path)

    @contextmanager
    def _guard_group(self, spec: RuntimeSpec):
        # A live managed process can outlast registry edits/removal.
        # Its ORIGINAL ownership group, not a newly edited config value,
        # is the resource that must be protected before stop/restart.
        owned = self._owned.get(spec.engine_id)
        group = (
            owned.slot_lease.group
            if owned is not None and owned.slot_lease is not None
            else spec.exclusive_group
        )
        if owned and owned.slot_lease and spec.exclusive_group not in {None, group}:
            raise RuntimeConflictError(
                f"{spec.engine_id}: configured GPU group changed while managed "
                "process is alive; refuse uncoordinated lifecycle handoff"
            )
        try:
            with runtime_group_guard(group, self._slot_dir) as guard:
                yield guard
        except RuntimeGroupAdmissionError as exc:
            raise RuntimeConflictError(
                f"{spec.engine_id}: GPU group lifecycle admission denied: {exc}"
            ) from exc

    @staticmethod
    def _health(spec: RuntimeSpec, timeout: float = 1.5) -> tuple[bool, str | None]:
        if not spec.health_url:
            return False, "health_url is not configured"
        try:
            request = urllib.request.Request(
                spec.health_url,
                headers={"User-Agent": "Character-Voice-Service-Runtime-Supervisor/1"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read(4096)
            # Health endpoints can be responsive while the single inference
            # slot is occupied. That is healthy but not idle/ready to admit
            # another synthesis request.
            if "application/json" in content_type.lower():
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    state = str(payload.get("status") or "").lower()
                    if state == "busy":
                        return True, "busy"
                    if state in {"stopping", "failed", "unhealthy"}:
                        return False, f"runtime health status: {state}"
            return True, None
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def _runtime_prefix(spec: RuntimeSpec) -> list[str]:
        paths: list[str] = []

        for item in spec.path_prepend:
            paths.append(str(item))

        if spec.executable:
            executable_dir = spec.executable.parent
            env_root = (
                executable_dir.parent
                if executable_dir.name.casefold() in {"scripts", "bin"}
                else executable_dir
            )
            paths.extend([
                str(executable_dir),
                str(env_root),
                str(env_root / "Scripts"),
                str(env_root / "Library" / "bin"),
                str(env_root / "Library" / "usr" / "bin"),
                str(env_root / "Library" / "mingw-w64" / "bin"),
            ])

        seen: set[str] = set()
        result: list[str] = []
        for item in paths:
            if not item:
                continue
            key = os.path.normcase(os.path.normpath(item))
            if key in seen:
                continue
            seen.add(key)
            if Path(item).exists():
                result.append(item)
        return result

    @classmethod
    def _environment(cls, spec: RuntimeSpec) -> dict[str, str]:
        env = dict(os.environ)

        # Do not let the shell's active Conda/base or arbitrary PYTHONPATH decide
        # which Python packages/DLLs an engine runtime receives.
        for key in list(env):
            upper = key.upper()
            if (
                upper.startswith("CONDA_")
                or upper.startswith("_CE_CONDA")
                or upper in {"PYTHONHOME", "PYTHONPATH", "MAMBA_EXE", "MAMBA_ROOT_PREFIX"}
            ):
                env.pop(key, None)

        existing_path = env.get("PATH", "")
        filtered_path = []
        for item in existing_path.split(os.pathsep):
            clean = item.strip().strip('"')
            if not clean:
                continue
            lowered = clean.casefold()
            if "miniconda" in lowered or "anaconda" in lowered:
                continue
            filtered_path.append(clean)

        prefix = cls._runtime_prefix(spec)
        env["PATH"] = os.pathsep.join([*prefix, *filtered_path])
        env["PYTHONNOUSERSITE"] = "1"
        env.update(spec.env)
        return env

    @staticmethod
    def _external_supervisor_status(spec: RuntimeSpec) -> dict | None:
        control = spec.external_control or {}
        status_file = str(control.get("status_file") or "").strip()
        service_key = str(control.get("service_key") or "").strip()
        if not status_file or not service_key:
            return None
        path = Path(status_file).expanduser()
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        services = payload.get("services") if isinstance(payload, dict) else None
        if not isinstance(services, list):
            return None
        for item in services:
            if not isinstance(item, dict):
                continue
            if str(item.get("key") or "") != service_key:
                continue
            return {
                "supervisor_session": payload.get("session_id"),
                "supervisor_version": payload.get("supervisor_version"),
                "supervisor_state": item.get("state"),
                "supervisor_detail": item.get("detail"),
                "pid": item.get("root_pid") or item.get("worker_pid"),
                "listener_pid": item.get("listener_pid"),
                "service_key": service_key,
            }
        return None

    def _record_failure(self, engine_id: str, reason: str) -> dict:
        """Record one failed lifecycle attempt and bound the next auto-start."""
        now = time.monotonic()
        previous = self._failures.get(engine_id)
        count = (
            previous["count"] + 1
            if previous and now - previous["last_failure_monotonic"] < CRASH_HISTORY_WINDOW_SECONDS
            else 1
        )
        delay = min(CRASH_BACKOFF_MAX_SECONDS, CRASH_BACKOFF_BASE_SECONDS * (2 ** min(count - 1, 12)))
        record = {
            "count": count,
            "reason": reason,
            "last_failure_monotonic": now,
            "next_retry_monotonic": now + delay,
        }
        self._failures[engine_id] = record
        return record

    def _restart_cooldown(self, engine_id: str) -> float:
        record = self._failures.get(engine_id)
        if not record:
            return 0.0
        return max(0.0, record["next_retry_monotonic"] - time.monotonic())

    def _clean_exited(self, engine_id: str) -> None:
        owned = self._owned.get(engine_id)
        if not owned:
            return
        code = owned.process.poll()
        if code is None:
            return
        if not owned.exit_observed:
            uptime = max(0.0, time.time() - owned.started_at)
            if uptime >= CRASH_HISTORY_WINDOW_SECONDS:
                self._failures.pop(engine_id, None)
            self._last_exit[engine_id] = {
                "pid": owned.process.pid,
                "returncode": code,
                "uptime_seconds": round(uptime, 2),
                "observed_unix": time.time(),
                "log_path": str(owned.log_path),
                "reason": "unexpected-exit",
            }
            self._record_failure(engine_id, "unexpected-exit")
            owned.exit_observed = True

        # A dead direct PID is not evidence that its descendants stopped.
        # Keep the group lease AND durable receipt when descendants exist.
        try:
            reconcile_exited_tree(
                owned.process, owned.slot_lease.group if owned.slot_lease else None,
            )
        except ProcessTreeUncertain as exc:
            owned.tree_uncertain = str(exc)
            return
        owned.tree_uncertain = None
        if owned.slot_lease and owned.launch_id:
            clear_launch_receipt(
                owned.slot_lease.group, self._slot_dir, owned.launch_id,
            )
        try:
            owned.log_handle.close()
        finally:
            self._owned.pop(engine_id, None)
            if owned.slot_lease:
                owned.slot_lease.release()

    def _log_tail(self, path: Path, limit: int = 4000) -> str:
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        return data[-limit:].decode("utf-8", errors="replace").strip()

    def status(self, engine_id: str) -> dict:
        # Startup can own the supervisor lock for minutes. A status/health
        # request must never wait behind the entire startup deadline.
        if not self._lock.acquire(blocking=False):
            spec = self.registry().get(engine_id)
            if spec is None:
                return {
                    "engine": engine_id, "configured": False,
                    "status": "unconfigured", "managed_by_supervisor": False,
                }
            return {
                "engine": engine_id,
                "runtime_id": spec.runtime_id,
                "runtime_revision": spec.configuration_revision,
                "configured": True,
                "enabled": spec.enabled,
                "status": "supervisor-transition",
                "ready": False,
                "busy": False,
                "supervisor_busy": True,
                "managed_by_supervisor": False,
                "endpoint": spec.endpoint,
                "health_url": spec.health_url,
                "health_error": "runtime lifecycle operation in progress",
            }
        try:
            registry = self.registry()
            spec = registry.get(engine_id)
            if spec is None:
                return {
                    "engine": engine_id,
                    "configured": False,
                    "status": "unconfigured",
                    "managed_by_supervisor": False,
                }

            self._clean_exited(engine_id)
            owned = self._owned.get(engine_id)
            ready, health_error = self._health(spec)
            process_running = bool(owned and owned.process.poll() is None)

            if ready and health_error == "busy":
                state = "busy"
            elif process_running and ready:
                state = "ready"
            elif process_running:
                state = "starting"
            elif ready:
                state = "external-ready"
            elif not spec.enabled:
                state = "disabled"
            else:
                state = "stopped"

            # Last exit and restart cooldown are diagnostic evidence, not
            # proof an old model is still running. Never suppress a healthy
            # externally owned service merely because the managed copy died.
            cooldown = self._restart_cooldown(engine_id) if spec.managed else 0.0
            if spec.managed and state == "stopped" and cooldown > 0:
                state = "restart-backoff"
            if owned and owned.tree_uncertain and not ready:
                state = "process-tree-unresolved"

            # A durable receipt can outlive the parent that created it.
            # Surface unresolved evidence without claiming GPU hardware state.
            owner_receipt = None
            if spec.managed and spec.exclusive_group and not process_running:
                try:
                    old = read_receipt(spec.exclusive_group, self._slot_dir)
                    if old is not None:
                        owner_receipt = {
                            "state": old["state"],
                            "engine_id": old.get("engine_id"),
                            "pid": old.get("pid"),
                        }
                except UncertainRuntimeOwner as exc:
                    owner_receipt = {"state": "unreadable", "reason": str(exc)}
                if owner_receipt and not ready and state in {"stopped", "restart-backoff"}:
                    state = "ownership-unresolved"

            external_activation = None
            if not spec.managed:
                control = spec.external_control or {}
                if (
                    str(control.get("mode") or "").strip().lower() == "file"
                    and str(control.get("request_dir") or "").strip()
                    and str(control.get("service_key") or "").strip()
                ):
                    try:
                        old_request = pending_activation(
                            str(control["request_dir"]).strip(),
                            str(control["service_key"]).strip(),
                        )
                        if old_request is not None:
                            external_activation = {
                                "state": "pending",
                                "request_id": old_request["request_id"],
                                "engine_id": old_request["engine_id"],
                                "created_unix": old_request.get("created_unix"),
                            }
                            if not ready:
                                state = "external-activation-pending"
                    except (ExternalActivationUncertain, OSError) as exc:
                        external_activation = {
                            "state": "unreadable", "reason": str(exc),
                        }
                        if not ready:
                            state = "external-activation-unresolved"

            external = self._external_supervisor_status(spec) if not spec.managed else None
            external_pid = external.get("pid") if external else None
            external_state = str(external.get("supervisor_state") or "") if external else ""
            if external_state and not ready and external_activation is None:
                # External status files may lag behind live HTTP health. Do
                # not advertise READY from an old receipt when the endpoint
                # is unavailable, or mask a live busy/ready engine as stopped.
                normalized = external_state.lower().replace(" ", "-")
                if normalized in {"ready", "running", "healthy", "active"}:
                    state = "external-unverified"
                else:
                    state = "external-" + normalized

            return {
                "engine": engine_id,
                "runtime_id": spec.runtime_id,
                "runtime_revision": spec.configuration_revision,
                "configured": True,
                "enabled": spec.enabled,
                "mode": spec.mode,
                "lifecycle_owner": spec.lifecycle_owner,
                "status": state,
                "ready": ready,
                "busy": bool(ready and health_error == "busy"),
                "managed_by_supervisor": process_running,
                "pid": owned.process.pid if process_running else external_pid,
                "external_supervisor": external,
                "external_activation": external_activation,
                "endpoint": spec.endpoint,
                "health_url": spec.health_url,
                "exclusive_group": spec.exclusive_group,
                "start_on_demand": spec.start_on_demand,
                "health_error": health_error if not ready else None,
                "log_path": str(owned.log_path) if owned else None,
                "last_exit": dict(self._last_exit[engine_id]) if engine_id in self._last_exit else None,
                "restart_failures": self._failures.get(engine_id, {}).get("count", 0),
                "restart_backoff_seconds": round(cooldown, 2),
                "last_start_failure": self._failures.get(engine_id, {}).get("reason"),
                "owner_receipt": owner_receipt,
                "process_tree_unresolved": owned.tree_uncertain if owned else None,
            }

        finally:
            self._lock.release()

    def diagnostics(self) -> dict:
        registry = self.registry()
        return {
            "registry": registry.diagnostic(),
            "runtimes": [
                self.status(engine_id)
                for engine_id in sorted(registry.runtimes)
            ],
        }

    def _stop_owned(self, engine_id: str, spec: RuntimeSpec) -> dict:
        self._clean_exited(engine_id)
        owned = self._owned.get(engine_id)
        if not owned:
            ready, _ = self._health(spec)
            return {
                "engine": engine_id,
                "status": "external-ready" if ready else "stopped",
                "managed_by_supervisor": False,
            }

        # Contained groups must be stopped as a unit. If group teardown
        # cannot be confirmed, do not erase the launch receipt or unlock GPU.
        try:
            stop_managed_tree(
                owned.process,
                owned.slot_lease.group if owned.slot_lease else None,
                spec.shutdown_timeout,
            )
        except (ProcessTreeUncertain, subprocess.TimeoutExpired, OSError) as exc:
            owned.tree_uncertain = str(exc)
            raise RuntimeConflictError(
                f"{engine_id}: unable to verify managed process tree teardown: {exc}"
            ) from exc

        if owned.slot_lease and owned.launch_id:
            clear_launch_receipt(
                owned.slot_lease.group, self._slot_dir, owned.launch_id,
            )
        try:
            owned.log_handle.close()
        finally:
            self._owned.pop(engine_id, None)
            if owned.slot_lease:
                owned.slot_lease.release()

        return self.status(engine_id)

    def stop(self, engine_id: str) -> dict:
        with self._lock:
            spec = self.registry().get(engine_id)
            if spec is None:
                raise RuntimeSupervisorError(f"runtime is not configured: {engine_id}")
            with self._guard_group(spec):
                return self._stop_owned(engine_id, spec)

    def _enforce_exclusive_group(self, target: RuntimeSpec, registry: RuntimeRegistry) -> None:
        if not target.exclusive_group:
            return

        for engine_id, other in registry.runtimes.items():
            if engine_id == target.engine_id:
                continue
            if other.exclusive_group != target.exclusive_group:
                continue

            self._clean_exited(engine_id)
            owned = self._owned.get(engine_id)
            if owned and owned.tree_uncertain:
                raise RuntimeConflictError(
                    f"{target.engine_id}: conflicting engine {engine_id} has "
                    f"unresolved descendants: {owned.tree_uncertain}"
                )
            if owned and owned.process.poll() is None:
                self._stop_owned(engine_id, other)
                continue

            ready, _ = self._health(other)
            if ready:
                raise RuntimeConflictError(
                    f"{target.engine_id} and {engine_id} share exclusive group "
                    f"{target.exclusive_group}, but {engine_id} is already running "
                    "outside the supervisor. Stop it first or let the supervisor own it."
                )

    def start(self, engine_id: str) -> dict:
        with self._lock:
            registry = self.registry()
            spec = registry.get(engine_id)
            if spec is None:
                raise RuntimeSupervisorError(f"runtime is not configured: {engine_id}")
            if not spec.enabled:
                raise RuntimeSupervisorError(f"runtime is disabled: {engine_id}")

            with self._guard_group(spec):
                # Reap a dead managed process before selecting health timeout:
                # during restart backoff, do not spend multiple seconds waiting
                # for a known-offline health endpoint on every client request.
                self._clean_exited(engine_id)
                remaining = self._restart_cooldown(engine_id) if spec.managed else 0.0
                ready, _ = self._health(spec, timeout=0.2 if remaining > 0 else 1.5)
                if ready:
                    return self.status(engine_id)
                if not spec.managed:
                    raise RuntimeSupervisorError(
                        f"{engine_id} is external-only and is not currently healthy"
                    )

                owned = self._owned.get(engine_id)
                if owned and owned.tree_uncertain:
                    raise RuntimeConflictError(
                        f"{engine_id}: previous managed process tree unresolved: "
                        f"{owned.tree_uncertain}; stop or investigate before retry"
                    )
                if owned and owned.process.poll() is None:
                    return self.status(engine_id)

                if remaining > 0:
                    raise RuntimeSupervisorError(
                        f"{engine_id}: restart backoff active for {remaining:.1f}s "
                        "after a failed runtime attempt; retry later or use explicit admin restart"
                    )

                if spec.executable is None or not spec.executable.is_file():
                    raise RuntimeSupervisorError(
                        f"{engine_id}: executable does not exist: {spec.executable}"
                    )
                if spec.cwd is None or not spec.cwd.is_dir():
                    raise RuntimeSupervisorError(
                        f"{engine_id}: cwd does not exist: {spec.cwd}"
                    )

                self._enforce_exclusive_group(spec, registry)

                # Cooperative lock first, then reconcile a durable receipt
                # left by any previous parent that may have died.
                slot_lease = None
                if spec.exclusive_group:
                    slot_lease = acquire_runtime_slot(spec.exclusive_group, self._slot_dir)
                    if slot_lease is None:
                        raise RuntimeConflictError(
                            f"{engine_id}: exclusive runtime group {spec.exclusive_group} "
                            "is held by another CVS process; do not start a second GPU engine"
                        )
                process = None
                log_handle = None
                launch_id = None
                try:
                    if slot_lease:
                        try:
                            reconcile_prior_owner(spec.exclusive_group, self._slot_dir)
                        except UncertainRuntimeOwner as exc:
                            raise RuntimeConflictError(str(exc)) from exc

                    self._log_dir.mkdir(parents=True, exist_ok=True)
                    log_path = self._log_dir / f"{engine_id}.log"
                    log_handle = log_path.open("ab", buffering=0)
                    creationflags = 0
                    if os.name == "nt":
                        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

                    # Publish "starting" BEFORE entering Popen. If this parent
                    # crashes before writing the child's PID, later starts
                    # fail closed instead of presuming no child was spawned.
                    if slot_lease:
                        launch_id = publish_starting(
                            spec.exclusive_group, self._slot_dir, engine_id,
                        )
                    process = subprocess.Popen(
                        managed_command(spec.command(), spec.exclusive_group),
                        cwd=str(spec.cwd),
                        env=self._environment(spec),
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        creationflags=creationflags,
                        **spawn_options(spec.exclusive_group),
                    )
                    if slot_lease:
                        publish_running(
                            spec.exclusive_group, self._slot_dir, launch_id, process.pid,
                        )
                except BaseException as exc:
                    # If a child was created but its identity could not be
                    # published, kill and reap it before clearing its receipt.
                    reaped = process is None
                    if process is not None:
                        try:
                            stop_managed_tree(
                                process, spec.exclusive_group, spec.shutdown_timeout,
                            )
                            reaped = True
                        except (OSError, subprocess.TimeoutExpired, ProcessTreeUncertain):
                            reaped = False
                    if log_handle:
                        log_handle.close()
                    if slot_lease:
                        try:
                            # Popen errors before a returned handle are ambiguous:
                            # keep the "starting" intent for manual reconciliation.
                            if launch_id and process is not None and reaped:
                                clear_launch_receipt(
                                    spec.exclusive_group, self._slot_dir, launch_id,
                                )
                        finally:
                            slot_lease.release()
                    if isinstance(exc, UncertainRuntimeOwner):
                        raise RuntimeConflictError(str(exc)) from exc
                    if isinstance(exc, Exception) and not isinstance(exc, RuntimeConflictError):
                        self._record_failure(engine_id, "spawn-failed")
                        if isinstance(exc, OSError):
                            raise RuntimeSupervisorError(
                                f"{engine_id}: unable to spawn managed runtime: {exc}"
                            ) from exc
                    raise

                self._owned[engine_id] = _OwnedProcess(
                    process=process,
                    log_path=log_path,
                    log_handle=log_handle,
                    started_at=time.time(),
                    slot_lease=slot_lease,
                    launch_id=launch_id,
                )

                deadline = time.monotonic() + spec.startup_timeout
                last_error = None
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        tail = self._log_tail(log_path)
                        self._clean_exited(engine_id)
                        message = (
                            f"{engine_id} exited during startup with code {process.returncode}"
                        )
                        if tail:
                            message += f": {tail}"
                        raise RuntimeSupervisorError(message)

                    ready, last_error = self._health(spec)
                    if ready:
                        return self.status(engine_id)
                    time.sleep(0.5)

                tail = self._log_tail(log_path)
                self._stop_owned(engine_id, spec)
                self._record_failure(engine_id, "startup-timeout")
                message = f"{engine_id} did not become healthy within {spec.startup_timeout:.0f}s"
                if last_error:
                    message += f": {last_error}"
                if tail:
                    message += f" | log: {tail}"
                raise RuntimeSupervisorError(message)

    def restart(self, engine_id: str) -> dict:
        with self._lock:
            spec = self.registry().get(engine_id)
            if spec is None:
                raise RuntimeSupervisorError(f"runtime is not configured: {engine_id}")
            with self._guard_group(spec):
                self._stop_owned(engine_id, spec)
                ready, _ = self._health(spec)
                if ready:
                    raise RuntimeSupervisorError(
                        f"{engine_id} is still healthy after stopping supervisor-owned process; "
                        "another external process may own the endpoint"
                    )
                # Explicit admin restart is the escape hatch from auto-start
                # backoff. Do not clear a recorded exit code or diagnostic log.
                self._failures.pop(engine_id, None)
                return self.start(engine_id)

    def _request_external_activation(self, spec: RuntimeSpec) -> dict:
        control = spec.external_control or {}
        mode = str(control.get("mode") or "").strip().lower()
        if mode != "file":
            raise RuntimeSupervisorError(
                f"{spec.engine_id} is owned by {spec.lifecycle_owner} and is not healthy; "
                "no supported external control bridge is configured"
            )

        request_dir_raw = str(control.get("request_dir") or "").strip()
        service_key = str(control.get("service_key") or "").strip()
        if not request_dir_raw or not service_key:
            raise RuntimeSupervisorError(
                f"{spec.engine_id}: external file control requires request_dir and service_key"
            )

        request_dir = Path(request_dir_raw).expanduser().resolve()
        # Request-scope coordination is separate from the managed engine's
        # GPU slot lock. An external System Supervisor is still the owner.
        lease = acquire_activation_lease(request_dir, service_key)
        if lease is None:
            raise RuntimeConflictError(
                f"{spec.engine_id}: external activation already in progress "
                f"for service_key={service_key}; not publishing another command"
            )
        try:
            ready, _ = self._health(spec)
            pending = pending_activation(request_dir, service_key)
            if pending and pending["engine_id"] != spec.engine_id:
                raise RuntimeConflictError(
                    f"{spec.engine_id}: external service_key={service_key} has "
                    f"a pending request for another engine {pending['engine_id']}; "
                    "refusing to clear or replace its activation"
                )
            if ready:
                if pending:
                    acknowledge_activation(
                        request_dir, service_key, pending["request_id"],
                    )
                return self.status(spec.engine_id)

            if pending:
                raise RuntimeConflictError(
                    f"{spec.engine_id}: external activation request "
                    f"{pending['request_id']} is unresolved; inspect System "
                    "Supervisor and confirm process state before resubmitting"
                )

            # Publish durable intent before the command file, so parent
            # failure cannot silently erase a possibly delivered request.
            try:
                request = publish_activation(
                    request_dir, service_key, spec.engine_id,
                )
            except ExternalActivationUncertain as exc:
                raise RuntimeConflictError(str(exc)) from exc

            deadline = time.monotonic() + spec.startup_timeout
            last_error = None
            while time.monotonic() < deadline:
                ready, last_error = self._health(spec)
                if ready:
                    acknowledge_activation(
                        request_dir, service_key, request["request_id"],
                    )
                    return self.status(spec.engine_id)
                time.sleep(0.5)

            raise RuntimeSupervisorError(
                f"{spec.engine_id}: external activation request "
                f"{request['request_id']} timed out after "
                f"{spec.startup_timeout:.0f}s; request remains pending "
                "to prevent duplicate starts"
                + (f": {last_error}" if last_error else "")
            )
        except ExternalActivationUncertain as exc:
            raise RuntimeConflictError(str(exc)) from exc
        finally:
            lease.release()

    def _reconcile_healthy_external_activation(self, spec: RuntimeSpec) -> None:
        """Recover a late successful activation without issuing a new command.

        If another caller owns the activation lock, its own completion path
        will reconcile the receipt. Avoid blocking a healthy engine request.
        """
        control = spec.external_control or {}
        if str(control.get("mode") or "").strip().lower() != "file":
            return
        request_dir_raw = str(control.get("request_dir") or "").strip()
        service_key = str(control.get("service_key") or "").strip()
        if not request_dir_raw or not service_key:
            return
        try:
            lease = acquire_activation_lease(request_dir_raw, service_key)
        except OSError:
            return
        if lease is None:
            return
        try:
            pending = pending_activation(request_dir_raw, service_key)
            if pending and pending["engine_id"] == spec.engine_id:
                # The first health sample was observed outside this mutex.
                # Revalidate against the live endpoint before clearing.
                healthy, _ = self._health(spec, timeout=0.5)
                if healthy:
                    acknowledge_activation(
                        request_dir_raw, service_key, pending["request_id"],
                    )
        except (ExternalActivationUncertain, OSError):
            # Preserve ambiguous evidence; an unavailable receipt must not
            # turn a live healthy engine into a new activation request.
            pass
        finally:
            lease.release()

    def ensure_ready(self, engine_id: str) -> dict | None:
        registry = self.registry()
        spec = registry.get(engine_id)
        if spec is None or not spec.enabled:
            # Backward compatibility: an unregistered engine may still be managed
            # manually, exactly as before Runtime Registry was introduced.
            return None

        # Keep cooldown requests on the short health-probe path in start()
        # rather than accumulating the default 1.5s wait per request here.
        if spec.managed and spec.start_on_demand and self._restart_cooldown(engine_id) > 0:
            return self.start(engine_id)

        ready, _ = self._health(spec)
        if ready:
            if not spec.managed:
                self._reconcile_healthy_external_activation(spec)
            return self.status(engine_id)
        if spec.managed and spec.start_on_demand:
            return self.start(engine_id)
        if (not spec.managed) and spec.start_on_demand:
            return self._request_external_activation(spec)
        return self.status(engine_id)

    def shutdown_all(self) -> None:
        with self._lock:
            registry = self.registry()
            for engine_id in list(self._owned):
                spec = registry.get(engine_id)
                if spec is None:
                    spec = RuntimeSpec(
                        engine_id=engine_id,
                        runtime_id=f"{engine_id}-local",
                        runtime_version=None,
                        enabled=True,
                        mode="managed",
                        executable=None,
                        cwd=None,
                        args=(),
                        endpoint=None,
                        health_url=None,
                        start_on_demand=False,
                        exclusive_group=None,
                        startup_timeout=1,
                        shutdown_timeout=5,
                    )
                try:
                    with self._guard_group(spec):
                        self._stop_owned(engine_id, spec)
                except Exception:
                    pass


runtime_supervisor = RuntimeSupervisor()
atexit.register(runtime_supervisor.shutdown_all)
