import io
import wave

import pytest

from tests.continuous_read_test import inspect_wav


def make_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(b"\x00\x00" * 2400)
    return output.getvalue()


def test_inspect_wav_accepts_non_empty_pcm_wav():
    result = inspect_wav(make_wav())
    assert result["channels"] == 1
    assert result["sample_rate_hz"] == 24000
    assert result["frame_count"] == 2400
    assert result["duration_seconds"] == 0.1


@pytest.mark.parametrize("audio", [b"", b"not a wav", b"RIFF0000WAVE"])
def test_inspect_wav_rejects_invalid_or_empty_audio(audio):
    with pytest.raises(ValueError):
        inspect_wav(audio)
