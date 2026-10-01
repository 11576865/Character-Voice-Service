from __future__ import annotations

import atexit
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


class RuntimeSupervisorError(RuntimeError):
    pass


class RuntimeConflictError(RuntimeSupervisorError):
    pass


@dataclass
class _OwnedProcess:
    process: subprocess.Popen
    log_path: Path
    log_handle: BinaryIO
    started_at: float


class RuntimeSupervisor:
    def __init__(
        self,
        *,
        registry_path: str | Path | None = None,
        log_dir: str | Path | None = None,
    ):
        self._registry_path = registry_path
        self._log_dir = Path(log_dir) if log_dir else DATA_DIR / "runtime-logs"
        self._owned: dict[str, _OwnedProcess] = {}
        self._lock = threading.RLock()

    def registry(self) -> RuntimeRegistry:
        return load_runtime_registry(self._registry_path)

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
                response.read(128)
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

    def _clean_exited(self, engine_id: str) -> None:
        owned = self._owned.get(engine_id)
        if not owned:
            return
        if owned.process.poll() is None:
            return
        try:
            owned.log_handle.close()
        finally:
            self._owned.pop(engine_id, None)

    def _log_tail(self, path: Path, limit: int = 4000) -> str:
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        return data[-limit:].decode("utf-8", errors="replace").strip()

    def status(self, engine_id: str) -> dict:
        with self._lock:
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

            if process_running and ready:
                state = "ready"
            elif process_running:
                state = "starting"
            elif ready:
                state = "external-ready"
            elif not spec.enabled:
                state = "disabled"
            else:
                state = "stopped"

            return {
                "engine": engine_id,
                "configured": True,
                "enabled": spec.enabled,
                "mode": spec.mode,
                "status": state,
                "ready": ready,
                "managed_by_supervisor": process_running,
                "pid": owned.process.pid if process_running else None,
                "endpoint": spec.endpoint,
                "health_url": spec.health_url,
                "exclusive_group": spec.exclusive_group,
                "start_on_demand": spec.start_on_demand,
                "health_error": health_error if not ready else None,
                "log_path": str(owned.log_path) if owned else None,
            }

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

        process = owned.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=spec.shutdown_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        try:
            owned.log_handle.close()
        finally:
            self._owned.pop(engine_id, None)

        return self.status(engine_id)

    def stop(self, engine_id: str) -> dict:
        with self._lock:
            spec = self.registry().get(engine_id)
            if spec is None:
                raise RuntimeSupervisorError(f"runtime is not configured: {engine_id}")
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

            ready, _ = self._health(spec)
            if ready:
                return self.status(engine_id)
            if not spec.managed:
                raise RuntimeSupervisorError(
                    f"{engine_id} is external-only and is not currently healthy"
                )

            self._clean_exited(engine_id)
            owned = self._owned.get(engine_id)
            if owned and owned.process.poll() is None:
                return self.status(engine_id)

            if spec.executable is None or not spec.executable.is_file():
                raise RuntimeSupervisorError(
                    f"{engine_id}: executable does not exist: {spec.executable}"
                )
            if spec.cwd is None or not spec.cwd.is_dir():
                raise RuntimeSupervisorError(
                    f"{engine_id}: cwd does not exist: {spec.cwd}"
                )

            self._enforce_exclusive_group(spec, registry)

            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self._log_dir / f"{engine_id}.log"
            log_handle = log_path.open("ab", buffering=0)

            creationflags = 0
            if os.name == "nt":
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

            try:
                process = subprocess.Popen(
                    spec.command(),
                    cwd=str(spec.cwd),
                    env=self._environment(spec),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            except Exception:
                log_handle.close()
                raise

            self._owned[engine_id] = _OwnedProcess(
                process=process,
                log_path=log_path,
                log_handle=log_handle,
                started_at=time.time(),
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
            self._stop_owned(engine_id, spec)
            ready, _ = self._health(spec)
            if ready:
                raise RuntimeSupervisorError(
                    f"{engine_id} is still healthy after stopping supervisor-owned process; "
                    "another external process may own the endpoint"
                )
            return self.start(engine_id)

    def _request_external_activation(self, spec: RuntimeSpec) -> dict:
        control = spec.external_control or {}
        mode = str(control.get("mode") or "").strip().lower()
        if mode != "file":
            raise RuntimeSupervisorError(
                f"{spec.engine_id} is owned by {spec.lifecycle_owner} and is not healthy; "
                "no supported external control bridge is configured"
            )

        request_dir = Path(str(control.get("request_dir") or "")).expanduser()
        service_key = str(control.get("service_key") or "").strip()
        if not request_dir or not service_key:
            raise RuntimeSupervisorError(
                f"{spec.engine_id}: external file control requires request_dir and service_key"
            )

        request_dir.mkdir(parents=True, exist_ok=True)
        request_id = uuid.uuid4().hex
        tmp_path = request_dir / f".{request_id}.tmp"
        request_path = request_dir / f"{request_id}.json"
        payload = {
            "schema_version": 1,
            "action": "activate-engine",
            "engine_id": spec.engine_id,
            "service_key": service_key,
            "request_id": request_id,
            "created_unix": time.time(),
        }
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp_path.replace(request_path)

        deadline = time.monotonic() + spec.startup_timeout
        last_error = None
        while time.monotonic() < deadline:
            ready, last_error = self._health(spec)
            if ready:
                return self.status(spec.engine_id)
            time.sleep(0.5)

        raise RuntimeSupervisorError(
            f"{spec.engine_id} activation request timed out after {spec.startup_timeout:.0f}s"
            + (f": {last_error}" if last_error else "")
        )

    def ensure_ready(self, engine_id: str) -> dict | None:
        registry = self.registry()
        spec = registry.get(engine_id)
        if spec is None or not spec.enabled:
            # Backward compatibility: an unregistered engine may still be managed
            # manually, exactly as before Runtime Registry was introduced.
            return None

        ready, _ = self._health(spec)
        if ready:
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
                    self._stop_owned(engine_id, spec)
                except Exception:
                    pass


runtime_supervisor = RuntimeSupervisor()
atexit.register(runtime_supervisor.shutdown_all)
