from __future__ import annotations

from typing import Any

from server.backends.base import TTSEngine
from server.backends.gpt_sovits import GPTSoVITSEngine
from server.backends.index_tts import IndexTTSEngine


_ENGINES: dict[str, TTSEngine] = {
    "gpt-sovits": GPTSoVITSEngine(),
    "index-tts": IndexTTSEngine(),
}


def engine_ids() -> tuple[str, ...]:
    return tuple(_ENGINES)


def get_engine(engine_id: str) -> TTSEngine:
    key = str(engine_id or "").strip().lower()
    try:
        return _ENGINES[key]
    except KeyError as exc:
        raise KeyError(f"unsupported TTS engine: {engine_id}") from exc


def engine_summaries() -> list[dict[str, Any]]:
    items = []
    for engine in _ENGINES.values():
        health = engine.health()
        items.append({
            "id": engine.engine_id,
            "name": engine.display_name,
            "status": health.get("status", "unknown"),
            "capabilities": engine.capabilities(),
        })
    return items


def synthesize(text: str, speed: float, profile: dict) -> bytes:
    model = profile.get("selected_model") or {}
    engine_id = str(model.get("engine") or "gpt-sovits").strip().lower()
    return get_engine(engine_id).synthesize(text=text, speed=speed, profile=profile)
