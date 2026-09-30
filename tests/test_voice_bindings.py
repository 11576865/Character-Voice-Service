import json

from server import voice_bindings


def test_binding_revision_is_deterministic():
    raw = {
        "binding_id": "march-index",
        "voice_id": "march-7th",
        "engine": "index-tts",
        "model_id": "index-tts-2.5",
        "speaker_reference_id": "neutral",
        "emotion_policy": "speaker",
        "parameters": {"emo_alpha": 1.0},
    }
    first = voice_bindings._normalize_binding(raw)
    second = voice_bindings._normalize_binding(raw)
    assert first["revision"] == second["revision"]
    assert len(first["revision"]) == 64


def test_register_and_resolve_binding(tmp_path):
    path = tmp_path / "voice-bindings.json"
    saved = voice_bindings.register_binding({
        "binding_id": "march-index",
        "voice_id": "march-7th",
        "engine": "index-tts",
        "model_id": "index-tts-2.5",
        "speaker_reference_id": "neutral",
        "emotion_reference_id": "happy",
        "emotion_policy": "separate",
        "parameters": {"emo_alpha": 0.7},
    }, path)

    by_model = voice_bindings.resolve_binding(
        "march-7th", selector="index-tts-2.5", path=path
    )
    assert by_model["binding_id"] == "march-index"
    assert by_model["revision"] == saved["revision"]

    encoded = json.loads(path.read_text(encoding="utf-8"))
    assert "revision" not in encoded["bindings"]["march-index"]
