"""Strict, bounded PCM/IEEE-float RIFF/WAVE output contract.

HTTP 200 and an audio/* Content-Type are NOT proof of decodable WAV.
This parser verifies the complete RIFF framing, fmt and data chunks,
interleaved frame sizing, and bounded audio duration. It does not claim
audible content or speaker/model identity.
"""
from __future__ import annotations

import struct


MAX_WAV_BYTES = 128 * 1024 * 1024
MAX_WAV_SECONDS = 900
_MIN_SAMPLE_RATE = 8000
_MAX_SAMPLE_RATE = 192000

_PCM_GUID_TAIL = bytes.fromhex("00001000800000aa00389b71")


class InvalidWav(ValueError):
    """An untrusted synthesis response violates CVS's PCM WAV contract."""


def read_bounded_wav_candidate(response) -> bytes:
    """Read at most one extra byte; never buffer an unbounded HTTP payload."""
    payload = response.read(MAX_WAV_BYTES + 1)
    if len(payload) > MAX_WAV_BYTES:
        raise InvalidWav("WAV payload exceeds the 128 MiB response limit")
    return payload


def validate_wav_bytes(audio: bytes) -> bytes:
    """Return validated bytes, or raise InvalidWav with a stable error reason.

    Only little-endian RIFF/WAVE with integer PCM, IEEE float, or standard
    WAVE_FORMAT_EXTENSIBLE carrying one of those two formats is accepted.
    This explicitly rejects compressed WAV codecs and RF64; these would
    require a separate decoder and revised public-output contract.
    """
    if not isinstance(audio, bytes):
        raise InvalidWav("WAV payload is not a bytes object")
    size = len(audio)
    if size > MAX_WAV_BYTES:
        raise InvalidWav("WAV payload exceeds the 128 MiB response limit")
    if size < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise InvalidWav("missing or truncated RIFF/WAVE header")
    if struct.unpack_from("<I", audio, 4)[0] != size - 8:
        raise InvalidWav("RIFF declared size does not match payload length")

    position = 12
    fmt = None
    data_bytes = None
    while position < size:
        if size - position < 8:
            raise InvalidWav("truncated WAV chunk header")
        chunk_id, chunk_size = struct.unpack_from("<4sI", audio, position)
        position += 8
        remaining = size - position
        if chunk_size > remaining:
            raise InvalidWav("WAV chunk extends beyond response payload")
        if chunk_id == b"fmt ":
            if fmt is not None or chunk_size < 16:
                raise InvalidWav("missing, duplicated or truncated WAV fmt chunk")
            encoding, channels, rate, bytes_per_second, alignment, bits = struct.unpack_from(
                "<HHIIHH", audio, position
            )
            if encoding == 0xFFFE:
                if chunk_size < 40:
                    raise InvalidWav("truncated WAVE_FORMAT_EXTENSIBLE fmt chunk")
                extension_size = struct.unpack_from("<H", audio, position + 16)[0]
                if extension_size < 22:
                    raise InvalidWav("invalid WAVE_FORMAT_EXTENSIBLE extension")
                guid = audio[position + 24:position + 40]
                if guid[4:] != _PCM_GUID_TAIL or guid[:2] not in {b"\x01\x00", b"\x03\x00"} or guid[2:4] != b"\x00\x00":
                    raise InvalidWav("unsupported WAVE_FORMAT_EXTENSIBLE subformat")
                encoding = struct.unpack_from("<H", guid)[0]
            if encoding not in {1, 3}:
                raise InvalidWav("unsupported compressed or non-PCM WAV codec")
            if (channels < 1 or channels > 8 or rate < _MIN_SAMPLE_RATE or rate > _MAX_SAMPLE_RATE):
                raise InvalidWav("unsupported WAV channel count or sample rate")
            if (encoding == 1 and bits not in {8, 16, 24, 32}) or (
                encoding == 3 and bits not in {32, 64}
            ):
                raise InvalidWav("unsupported WAV sample format")
            if alignment != channels * (bits // 8) or bytes_per_second != rate * alignment:
                raise InvalidWav("inconsistent WAV frame alignment or byte rate")
            fmt = (rate, alignment)

        elif chunk_id == b"data":
            if fmt is None:
                raise InvalidWav("WAV data precedes its fmt chunk")
            if data_bytes is not None:
                raise InvalidWav("multiple WAV data chunks are unsupported")
            data_bytes = chunk_size

        position += chunk_size
        if chunk_size % 2:
            if position >= size:
                raise InvalidWav("missing WAV chunk padding byte")
            position += 1

    if position != size or fmt is None or data_bytes is None:
        raise InvalidWav("missing WAV fmt or data chunk")
    sample_rate, block_alignment = fmt
    if data_bytes == 0 or data_bytes % block_alignment:
        raise InvalidWav("WAV contains no complete audio frames")
    frames = data_bytes // block_alignment
    if frames / sample_rate > MAX_WAV_SECONDS:
        raise InvalidWav("WAV exceeds the 15-minute audio duration limit")
    return audio
