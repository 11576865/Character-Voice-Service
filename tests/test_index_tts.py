import json

from server.engines import index_tts


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "audio/wav"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_index_tts_adapter_sends_resolved_reference(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(b"RIFF....WAVE")

    monkeypatch.setattr(index_tts.urllib.request, "urlopen", fake_urlopen)

    adapter = index_tts.IndexTTSAdapter()
    audio = adapter.synthesize(
        text="Hello from IndexTTS.",
        speed=1.25,
        profile={
            "target_language": "en",
            "reference_audio": "D:/refs/march.wav",
            "parameters": {"emo_alpha": 0.7},
        },
    )

    payload = json.loads(captured["request"].data.decode("utf-8"))
    assert audio == b"RIFF....WAVE"
    assert payload["speaker_audio"] == "D:/refs/march.wav"
    assert payload["lang"] == "en"
    assert payload["duration_factor"] == 0.8
    assert payload["emo_alpha"] == 0.7
    assert captured["timeout"] == 300


def test_index_tts_capabilities_are_conservative():
    caps = index_tts.IndexTTSAdapter().capabilities()
    assert caps["max_concurrency"] == 1
    assert caps["supports_cancel"] is False
    assert caps["audio_streaming"] is False
    assert caps["output_sample_rate"] == 22050
