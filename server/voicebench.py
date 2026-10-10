"""Sequential provenance-checked CVS generation over a frozen held-out corpus.

A completed run proves validated API responses and WAV artifacts, NOT voice
quality. No automatic evaluation decision or model promotion is made.
"""
import hashlib
import json
import os
import re
import time
import uuid
import wave
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from server.benchmark_dataset import verify_dataset

SCHEMA_VERSION = 1
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_PROVENANCE_FIELDS = (
    "voice", "model", "engine", "model_revision", "generation_revision",
    "reference", "runtime", "runtime_revision", "binding", "binding_revision",
)


def _identity(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{field} must be a stable ASCII identifier")
    return value


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("voicebench only supports an explicit loopback HTTP CVS origin")
    try:
        if not parsed.port:
            raise ValueError("CVS origin must include its port")
    except ValueError as exc:
        raise ValueError("CVS origin must include a valid port") from exc
    return url.rstrip("/")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _durable_json(path: Path, record: dict) -> None:
    """Atomic replace for checkpoints; audio artifacts are never overwritten."""
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _assert_wav(data: bytes) -> dict:
    try:
        with wave.open(BytesIO(data), "rb") as audio:
            if (audio.getcomptype() != "NONE" or audio.getnframes() <= 0
                    or audio.getframerate() <= 0 or audio.getnchannels() <= 0):
                raise ValueError("synthesis returned empty or unsupported PCM WAV")
            expected_bytes = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
            if len(audio.readframes(audio.getnframes())) != expected_bytes:
                raise ValueError("synthesis returned a truncated PCM WAV")
            return {
                "frames": audio.getnframes(),
                "channels": audio.getnchannels(),
                "sample_rate": audio.getframerate(),
                "duration_seconds": audio.getnframes() / audio.getframerate(),
            }
    except (wave.Error, EOFError) as exc:
        raise ValueError("synthesis response is not a readable PCM WAV") from exc


def _resolved(response: httpx.Response, *, voice: str, reference_id: str) -> dict:
    response.raise_for_status()
    raw = response.json()
    if not isinstance(raw, dict):
        raise ValueError("speech resolve response must be an object")
    if raw.get("voice") != voice or raw.get("reference") != reference_id:
        raise ValueError("resolved voice/reference differs from the pinned request")
    if not isinstance(raw.get("model"), str) or not raw["model"]:
        raise ValueError("resolved model identity missing")
    if not isinstance(raw.get("engine"), str) or not raw["engine"]:
        raise ValueError("resolved engine identity missing")
    _sha(raw.get("model_revision"), "model revision")
    _sha(raw.get("generation_revision"), "generation revision")
    return {key: raw.get(key) for key in _PROVENANCE_FIELDS}


def _check_headers(response: httpx.Response, identity: dict) -> str:
    expected = {
        "x-cvs-voice": identity["voice"],
        "x-cvs-model": identity["model"],
        "x-cvs-engine": identity["engine"],
        "x-cvs-model-revision": identity["model_revision"],
        "x-cvs-generation-revision": identity["generation_revision"],
        "x-selected-reference": identity["reference"],
    }
    for key, value in expected.items():
        if response.headers.get(key) != value:
            raise ValueError(f"speech response provenance mismatch: {key}")
    request_id = response.headers.get("x-cvs-request-id")
    if not request_id:
        raise ValueError("speech response has no request ID")
    if "audio/wav" not in response.headers.get("content-type", "").lower():
        raise ValueError("speech response is not audio/wav")
    return request_id


def _confirm_reference(client: httpx.Client, *, voice: str, reference_id: str,
                       admin_token: str, expected_hash: str) -> None:
    endpoint = f"/v1/voices/{quote(voice, safe='')}/references/{quote(reference_id, safe='')}/audio"
    response = client.get(endpoint, headers={"X-CVS-Token": admin_token})
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != expected_hash:
        raise ValueError("live CVS reference WAV does not match the frozen reference item")


def _validate_resume(saved: dict, settings: dict, samples: list[dict], output_dir: Path) -> None:
    if not isinstance(saved, dict) or saved.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported run checkpoint")
    if saved.get("settings") != settings:
        raise ValueError("resume settings or frozen corpus differ from checkpoint")
    if not isinstance(saved.get("identity"), dict):
        raise ValueError("run checkpoint lacks resolved identity")
    records = saved.get("items")
    if not isinstance(records, dict) or set(records) != {item["id"] for item in samples}:
        raise ValueError("run checkpoint item identities differ from frozen corpus")
    for item in samples:
        name = item["id"]
        record = records[name]
        if not isinstance(record, dict):
            raise ValueError(f"invalid checkpoint item: {name}")
        out = output_dir / "audio" / f"{name}.wav"
        if record.get("status") == "ok":
            if not out.is_file() or hashlib.sha256(out.read_bytes()).hexdigest() != record.get("output_sha256"):
                raise ValueError(f"completed audio artifact missing or mutated: {name}")
            _assert_wav(out.read_bytes())
        elif out.exists():
            raise ValueError(f"untracked audio file would be overwritten: {name}")


def run_voicebench(
    *, manifest_path: Path, audio_root: Path, output_dir: Path,
    voice: str, model_id: str, reference_id: str, reference_item_id: str,
    admin_token: str, base_url: str = "http://127.0.0.1:9881", speed: float = 1.0,
    timeout: float = 180.0, resume: bool = False, client: httpx.Client | None = None,
) -> dict:
    """Run one pinned voice/model/reference over ALL held-out examples.

    Network/identity failure leaves partial evidence. Resume rechecks corpus,
    live reference, serving identity and all previously completed WAV hashes.
    """
    voice = _identity(voice, "voice")
    model_id = _identity(model_id, "model_id")
    reference_id = _identity(reference_id, "reference_id")
    reference_item_id = _identity(reference_item_id, "reference_item_id")
    base_url = _loopback_url(base_url)
    if not isinstance(admin_token, str) or not admin_token.strip():
        raise ValueError("a private CVS admin token is required to verify live reference WAV")
    if not (0 < speed <= 4.0) or not (0 < timeout <= 3600):
        raise ValueError("speed and timeout must be positive and within supported limits")

    # Rehash every original WAV before contacting the synthesis backend.
    checked = verify_dataset(manifest_path, audio_root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples = sorted(
        (row for row in manifest["items"] if row["split"] == "test-recorded"),
        key=lambda row: row["id"],
    )
    if not samples:
        raise ValueError("benchmark corpus has no held-out test-recorded items")
    references = [row for row in manifest["items"] if row["id"] == reference_item_id]
    if (len(references) != 1 or not references[0]["reference"]
            or references[0]["split"] == "test-recorded"):
        raise ValueError("reference item must be a non-test member of the frozen reference pool")
    settings = {
        "dataset_id": checked["dataset_id"], "dataset_sha256": checked["dataset_sha256"],
        "voice": voice, "requested_model_id": model_id,
        "reference_id": reference_id, "reference_item_id": reference_item_id,
        "reference_audio_sha256": references[0]["audio_sha256"],
        "base_url": base_url, "speed": speed,
    }
    output_dir = Path(output_dir)
    if not resume and output_dir.exists():
        raise ValueError("output directory already exists; choose a new run directory or --resume")
    checkpoint = output_dir / "run.json"
    if resume and not checkpoint.is_file():
        raise ValueError("--resume requires an existing run.json checkpoint")
    saved = json.loads(checkpoint.read_text(encoding="utf-8")) if resume else None
    if resume:
        _validate_resume(saved, settings, samples, output_dir)

    own_client = client is None
    http = client or httpx.Client(base_url=base_url, timeout=timeout, follow_redirects=False, trust_env=False)
    try:
        _confirm_reference(http, voice=voice, reference_id=reference_id,
                           admin_token=admin_token, expected_hash=references[0]["audio_sha256"])
        request = {
            "voice": voice, "model_id": model_id, "reference_id": reference_id,
            "response_format": "wav", "speed": speed,
        }
        initial = _resolved(
            http.post("/v1/audio/resolve", json={**request, "input": samples[0]["text"]}),
            voice=voice, reference_id=reference_id,
        )
        if saved is not None and saved["identity"] != initial:
            raise ValueError("resume backend/model/reference identity differs from checkpoint")
        if saved is None:
            output_dir.mkdir(parents=True, exist_ok=False)
            (output_dir / "audio").mkdir()
            saved = {
                "schema_version": SCHEMA_VERSION,
                "run_id": uuid.uuid4().hex,
                "settings": settings,
                "identity": initial,
                "started_at": _utc(),
                "status": "running",
                "quality_evaluation": "not_performed",
                "items": {item["id"]: {"status": "pending"} for item in samples},
            }
            _durable_json(checkpoint, saved)
        if saved["status"] == "complete":
            return saved

        saved["status"] = "running"
        _durable_json(checkpoint, saved)
        for item in samples:
            item_id = item["id"]
            if saved["items"][item_id]["status"] == "ok":
                continue
            try:
                identity = _resolved(http.post(
                    "/v1/audio/resolve", json={**request, "input": item["text"]}
                ), voice=voice, reference_id=reference_id)
                if identity != initial:
                    raise ValueError("serving identity changed during benchmark")
                start = time.perf_counter()
                response = http.post("/v1/audio/speech", json={**request, "input": item["text"]})
                elapsed = time.perf_counter() - start
                response.raise_for_status()
                request_id = _check_headers(response, initial)
                metrics = _assert_wav(response.content)
                target = output_dir / "audio" / f"{item_id}.wav"
                with target.open("xb") as stream:
                    stream.write(response.content)
                    stream.flush()
                    os.fsync(stream.fileno())
                saved["items"][item_id] = {
                    "status": "ok", "request_id": request_id,
                    "output_sha256": hashlib.sha256(response.content).hexdigest(),
                    "output_bytes": len(response.content),
                    "elapsed_seconds": round(elapsed, 6),
                    "realtime_factor": round(elapsed / metrics["duration_seconds"], 6),
                    **metrics,
                }
                _durable_json(checkpoint, saved)
            except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
                saved["items"][item_id] = {"status": "failed", "error": type(exc).__name__}
                saved["status"] = "partial"
                _durable_json(checkpoint, saved)
                raise RuntimeError(f"voicebench stopped at {item_id}: {type(exc).__name__}") from exc

        saved["status"] = "complete"
        saved["completed_at"] = _utc()
        _durable_json(checkpoint, saved)
        return saved
    finally:
        if own_client:
            http.close()
