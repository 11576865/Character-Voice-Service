# CVS speech WAV output integrity gate v1

## Source audit and previous failure

CVS's public `POST /v1/audio/speech` promises an `audio/wav`
response. Before this change, `server/backends/gpt_sovits.py` returned
arbitrary HTTP 200 response bytes without decoding them, and
`server/engines/index_tts.py` / `sidecar.py` checked only that the
Content-Type contained `audio/` and the body was nonempty. The
public endpoint then labeled any returned bytes `audio/wav` and,
when enabled, saved them under a `.wav` filename.

Tests previously used `b"wav-data"` or `b"RIFF....WAVE"` as
successful generated output: neither is a complete PCM WAV.

A 200 status or an `audio/` header cannot establish valid audio.
This is a source-level contract gap, not evidence of a particular
production model delivering malformed bytes.

## New acceptance boundary

`server/audio_validation.py` is the shared pure-Python validator:

- The entire response must be a canonical little-endian
  `RIFF/WAVE` container with an exact RIFF byte count and valid
  non-overlapping chunk boundaries and padding.
- Exactly one `fmt ` chunk must precede exactly one `data` chunk.
  Extra metadata chunks are permitted as long as their declared
  lengths and padding are consistent.
- The selected format must be integer PCM (8/16/24/32-bit) or IEEE
  float (32/64-bit), either directly or inside the standard
  `WAVE_FORMAT_EXTENSIBLE` format with the correct GUID.
  Compressed WAV codecs, unrecognized extensions, RF64 and arbitrary
  HTML/JSON error bodies are rejected as outside this contract.
- Audio must have 1–8 channels, 8–192 kHz sample rate, consistent
  byte rate and frame block alignment, at least one full data
  frame, and a calculated duration no greater than **900 seconds**.
- Each TTS adapter limits its HTTP response read to
  **128 MiB + one byte**; oversized responses are rejected rather
  than being fully buffered. The size cap includes container overhead.

**The central engine dispatcher validates returned audio bytes only
after retiring a successfully completed group inference receipt.**
This distinction is intentional: an upstream HTTP transaction can
finish yet return invalid bytes. Such a result is a **502 Bad Gateway**,
not an unknown-outcome GPU inference requiring manual recovery.

The public route independently validates bytes again **before saving
files or returning success headers**, so newly added or monkeypatched
adapters cannot silently bypass the wire contract.

## Scope of "valid WAV"

This verifies container and uncompressed audio-frame structure. It
does not prove the waveform is audible, non-silent, correctly spoken,
free from clipping, or from the selected character/model. PCM samples
can still contain silence or poor speech and pass this gate.
The format and duration limits are conservative defaults, not a
verified performance budget for every supported TTS model.

The adapter-level bounded HTTP read can fail before the upstream
request has completed (e.g., oversized streaming response). In such
unknown-outcome cases, the existing inference admission receipt may
intentionally remain unresolved until the backend is inspected. A
**fully completed but structurally malformed** response is rejected
after the group receipt has been retired. Direct adapter calls are
internal; the public API and the engine dispatcher are the
mandatory audio acceptance boundaries.

## Regression coverage

- `tests/wav_fixtures.py` creates real PCM WAV bytes with the
  standard-library `wave` writer.
- `tests/test_audio_validation.py` checks real PCM decoding, float
  and extensible WAV framing, legal padded metadata, wrong RIFF
  length, truncated chunks, empty/partial frames, unsupported
  codecs, duration limits and bounded reads, plus all three engine
  identity routes through the shared dispatcher.
- `tests/test_contract.py` verifies a real PCM payload is returned
  with success metadata and that invalid WAV/JSON bodies cannot be
  saved or returned as successful `audio/wav`.
- Previously fake successful audio in `tests/test_gpt_sovits.py`,
  `tests/test_index_tts.py` and the managed-runtime activity tests
  is replaced with real PCM WAV test fixtures.
- Linux full pytest and Windows targeted CI run the validation tests.

## Deployment acceptance and unresolved risks

This change is **source + automated mock validation**; real
IndexTTS / GPT-SoVITS / third-party Sidecar outputs on a GPU host
have not been verified. In particular, the new 128 MiB/15-minute
limits must be measured against realistic longest supported user
inputs before declaring field acceptance.

The separate request-to-backend model identity/provenance problem
is not fixed by WAV integrity alone. Real output should additionally
be checked for correct speaker and model identity, intelligibility,
duration and GPU recovery. Production audio quality and System
Supervisor interoperability remain FIELD-PENDING.

**Both Linux and Windows CI must pass on the final PR head before
merging.**
