import json
import urllib.error

import pytest
from fastapi import HTTPException

import server.backends.gpt_sovits as backend


PROFILE = {
    "reference_audio": "C:/private/reference.wav",
    "reference_text": "Exact reference text.",
    "reference_language": "en",
    "target_language": "en",
    "parameters": {"top_k": 7, "temperature": 0.8},
}


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body

    def close(self):
        pass


def test_synthesize_maps_profile_to_backend_payload(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(b"wav-data")

    monkeypatch.setattr(backend.urllib.request, "urlopen", fake_urlopen)
    result = backend.synthesize("Hello", 1.2, PROFILE)

    assert result == b"wav-data"
    assert captured["url"] == backend.GPT_SOVITS_TTS_URL
    assert captured["timeout"] == 120
    assert captured["payload"]["text"] == "Hello"
    assert captured["payload"]["speed_factor"] == 1.2
    assert captured["payload"]["ref_audio_path"] == PROFILE["reference_audio"]
    assert captured["payload"]["top_k"] == 7
    assert captured["payload"]["temperature"] == 0.8
    assert captured["payload"]["streaming_mode"] is False
    assert captured["payload"]["media_type"] == "wav"


def test_synthesize_converts_connection_error_to_503(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(backend.urllib.request, "urlopen", fail)
    with pytest.raises(HTTPException) as exc_info:
        backend.synthesize("Hello", 1.0, PROFILE)

    assert exc_info.value.status_code == 503
    assert "connection refused" in exc_info.value.detail


def test_synthesize_converts_backend_http_error_to_502(monkeypatch):
    error = urllib.error.HTTPError(
        backend.GPT_SOVITS_TTS_URL,
        400,
        "Bad Request",
        hdrs=None,
        fp=FakeResponse(b"invalid request"),
    )

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(backend.urllib.request, "urlopen", fail)
    with pytest.raises(HTTPException) as exc_info:
        backend.synthesize("Hello", 1.0, PROFILE)

    assert exc_info.value.status_code == 502
    assert "invalid request" in exc_info.value.detail
