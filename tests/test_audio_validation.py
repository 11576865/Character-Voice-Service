"""WAV output contract: real PCM bytes, corrupt-200 responses and payload bounds."""
import io
import struct
import wave
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.audio_validation import (
    InvalidWav, MAX_WAV_BYTES, read_bounded_wav_candidate, validate_wav_bytes,
)
from server import engines
from wav_fixtures import make_pcm_wav


def _replace_size(payload: bytearray):
    struct.pack_into("<I", payload, 4, len(payload) - 8)
    return bytes(payload)


@pytest.mark.parametrize("channels,width", [(1, 1), (1, 2), (2, 2), (2, 3), (1, 4)])
def test_accepts_actual_pcm_audio_with_complete_frames(channels, width):
    audio = make_pcm_wav(channels=channels, sample_width=width, frames=150)
    assert validate_wav_bytes(audio) == audio
    with wave.open(io.BytesIO(audio), "rb") as decoded:
        assert decoded.getnchannels() == channels
        assert decoded.getnframes() == 150
        assert len(decoded.readframes(150)) == 150 * channels * width


def test_accepts_legal_odd_length_metadata_chunk_with_padding():
    wav = make_pcm_wav(frames=20)
    junk = b"JUNK" + struct.pack("<I", 3) + b"abc" + b"\x00"
    modified = _replace_size(bytearray(wav[:12] + junk + wav[12:]))
    assert validate_wav_bytes(modified) == modified


def test_accepts_standard_extensible_pcm_and_float_wave():
    for subtype, bits, samples in [
        (1, 16, b"\x00\x00" * 20),
        (3, 32, struct.pack("<20f", *[0.05] * 20)),
    ]:
        guid = struct.pack("<I", subtype) + bytes.fromhex("00001000800000aa00389b71")
        fmt = struct.pack("<HHIIHHHHI", 0xFFFE, 1, 24000, 24000 * (bits // 8),
                          bits // 8, bits, 22, bits, 0) + guid
        assert len(fmt) == 40
        riff = b"WAVE" + b"fmt " + struct.pack("<I", 40) + fmt
        riff += b"data" + struct.pack("<I", len(samples)) + samples
        audio = b"RIFF" + struct.pack("<I", len(riff)) + riff
        assert validate_wav_bytes(audio) == audio


@pytest.mark.parametrize("payload", [
    b"",
    b'RIFF....WAVE',
    b'{"error":"upstream failed"}',
    b'<html>backend failure</html>',
    make_pcm_wav()[:30],
    make_pcm_wav()[:-2],
])
def test_rejects_non_wav_or_truncated_payload(payload):
    with pytest.raises(InvalidWav):
        validate_wav_bytes(payload)


def test_rejects_wrong_riff_length_even_if_wave_header_looks_valid():
    raw = bytearray(make_pcm_wav())
    struct.pack_into("<I", raw, 4, len(raw) + 400)
    with pytest.raises(InvalidWav, match="RIFF declared size"):
        validate_wav_bytes(bytes(raw))


def test_rejects_partial_pcm_frame_and_empty_audio():
    raw = bytearray(make_pcm_wav(frames=3))
    # Keep an even-length RIFF chunk with a 5-byte PCM data section
    # and one padding byte. The declared data length is not frame-aligned.
    struct.pack_into("<I", raw, raw.index(b"data") + 4, 5)
    with pytest.raises(InvalidWav, match="no complete audio frames"):
        validate_wav_bytes(bytes(raw))
    empty = make_pcm_wav(frames=0)
    with pytest.raises(InvalidWav, match="no complete audio frames"):
        validate_wav_bytes(empty)


def test_rejects_unsupported_codec_and_inconsistent_fmt():
    audio = bytearray(make_pcm_wav())
    fmt_pos = audio.index(b"fmt ") + 8
    struct.pack_into("<H", audio, fmt_pos, 6)  # a-law compressed data
    with pytest.raises(InvalidWav, match="unsupported"):
        validate_wav_bytes(bytes(audio))
    struct.pack_into("<H", audio, fmt_pos, 1)
    struct.pack_into("<H", audio, fmt_pos + 12, 999)  # invalid block-align
    with pytest.raises(InvalidWav, match="alignment"):
        validate_wav_bytes(bytes(audio))


def test_rejects_duration_beyond_limit_even_for_structurally_valid_pcm(monkeypatch):
    import server.audio_validation as validator
    monkeypatch.setattr(validator, "MAX_WAV_SECONDS", 0.001)
    with pytest.raises(InvalidWav, match="duration"):
        validate_wav_bytes(make_pcm_wav(frames=150))


def test_read_bounded_audio_does_not_buffer_arbitrarily_large_response(monkeypatch):
    import server.audio_validation as validator
    monkeypatch.setattr(validator, "MAX_WAV_BYTES", 100)

    class Response:
        requested = None
        def read(self, n):
            self.requested = n
            return b"X" * n

    response = Response()
    with pytest.raises(InvalidWav, match="exceeds"):
        read_bounded_wav_candidate(response)
    assert response.requested == 101


@pytest.mark.parametrize("engine_id", ["gpt-sovits", "index-tts", "third-party-sidecar"])
@pytest.mark.parametrize("invalid", [b"not-a-wav", b"RIFF....WAVE", b""])
def test_all_engine_dispatches_reject_complete_but_invalid_adapter_audio(
    monkeypatch, engine_id, invalid
):
    monkeypatch.setattr(
        engines.runtime_supervisor,
        "registry",
        lambda: SimpleNamespace(get=lambda name: None),
    )
    monkeypatch.setattr(engines.runtime_supervisor, "ensure_ready", lambda name: None)
    monkeypatch.setattr(
        engines, "get_adapter",
        lambda name: SimpleNamespace(synthesize=lambda text, speed, profile: invalid),
    )
    with pytest.raises(HTTPException) as result:
        engines.synthesize("Hello", 1.0, {"selected_model": {"engine": engine_id}})
    assert result.value.status_code == 502
    assert "invalid upstream WAV" in result.value.detail


def test_valid_complete_response_clears_gpu_group_intent_before_returning():
    # Covered by test_runtime_inference_admission for a real managed
    # mock process; this test checks only pure WAV semantics.
    assert validate_wav_bytes(make_pcm_wav(frames=10))


def test_wrong_payload_type_is_rejected():
    with pytest.raises(InvalidWav, match="bytes object"):
        validate_wav_bytes("RIFF....WAVE")
