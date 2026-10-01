from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.config import PROJECT_ROOT


ENGINE_SCHEMA_VERSION = 1
ENGINE_DESCRIPTOR_DIR = PROJECT_ROOT / "engines"
_ENGINE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ALLOWED_ADAPTERS = {
    "builtin:gpt-sovits",
    "builtin:index-tts",
    "cvs-sidecar-v1",
}


@dataclass(frozen=True)
class EngineDescriptor:
    engine_id: str
    name: str
    capability_kind: str
    adapter_kind: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    protocol: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    def public(self) -> dict[str, Any]:
        return {
            "engine": self.engine_id,
            "name": self.name,
            "capability_kind": self.capability_kind,
            "adapter_kind": self.adapter_kind,
            "capabilities": dict(self.capabilities),
            "protocol": dict(self.protocol),
            "metadata": dict(self.metadata),
            "source": str(self.source_path) if self.source_path else None,
        }


def _validate_descriptor(raw: object, source: Path) -> EngineDescriptor:
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: engine descriptor must be an object")
    if raw.get("schema_version") != ENGINE_SCHEMA_VERSION:
        raise ValueError(f"{source}: unsupported engine descriptor schema")

    engine_id = str(raw.get("engine_id") or "").strip()
    if not _ENGINE_ID.fullmatch(engine_id):
        raise ValueError(f"{source}: invalid engine_id")

    adapter_kind = str(raw.get("adapter_kind") or "").strip()
    if adapter_kind not in _ALLOWED_ADAPTERS:
        raise ValueError(
            f"{source}: unsupported adapter_kind {adapter_kind!r}; "
            f"allowed: {', '.join(sorted(_ALLOWED_ADAPTERS))}"
        )

    capability_kind = str(raw.get("capability_kind") or "speech-synthesis").strip()
    if not capability_kind:
        raise ValueError(f"{source}: capability_kind is required")

    capabilities = raw.get("capabilities") or {}
    protocol = raw.get("protocol") or {}
    metadata = raw.get("metadata") or {}
    if not isinstance(capabilities, dict):
        raise ValueError(f"{source}: capabilities must be an object")
    if not isinstance(protocol, dict):
        raise ValueError(f"{source}: protocol must be an object")
    if not isinstance(metadata, dict):
        raise ValueError(f"{source}: metadata must be an object")

    if adapter_kind == "cvs-sidecar-v1":
        health_path = str(protocol.get("health_path") or "/health")
        synthesize_path = str(protocol.get("synthesize_path") or "/v1/synthesize")
        if not health_path.startswith("/") or not synthesize_path.startswith("/"):
            raise ValueError(f"{source}: sidecar protocol paths must start with /")

    return EngineDescriptor(
        engine_id=engine_id,
        name=str(raw.get("name") or engine_id).strip() or engine_id,
        capability_kind=capability_kind,
        adapter_kind=adapter_kind,
        capabilities=dict(capabilities),
        protocol=dict(protocol),
        metadata=dict(metadata),
        source_path=source,
    )


def load_engine_descriptors(
    directory: Path = ENGINE_DESCRIPTOR_DIR,
) -> dict[str, EngineDescriptor]:
    if not directory.is_dir():
        return {}

    descriptors: dict[str, EngineDescriptor] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name.casefold().endswith(".example.json"):
            continue
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        descriptor = _validate_descriptor(raw, path)
        if descriptor.engine_id in descriptors:
            raise ValueError(
                f"duplicate engine_id {descriptor.engine_id!r}: "
                f"{descriptors[descriptor.engine_id].source_path} and {path}"
            )
        descriptors[descriptor.engine_id] = descriptor
    return descriptors


def get_engine_descriptor(engine_id: str) -> EngineDescriptor:
    descriptors = load_engine_descriptors()
    try:
        return descriptors[engine_id]
    except KeyError as exc:
        raise ValueError(f"engine descriptor not found: {engine_id}") from exc
