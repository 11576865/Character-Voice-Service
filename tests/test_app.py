import json

from fastapi.testclient import TestClient

import server.app as app_module


client = TestClient(app_module.app)


def write_legacy_profile(directory, voice_id="march-7th", display_name="March 7th"):
    profile = {
        "name": display_name,
        "reference_audio": "C:/private/reference.wav",
        "reference_text": "Exact reference text.",
        "reference_language": "en",
        "target_language": "en",
    }
    (directory / f"{voice_id}.json").write_text(json.dumps(profile), encoding="utf-8")
    return profile


def write_registry_profile(directory, voice_id="march-7th", display_name="March 7th"):
    profile = {
        "schema_version": 2,
        "name": display_name,
        "target_language": "en",
        "default_model": "self-400",
        "models": {
            "self-400": {
                "name": "Self 400",
                "engine": "gpt-sovits",
                "version": "v2pro",
                "gpt_weights": "D:/models/self.ckpt",
                "sovits_weights": "D:/models/self.pth",
            },
            "downloaded": {
                "name": "Downloaded",
                "engine": "gpt-sovits",
                "version": "v2pro",
                "gpt_weights": "D:/models/downloaded.ckpt",
                "sovits_weights": "D:/models/downloaded.pth",
            },
        },
        "default_reference": "neutral",
        "references": {
            "neutral": {
                "name": "Neutral",
                "audio": "D:/refs/neutral.wav",
                "text": "Neutral reference.",
                "language": "en",
                "emotion": "neutral",
                "intensity": 0.4,
                "quality": "good",
            },
            "surprised": {
                "name": "Surprised",
                "audio": "D:/refs/surprised.wav",
                "text": "Surprised reference!",
                "language": "en",
                "emotion": "surprised",
                "intensity": 0.8,
                "quality": "good",
            },
        },
    }
    (directory / f"{voice_id}.json").write_text(json.dumps(profile), encoding="utf-8")
    return profile


def test_root_lists_public_endpoints():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["health"] == "/health"
    assert response.json()["voices"] == "/v1/voices"


def test_reader_modules_are_served():
    page = client.get("/test")
    module = client.get("/reader-assets/js/reader.js")
    assert page.status_code == 200
    assert "/reader-assets/js/reader.js" in page.text
    assert 'id="modelId"' in page.text
    assert 'id="referenceId"' in page.text
    assert module.status_code == 200
    assert "javascript" in module.headers["content-type"]


def test_voice_list_reads_legacy_profile_without_exposing_paths(tmp_path, monkeypatch):
    write_legacy_profile(tmp_path)
    (tmp_path / "example.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)

    response = client.get("/v1/voices")

    assert response.status_code == 200
    voice = response.json()["voices"][0]
    assert voice["id"] == "march-7th"
    assert voice["name"] == "March 7th"
    assert voice["schema_version"] == 1
    assert voice["default_model"] == "loaded"
    assert voice["models"][0]["managed"] is False
    assert voice["default_reference"] == "default"
    assert voice["references"][0]["language"] == "en"
    assert "C:/private" not in json.dumps(voice)


def test_voice_list_exposes_registry_metadata_only(tmp_path, monkeypatch):
    write_registry_profile(tmp_path)
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)

    response = client.get("/v1/voices")
    voice = response.json()["voices"][0]

    assert voice["default_model"] == "self-400"
    assert [item["id"] for item in voice["models"]] == ["self-400", "downloaded"]
    assert voice["default_reference"] == "neutral"
    assert [item["id"] for item in voice["references"]] == ["neutral", "surprised"]
    assert voice["references"][1]["emotion"] == "surprised"
    encoded = json.dumps(voice)
    assert "D:/models" not in encoded
    assert "D:/refs" not in encoded
    assert "Surprised reference!" not in encoded


def test_voice_list_marks_invalid_json(tmp_path, monkeypatch):
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)

    response = client.get("/v1/voices")

    assert response.status_code == 200
    assert response.json()["voices"][0]["error"] == "invalid profile"


def test_speech_resolves_requested_model_and_reference(tmp_path, monkeypatch):
    write_registry_profile(tmp_path)
    wav = b"RIFF\x00\x00\x00\x00WAVE"
    calls = []
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)
    monkeypatch.setattr(app_module, "SAVE_GENERATED_WAV", False)

    def fake_synthesize(**kwargs):
        calls.append(kwargs)
        return wav

    monkeypatch.setattr(app_module, "synthesize", fake_synthesize)
    response = client.post("/v1/audio/speech", json={
        "voice": "march-7th",
        "model_id": "downloaded",
        "reference_id": "surprised",
        "input": "Hello",
        "response_format": "wav",
        "speed": 1.25,
    })

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == wav
    selection = calls[0]["profile"]
    assert calls[0]["text"] == "Hello"
    assert calls[0]["speed"] == 1.25
    assert selection["selected_model"]["id"] == "downloaded"
    assert selection["selected_reference"]["id"] == "surprised"
    assert selection["reference_audio"] == "D:/refs/surprised.wav"


def test_speech_legacy_profile_still_works(tmp_path, monkeypatch):
    write_legacy_profile(tmp_path)
    wav = b"RIFF\x00\x00\x00\x00WAVE"
    calls = []
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)
    monkeypatch.setattr(app_module, "SAVE_GENERATED_WAV", False)
    monkeypatch.setattr(
        app_module,
        "synthesize",
        lambda **kwargs: calls.append(kwargs) or wav,
    )

    response = client.post("/v1/audio/speech", json={
        "voice": "march-7th",
        "input": "Hello",
    })

    assert response.status_code == 200
    assert calls[0]["profile"]["selected_model"]["managed"] is False
    assert calls[0]["profile"]["reference_audio"] == "C:/private/reference.wav"


def test_speech_rejects_unknown_registered_assets(tmp_path, monkeypatch):
    write_registry_profile(tmp_path)
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)

    assert client.post("/v1/audio/speech", json={
        "voice": "march-7th", "model_id": "missing", "input": "Hello"
    }).status_code == 404
    assert client.post("/v1/audio/speech", json={
        "voice": "march-7th", "reference_id": "missing", "input": "Hello"
    }).status_code == 404


def test_speech_rejects_invalid_requests(tmp_path, monkeypatch):
    write_legacy_profile(tmp_path)
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)

    assert client.post("/v1/audio/speech", json={"voice": "march-7th", "input": "   "}).status_code == 400
    assert client.post("/v1/audio/speech", json={"voice": "march-7th", "input": "Hello", "speed": 0}).status_code == 400
    assert client.post("/v1/audio/speech", json={"voice": "march-7th", "input": "Hello", "response_format": "mp3"}).status_code == 400
    assert client.post("/v1/audio/speech", json={"voice": "march-7th", "input": "Hello", "model": "other"}).status_code == 400
    assert client.post("/v1/audio/speech", json={"input": "Hello", "voice": "../secret"}).status_code == 400
    assert client.post("/v1/audio/speech", json={"input": "Hello", "voice": "missing"}).status_code == 404


def test_speech_requires_explicit_voice_id():
    response = client.post("/v1/audio/speech", json={"input": "Hello"})
    assert response.status_code == 422


def test_example_template_cannot_be_used_as_a_voice():
    response = client.post("/v1/audio/speech", json={"voice": "example", "input": "Hello"})
    assert response.status_code == 404


def test_health_reports_backend_offline(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fail)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["backend_status"] == "offline"
