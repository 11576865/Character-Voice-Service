import json
import urllib.error

import pytest
from fastapi import HTTPException

import server.backends.gpt_sovits as backend


BASE_PROFILE = {
    "reference_audio": "C:/private/reference.wav",
    "reference_text": "Exact reference text.",
    "reference_language": "en",
    "target_language": "en",
    "parameters": {"top_k": 7, "temperature": 0.8},
    "selected_model": {
        "id": "loaded",
        "engine": "gpt-sovits",
        "managed": False,
        "gpt_weights": None,
        "sovits_weights": None,
    },
}


def managed_profile(model_id="self-400", gpt="D:/models/a.ckpt", sovits="D:/models/a.pth"):
    profile = dict(BASE_PROFILE)
    profile["selected_model"] = {
        "id": model_id,
        "engine": "gpt-sovits",
        "managed": True,
        "gpt_weights": gpt,
        "sovits_weights": sovits,
    }
    return profile


class FakeResponse:
    def __init__(self, body=b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body

    def close(self):
        pass


@pytest.fixture(autouse=True)
def reset_backend_state():
    backend.reset_model_state()
    yield
    backend.reset_model_state()


def test_synthesize_maps_profile_to_backend_payload(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(b"wav-data")

    monkeypatch.setattr(backend.urllib.request, "urlopen", fake_urlopen)
    result = backend.synthesize("Hello", 1.2, BASE_PROFILE)

    assert result == b"wav-data"
    assert captured["url"] == backend.GPT_SOVITS_TTS_URL
    assert captured["timeout"] == 120
    assert captured["payload"]["text"] == "Hello"
    assert captured["payload"]["speed_factor"] == 1.2
    assert captured["payload"]["ref_audio_path"] == BASE_PROFILE["reference_audio"]
    assert captured["payload"]["top_k"] == 7
    assert captured["payload"]["temperature"] == 0.8
    assert captured["payload"]["streaming_mode"] is False
    assert captured["payload"]["media_type"] == "wav"


def test_managed_model_switches_weights_before_tts_and_reuses_loaded_model(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        url = request if isinstance(request, str) else request.full_url
        calls.append(url)
        if isinstance(request, str):
            return FakeResponse(b'{"message":"success"}')
        return FakeResponse(b"wav-data")

    monkeypatch.setattr(backend.urllib.request, "urlopen", fake_urlopen)
    profile = managed_profile()

    assert backend.synthesize("One", 1.0, profile) == b"wav-data"
    assert backend.synthesize("Two", 1.0, profile) == b"wav-data"

    assert calls[0].startswith(backend.GPT_SOVITS_SET_SOVITS_WEIGHTS_URL)
    assert "D%3A%2Fmodels%2Fa.pth" in calls[0]
    assert calls[1].startswith(backend.GPT_SOVITS_SET_GPT_WEIGHTS_URL)
    assert "D%3A%2Fmodels%2Fa.ckpt" in calls[1]
    assert calls[2] == backend.GPT_SOVITS_TTS_URL
    assert calls[3] == backend.GPT_SOVITS_TTS_URL
    assert len(calls) == 4


def test_switching_registered_model_loads_new_weight_pair(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        url = request if isinstance(request, str) else request.full_url
        calls.append(url)
        return FakeResponse(b"wav-data")

    monkeypatch.setattr(backend.urllib.request, "urlopen", fake_urlopen)

    backend.synthesize("One", 1.0, managed_profile())
    backend.synthesize(
        "Two",
        1.0,
        managed_profile("other", "D:/models/b.ckpt", "D:/models/b.pth"),
    )

    weight_calls = [url for url in calls if "/set_" in url]
    assert len(weight_calls) == 4
    assert any("b.pth" in url for url in weight_calls)
    assert any("b.ckpt" in url for url in weight_calls)


def test_external_model_is_rejected_after_service_managed_switch(monkeypatch):
    def fake_urlopen(request, timeout):
        return FakeResponse(b"wav-data")

    monkeypatch.setattr(backend.urllib.request, "urlopen", fake_urlopen)
    backend.synthesize("Managed", 1.0, managed_profile())

    with pytest.raises(HTTPException) as exc_info:
        backend.synthesize("External", 1.0, BASE_PROFILE)

    assert exc_info.value.status_code == 409
    assert "externally loaded" in exc_info.value.detail


def test_synthesize_converts_connection_error_to_503(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(backend.urllib.request, "urlopen", fail)
    with pytest.raises(HTTPException) as exc_info:
        backend.synthesize("Hello", 1.0, BASE_PROFILE)

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
        backend.synthesize("Hello", 1.0, BASE_PROFILE)

    assert exc_info.value.status_code == 502
    assert "invalid request" in exc_info.value.detail


def test_weight_switch_http_error_is_reported_before_tts(monkeypatch):
    error = urllib.error.HTTPError(
        backend.GPT_SOVITS_SET_SOVITS_WEIGHTS_URL,
        400,
        "Bad Request",
        hdrs=None,
        fp=FakeResponse(b"bad model path"),
    )

    monkeypatch.setattr(backend.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(HTTPException) as exc_info:
        backend.synthesize("Hello", 1.0, managed_profile())

    assert exc_info.value.status_code == 502
    assert "weight switch" in exc_info.value.detail
    assert "bad model path" in exc_info.value.detail
