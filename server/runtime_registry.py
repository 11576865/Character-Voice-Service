from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.config import PROJECT_ROOT


RUNTIME_SCHEMA_VERSION = 1
DEFAULT_RUNTIME_REGISTRY = PROJECT_ROOT / "config" / "runtimes.local.json"
_ENGINE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class RuntimeSpec:
    engine_id: str
    runtime_id: str
    runtime_version: str | None
    enabled: bool
    mode: str
    executable: Path | None
    cwd: Path | None
    args: tuple[str, ...]
    endpoint: str | None
    health_url: str | None
    start_on_demand: bool
    exclusive_group: str | None
    startup_timeout: float
    shutdown_timeout: float
    env: dict[str, str] = field(default_factory=dict)
    path_prepend: tuple[Path, ...] = ()
    lifecycle_owner: str = "cvs"
    external_control: dict[str, str] = field(default_factory=dict)
    dependencies: tuple[dict[str, Any], ...] = ()

    @property
    def managed(self) -> bool:
        return self.mode == "managed"

    @property
    def configuration_revision(self) -> str:
        payload = {
            "engine_id": self.engine_id,
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "mode": self.mode,
            "endpoint": self.endpoint,
            "health_url": self.health_url,
            "lifecycle_owner": self.lifecycle_owner,
            "exclusive_group": self.exclusive_group,
            "dependencies": list(self.dependencies),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def command(self) -> list[str]:
        if not self.managed or self.executable is None:
            raise ValueError(f"{self.engine_id}: runtime is not managed")
        return [str(self.executable), *self.args]

    def diagnostic(self) -> dict[str, Any]:
        return {
            "engine": self.engine_id,
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "runtime_revision": self.configuration_revision,
            "enabled": self.enabled,
            "mode": self.mode,
            "managed": self.managed,
            "executable": str(self.executable) if self.executable else None,
            "cwd": str(self.cwd) if self.cwd else None,
            "args": list(self.args),
            "endpoint": self.endpoint,
            "health_url": self.health_url,
            "start_on_demand": self.start_on_demand,
            "exclusive_group": self.exclusive_group,
            "startup_timeout": self.startup_timeout,
            "shutdown_timeout": self.shutdown_timeout,
            "env_keys": sorted(self.env),
            "path_prepend": [str(path) for path in self.path_prepend],
            "lifecycle_owner": self.lifecycle_owner,
            "external_control": dict(self.external_control),
            "dependencies": [dict(item) for item in self.dependencies],
        }


@dataclass(frozen=True)
class RuntimeRegistry:
    path: Path
    schema_version: int
    runtimes: dict[str, RuntimeSpec]

    def get(self, engine_id: str) -> RuntimeSpec | None:
        return self.runtimes.get(engine_id)

    def diagnostic(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "exists": self.path.is_file(),
            "schema_version": self.schema_version,
            "engines": [
                self.runtimes[key].diagnostic()
                for key in sorted(self.runtimes)
            ],
        }


def _registry_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    configured = os.environ.get("CVS_RUNTIME_REGISTRY", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_RUNTIME_REGISTRY


def _expand_path(value: object, *, field_name: str) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    text = os.path.expandvars(str(value).strip())
    return Path(text).expanduser()


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: object, default: float, *, minimum: float, maximum: float, label: str) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return number


def _parse_spec(engine_id: str, raw: object) -> RuntimeSpec:
    if not _ENGINE_ID.fullmatch(engine_id):
        raise ValueError(f"invalid runtime engine id: {engine_id}")
    if not isinstance(raw, dict):
        raise ValueError(f"{engine_id}: runtime entry must be an object")

    mode = str(raw.get("mode") or "managed").strip().lower()
    if mode not in {"managed", "external"}:
        raise ValueError(f"{engine_id}: mode must be managed or external")

    runtime_id = str(raw.get("runtime_id") or f"{engine_id}-local").strip()
    if not _ENGINE_ID.fullmatch(runtime_id):
        raise ValueError(f"{engine_id}: invalid runtime_id")
    runtime_version = _string_or_none(raw.get("runtime_version"))

    executable = _expand_path(
        raw.get("executable", raw.get("python")),
        field_name=f"{engine_id}.executable",
    )
    cwd = _expand_path(raw.get("cwd"), field_name=f"{engine_id}.cwd")

    args_raw = raw.get("args") or []
    if not isinstance(args_raw, list) or not all(isinstance(item, str) for item in args_raw):
        raise ValueError(f"{engine_id}: args must be a string array")
    args = tuple(os.path.expandvars(item) for item in args_raw)

    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise ValueError(f"{engine_id}: env must be an object")
    env: dict[str, str] = {}
    for key, value in env_raw.items():
        if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
            raise ValueError(f"{engine_id}: invalid environment variable name")
        if value is None:
            continue
        env[key] = os.path.expandvars(str(value))

    prepend_raw = raw.get("path_prepend") or []
    if not isinstance(prepend_raw, list):
        raise ValueError(f"{engine_id}: path_prepend must be an array")
    path_prepend = tuple(
        path
        for path in (
            _expand_path(item, field_name=f"{engine_id}.path_prepend")
            for item in prepend_raw
        )
        if path is not None
    )

    enabled = bool(raw.get("enabled", True))
    if enabled and mode == "managed":
        if executable is None:
            raise ValueError(f"{engine_id}: managed runtime requires executable/python")
        if cwd is None:
            raise ValueError(f"{engine_id}: managed runtime requires cwd")

    endpoint = _string_or_none(raw.get("endpoint"))
    health_url = _string_or_none(raw.get("health_url"))
    if enabled and not health_url:
        raise ValueError(f"{engine_id}: enabled runtime requires health_url")

    group = _string_or_none(raw.get("exclusive_group"))
    if group and not _ENGINE_ID.fullmatch(group):
        raise ValueError(f"{engine_id}: invalid exclusive_group")

    lifecycle_owner = str(raw.get("lifecycle_owner") or ("cvs" if mode == "managed" else "external")).strip()
    control_raw = raw.get("external_control") or {}
    if not isinstance(control_raw, dict):
        raise ValueError(f"{engine_id}: external_control must be an object")
    external_control = {
        str(key): os.path.expandvars(str(value))
        for key, value in control_raw.items()
        if value is not None
    }

    deps_raw = raw.get("dependencies") or []
    if not isinstance(deps_raw, list):
        raise ValueError(f"{engine_id}: dependencies must be an array")
    dependencies: list[dict[str, Any]] = []
    for index, item in enumerate(deps_raw):
        if not isinstance(item, dict):
            raise ValueError(f"{engine_id}: dependency #{index + 1} must be an object")
        dep_id = str(item.get("id") or "").strip()
        dep_kind = str(item.get("kind") or "").strip()
        if not dep_id or not dep_kind:
            raise ValueError(f"{engine_id}: dependency #{index + 1} requires id and kind")
        normalized = {
            str(key): (
                os.path.expandvars(str(value))
                if isinstance(value, str)
                else value
            )
            for key, value in item.items()
            if value is not None
        }
        normalized["id"] = dep_id
        normalized["kind"] = dep_kind
        normalized.setdefault("ownership", "unspecified")
        dependencies.append(normalized)

    return RuntimeSpec(
        engine_id=engine_id,
        runtime_id=runtime_id,
        runtime_version=runtime_version,
        enabled=enabled,
        mode=mode,
        executable=executable,
        cwd=cwd,
        args=args,
        endpoint=endpoint,
        health_url=health_url,
        start_on_demand=bool(raw.get("start_on_demand", False)),
        exclusive_group=group,
        startup_timeout=_number(
            raw.get("startup_timeout"), 120.0, minimum=1.0, maximum=900.0,
            label=f"{engine_id}.startup_timeout",
        ),
        shutdown_timeout=_number(
            raw.get("shutdown_timeout"), 15.0, minimum=1.0, maximum=120.0,
            label=f"{engine_id}.shutdown_timeout",
        ),
        env=env,
        path_prepend=path_prepend,
        lifecycle_owner=lifecycle_owner,
        external_control=external_control,
        dependencies=tuple(dependencies),
    )


def load_runtime_registry(path: str | Path | None = None) -> RuntimeRegistry:
    registry_path = _registry_path(path)
    if not registry_path.is_file():
        return RuntimeRegistry(
            path=registry_path,
            schema_version=RUNTIME_SCHEMA_VERSION,
            runtimes={},
        )

    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Runtime Registry JSON is invalid: {registry_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("Runtime Registry root must be an object")
    schema_version = raw.get("schema_version")
    if schema_version != RUNTIME_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported Runtime Registry schema_version: {schema_version}; "
            f"expected {RUNTIME_SCHEMA_VERSION}"
        )

    engines = raw.get("engines") or {}
    if not isinstance(engines, dict):
        raise ValueError("Runtime Registry engines must be an object")

    parsed = {
        str(engine_id): _parse_spec(str(engine_id), spec)
        for engine_id, spec in engines.items()
    }
    return RuntimeRegistry(
        path=registry_path,
        schema_version=schema_version,
        runtimes=parsed,
    )


def runtime_base_url(engine_id: str, fallback: str) -> str:
    """Return the machine-local registered endpoint when available."""
    registry = load_runtime_registry()
    spec = registry.get(engine_id)
    if spec and spec.enabled and spec.endpoint:
        return spec.endpoint.rstrip("/")
    return fallback.rstrip("/")
