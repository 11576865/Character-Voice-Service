from __future__ import annotations

from server.engine_registry import load_engine_descriptors
from server.engines.gpt_sovits import GPTSoVITSAdapter
from server.engines.index_tts import IndexTTSAdapter
from server.engines.sidecar import SidecarV1Adapter
from server.runtime_supervisor import runtime_supervisor


_BUILTIN_FACTORIES = {
    "builtin:gpt-sovits": GPTSoVITSAdapter,
    "builtin:index-tts": IndexTTSAdapter,
}


def _load_adapters() -> dict[str, object]:
    descriptors = load_engine_descriptors()
    adapters: dict[str, object] = {}
    for engine_id, descriptor in descriptors.items():
        if descriptor.adapter_kind in _BUILTIN_FACTORIES:
            adapter = _BUILTIN_FACTORIES[descriptor.adapter_kind]()
        elif descriptor.adapter_kind == "cvs-sidecar-v1":
            adapter = SidecarV1Adapter(descriptor)
        else:
            raise ValueError(
                f"unsupported adapter kind for {engine_id}: {descriptor.adapter_kind}"
            )
        adapters[engine_id] = adapter
    return adapters


def get_adapter(engine_id: str):
    adapters = _load_adapters()
    try:
        return adapters[engine_id]
    except KeyError as exc:
        raise ValueError(f"unsupported speech engine: {engine_id}") from exc


def list_engines() -> list[dict]:
    descriptors = load_engine_descriptors()
    adapters = _load_adapters()
    items = []
    for engine_id in sorted(adapters):
        descriptor = descriptors[engine_id]
        items.append(
            {
                "engine": engine_id,
                "name": descriptor.name,
                "capability_kind": descriptor.capability_kind,
                "adapter_kind": descriptor.adapter_kind,
                "capabilities": adapters[engine_id].capabilities(),
            }
        )
    return items


def synthesize(text: str, speed: float, profile: dict) -> bytes:
    model = profile.get("selected_model") or {}
    engine_id = str(model.get("engine") or "gpt-sovits")
    runtime_supervisor.ensure_ready(engine_id)
    return get_adapter(engine_id).synthesize(text, speed, profile)
