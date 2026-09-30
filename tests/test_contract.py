import json

from fastapi.testclient import TestClient

import server.app as app_module


client = TestClient(app_module.app)


def _write_profile(path):
    path.write_text(json.dumps({
        "schema_version": 2,
        "name": "March 7th",
        "target_language": "en",
        "default_model": "local",
        "models": {
            "local": {
                "name": "Local",
                "engine": "gpt-sovits",
                "version": "v4",
                "gpt_weights": "D:/models/a.ckpt",
                "sovits_weights": "D:/models/a.pth"
            }
        },
        "default_reference": "neutral",
        "references": {
            "neutral": {
                "name": "Neutral",
                "audio": "D:/refs/a.wav",
                "text": "Reference.",
                "language": "en",
                "emotion": "neutral",
                "quality": "good"
            }
        }
    }), encoding="utf-8")


def test_root_exposes_contract_without_reader_routes():
    response = client.get("/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["contract"] == "Character Voice Contract v1"
    assert payload["speech"] == "/v1/audio/speech"
    assert client.get("/test").status_code == 404
    assert client.get("/v1/books").status_code == 404


def test_speech_returns_contract_metadata(tmp_path, monkeypatch):
    _write_profile(tmp_path / "march-7th.json")
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)
    monkeypatch.setattr(app_module, "SAVE_GENERATED_WAV", False)
    monkeypatch.setattr(app_module, "synthesize", lambda **kwargs: b"RIFF....WAVE")

    response = client.post("/v1/audio/speech", json={
        "voice": "march-7th",
        "input": "Hello",
        "speed": 1.0
    })

    assert response.status_code == 200
    assert response.headers["x-cvs-voice"] == "march-7th"
    assert response.headers["x-cvs-engine"] == "gpt-sovits"
    assert response.headers["x-selected-reference"] == "neutral"
    assert len(response.headers["x-cvs-generation-revision"]) == 64
    assert "x-cvs-serving-revision" not in response.headers


def test_public_voice_summary_hides_local_paths_and_reference_text(tmp_path, monkeypatch):
    _write_profile(tmp_path / "march-7th.json")
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)

    response = client.get("/v1/voices")
    assert response.status_code == 200
    encoded = json.dumps(response.json())
    assert "D:/models" not in encoded
    assert "D:/refs" not in encoded
    assert "Reference." not in encoded


def test_speech_routes_to_index_tts_runtime_binding(tmp_path, monkeypatch):
    path = tmp_path / "march-7th.json"
    raw = {
        "schema_version": 2,
        "name": "March 7th",
        "target_language": "en",
        "default_model": "gpt",
        "models": {
            "gpt": {
                "name": "GPT",
                "engine": "gpt-sovits",
                "version": "v4",
                "gpt_weights": "D:/models/a.ckpt",
                "sovits_weights": "D:/models/a.pth",
            },
            "index-tts-2.5": {
                "name": "IndexTTS 2.5 shared runtime",
                "engine": "index-tts",
                "version": "2.5",
                "parameters": {"emo_alpha": 1.0},
            },
        },
        "default_reference": "neutral",
        "references": {
            "neutral": {
                "name": "Neutral",
                "audio": "D:/refs/a.wav",
                "text": "Reference.",
                "language": "en",
                "emotion": "neutral",
                "quality": "good",
            }
        },
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)
    monkeypatch.setattr(app_module, "SAVE_GENERATED_WAV", False)

    calls = []
    monkeypatch.setattr(
        app_module,
        "synthesize",
        lambda **kwargs: calls.append(kwargs) or b"RIFF....WAVE",
    )

    response = client.post("/v1/audio/speech", json={
        "voice": "march-7th",
        "model_id": "index-tts-2.5",
        "input": "Hello from IndexTTS.",
    })

    assert response.status_code == 200
    assert response.headers["x-cvs-engine"] == "index-tts"
    assert response.headers["x-cvs-model"] == "index-tts-2.5"
    assert calls[0]["profile"]["selected_model"]["engine"] == "index-tts"
