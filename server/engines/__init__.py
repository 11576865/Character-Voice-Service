from __future__ import annotations

from server.engine_registry import load_engine_descriptors
from server.engines.gpt_sovits import GPTSoVITSAdapter
from server.engines.index_tts import IndexTTSAdapter
from server.engines.sidecar import SidecarV1Adapter
from fastapi import HTTPException

from server.runtime_inference_admission import RuntimeGroupAdmissionError, runtime_group_guard
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
    registry_spec = runtime_supervisor.registry().get(engine_id)
    group = registry_spec.exclusive_group if registry_spec and registry_spec.enabled else None
    try:
        # Acquire GPU group activity BEFORE ensure_ready(), because that call
        # may launch an engine and auto-stop its managed sibling. The
        # operation lock is reentrant only on this thread, so the nested
        # start() remains legal but unrelated stop()/start() calls lose.
        with runtime_group_guard(group, runtime_supervisor._slot_dir) as admission:
            runtime_supervisor.ensure_ready(engine_id)
            adapter = get_adapter(engine_id)
            request_id = admission.begin_inference(engine_id) if admission else None
            # Only a completed synthesis response retires the durable
            # in-flight evidence. Timeouts, connection drops and abnormal
            # controller termination must not assume GPU work has stopped.
            try:
                audio = adapter.synthesize(text, speed, profile)
            except HTTPException as exc:
                # Built-in adapter 400/409 responses are deterministic local
                # validation/model-mode refusals, before any TTS HTTP request
                # is submitted. They cannot represent an in-flight GPU job.
                # Upstream HTTP errors are translated to 502/503 and retain
                # the conservative pending receipt for operator review.
                if admission and request_id and exc.status_code in {400, 409}:
                    admission.finish_inference(request_id)
                raise
            if admission and request_id:
                admission.finish_inference(request_id)
            return audio
    except RuntimeGroupAdmissionError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{engine_id}: GPU inference admission denied: {exc}",
            headers={"Retry-After": "2"},
        ) from exc
