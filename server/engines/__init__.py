from server.engines.gpt_sovits import GPTSoVITSAdapter
from server.engines.index_tts import IndexTTSAdapter
from server.runtime_supervisor import runtime_supervisor


_ADAPTERS = {
    "gpt-sovits": GPTSoVITSAdapter(),
    "index-tts": IndexTTSAdapter(),
}


def get_adapter(engine_id: str):
    try:
        return _ADAPTERS[engine_id]
    except KeyError as exc:
        raise ValueError(f"unsupported speech engine: {engine_id}") from exc


def list_engines() -> list[dict]:
    return [
        {"engine": engine_id, "capabilities": adapter.capabilities()}
        for engine_id, adapter in sorted(_ADAPTERS.items())
    ]


def synthesize(text: str, speed: float, profile: dict) -> bytes:
    model = profile.get("selected_model") or {}
    engine_id = str(model.get("engine") or "gpt-sovits")
    runtime_supervisor.ensure_ready(engine_id)
    return get_adapter(engine_id).synthesize(text, speed, profile)
