import json

from fastapi.testclient import TestClient

import server.app as app_module


client = TestClient(app_module.app)


def write_profile(directory, voice_id="march-7th", display_name="March 7th"):
    profile = {
        "name": display_name,
        "reference_audio": "C:/private/reference.wav",
        "reference_text": "Exact reference text.",
        "reference_language": "en",
        "target_language": "en",
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
    assert module.status_code == 200
    assert "javascript" in module.headers["content-type"]


def test_voice_list_reads_valid_profiles(tmp_path, monkeypatch):
    write_profile(tmp_path)
    (tmp_path / "example.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)

    response = client.get("/v1/voices")

    assert response.status_code == 200
    assert response.json() == {
        "voices": [{
            "id": "march-7th",
            "name": "March 7th",
            "reference_language": "en",
            "target_language": "en",
        }]
    }


def test_voice_list_marks_invalid_json(tmp_path, monkeypatch):
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)

    response = client.get("/v1/voices")

    assert response.status_code == 200
    assert response.json()["voices"][0]["error"] == "invalid profile"


def test_speech_returns_mocked_wav(tmp_path, monkeypatch):
    profile = write_profile(tmp_path)
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
        "input": "Hello",
        "response_format": "wav",
        "speed": 1.25,
    })

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == wav
    assert calls == [{"text": "Hello", "speed": 1.25, "profile": profile}]


def test_speech_rejects_invalid_requests(tmp_path, monkeypatch):
    write_profile(tmp_path)
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path)

    assert client.post("/v1/audio/speech", json={"voice": "march-7th", "input": "   "}).status_code == 400
    assert client.post("/v1/audio/speech", json={"voice": "march-7th", "input": "Hello", "speed": 0}).status_code == 400
    assert client.post("/v1/audio/speech", json={"voice": "march-7th", "input": "Hello", "response_format": "mp3"}).status_code == 400
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
