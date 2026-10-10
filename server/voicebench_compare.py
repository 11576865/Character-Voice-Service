"""Offline, fail-closed comparison of two completed CVS voicebench runs.

This is a generation-path integrity audit, NOT an audio-quality ranking.
No ASR, MOS, speaker-embedding or human-listening measurements are inferred.
"""
import hashlib
import json
import math
import os
import statistics
import uuid
from pathlib import Path

from server.benchmark_dataset import verify_dataset
from server.voicebench import _assert_wav, _sha

REPORT_SCHEMA = "1.0"


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")
    if not positive and value < 0:
        raise ValueError(f"{label} cannot be negative")
    return float(value)


def _load_run(directory: Path, *, dataset: dict, test_ids: list[str]) -> dict:
    directory = Path(directory)
    checkpoint = directory / "run.json"
    audio_dir = directory / "audio"
    if checkpoint.is_symlink() or audio_dir.is_symlink() or not audio_dir.is_dir():
        raise ValueError("run checkpoint and audio directory must be real local files")
    record = json.loads(checkpoint.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError("unsupported voicebench run schema")
    if record.get("status") != "complete":
        raise ValueError("only complete voicebench runs can be compared")
    if record.get("quality_evaluation") != "not_performed":
        raise ValueError("unsupported run quality-evaluation state")
    settings, identity, items = record.get("settings"), record.get("identity"), record.get("items")
    if not isinstance(settings, dict) or not isinstance(identity, dict) or not isinstance(items, dict):
        raise ValueError("run provenance or item records are missing")
    if (settings.get("dataset_id") != dataset["dataset_id"]
            or settings.get("dataset_sha256") != dataset["dataset_sha256"]):
        raise ValueError("run dataset fingerprint does not match reverified frozen source")
    if set(items) != set(test_ids):
        raise ValueError("run results do not exactly cover the frozen test set")
    if (identity.get("voice") != settings.get("voice")
            or identity.get("reference") != settings.get("reference_id")):
        raise ValueError("run serving voice/reference identity mismatch")
    for field in ("model_revision", "generation_revision"):
        _sha(identity.get(field), field)
    for field in ("model", "engine", "reference", "voice"):
        if not isinstance(identity.get(field), str) or not identity[field]:
            raise ValueError(f"run identity missing: {field}")
    _sha(settings.get("reference_audio_sha256"), "reference_audio_sha256")
    if not isinstance(record.get("run_id"), str) or not record["run_id"]:
        raise ValueError("run_id missing")

    frozen_refs = {
        row["id"]: row for row in dataset["items"]
        if row["split"] != "test-recorded" and row.get("reference") is True
    }
    ref = frozen_refs.get(settings.get("reference_item_id"))
    if not ref or ref.get("audio_sha256") != settings["reference_audio_sha256"]:
        raise ValueError("reference identity differs from frozen reference pool")

    speed = _number(settings.get("speed"), "speed", positive=True)
    auditable = {}
    for item_id in test_ids:
        row = items[item_id]
        if not isinstance(row, dict) or row.get("status") != "ok":
            raise ValueError(f"incomplete voicebench item: {item_id}")
        artifact = audio_dir / f"{item_id}.wav"
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError(f"missing or linked output audio: {item_id}")
        if artifact.with_name(artifact.name + ".part").exists():
            raise ValueError(f"unpublished staged audio exists: {item_id}")
        data = artifact.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        _sha(row.get("output_sha256"), "output_sha256")
        if (row["output_sha256"] != digest
                or type(row.get("output_bytes")) is not int
                or row["output_bytes"] != len(data)):
            raise ValueError(f"output WAV hash or size mismatch: {item_id}")
        meta = _assert_wav(data)
        for field in ("frames", "channels", "sample_rate"):
            if type(row.get(field)) is not int or row[field] != meta[field]:
                raise ValueError(f"output WAV metadata mismatch: {item_id}/{field}")
        if not math.isclose(_number(row.get("duration_seconds"), "duration_seconds", positive=True),
                            meta["duration_seconds"], abs_tol=1e-6, rel_tol=0):
            raise ValueError(f"output WAV duration mismatch: {item_id}")
        elapsed = _number(row.get("elapsed_seconds"), "elapsed_seconds")
        rtf = _number(row.get("realtime_factor"), "realtime_factor")
        # The runner rounds both elapsed and RTF to six decimals. Bound
        # roundoff using the waveform duration rather than a fixed tolerance.
        tolerance = 0.0000005 + 0.0000005 / meta["duration_seconds"]
        if not math.isclose(rtf, elapsed / meta["duration_seconds"], rel_tol=0, abs_tol=tolerance):
            raise ValueError(f"output WAV realtime factor mismatch: {item_id}")
        if not isinstance(row.get("request_id"), str) or not row["request_id"]:
            raise ValueError(f"missing request ID: {item_id}")
        auditable[item_id] = {
            "output_sha256": digest,
            "request_id": row["request_id"],
            "elapsed_seconds": elapsed,
            "duration_seconds": meta["duration_seconds"],
            "realtime_factor": rtf,
        }

    total_elapsed = sum(row["elapsed_seconds"] for row in auditable.values())
    total_audio = sum(row["duration_seconds"] for row in auditable.values())
    return {
        "run_id": record["run_id"],
        "voice": identity["voice"], "model": identity["model"],
        "engine": identity["engine"],
        "model_revision": identity["model_revision"],
        "generation_revision": identity["generation_revision"],
        "reference_id": identity["reference"],
        "reference_item_id": settings["reference_item_id"],
        "reference_audio_sha256": settings["reference_audio_sha256"],
        "speed": speed,
        "items": auditable,
        "summary": {
            "item_count": len(test_ids),
            "total_elapsed_seconds": round(total_elapsed, 6),
            "total_generated_seconds": round(total_audio, 6),
            "aggregate_rtf": round(total_elapsed / total_audio, 6),
            "median_item_rtf": round(statistics.median(
                row["realtime_factor"] for row in auditable.values()), 6),
        },
    }


def compare_runs(
    *, manifest_path: Path, audio_root: Path, first_run: Path,
    second_run: Path, output: Path,
) -> dict:
    """Generate a create-only, content-addressed descriptive performance audit."""
    if Path(first_run).resolve() == Path(second_run).resolve():
        raise ValueError("A and B must use different run directories")
    checked = verify_dataset(manifest_path, audio_root)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("dataset_id") != checked["dataset_id"] or manifest.get("dataset_sha256") != checked["dataset_sha256"]:
        raise ValueError("frozen manifest changed during verification")
    fingerprint_payload = {
        "schema_version": manifest.get("schema_version"),
        "dataset_id": manifest["dataset_id"],
        "items": manifest.get("items"),
    }
    if hashlib.sha256(_canonical(fingerprint_payload)).hexdigest() != checked["dataset_sha256"]:
        raise ValueError("frozen manifest contents changed after source verification")
    test_ids = sorted(row["id"] for row in manifest["items"] if row["split"] == "test-recorded")
    if not test_ids:
        raise ValueError("no frozen test-recorded samples")
    a = _load_run(first_run, dataset=manifest, test_ids=test_ids)
    b = _load_run(second_run, dataset=manifest, test_ids=test_ids)
    if a["run_id"] == b["run_id"]:
        raise ValueError("A and B have the same run identity")
    for field in ("voice", "reference_item_id", "reference_audio_sha256", "speed"):
        if a[field] != b[field]:
            raise ValueError(f"runs do not share a controlled {field}")
    rows = []
    for item_id in test_ids:
        left, right = a["items"][item_id], b["items"][item_id]
        rows.append({
            "item_id": item_id,
            "A": left,
            "B": right,
            "identical_output_bytes": left["output_sha256"] == right["output_sha256"],
        })
    # Canonical fingerprint excludes an invocation timestamp for reproducibility.
    payload = {
        "schema_version": REPORT_SCHEMA,
        "kind": "generation_path_observational_comparison",
        "quality_evaluation": "not_performed",
        "dataset": {
            "dataset_id": checked["dataset_id"],
            "dataset_sha256": checked["dataset_sha256"],
            "test_item_ids": test_ids,
        },
        "runs": {"A": {key: value for key, value in a.items() if key != "items"},
                 "B": {key: value for key, value in b.items() if key != "items"}},
        "paired_items": rows,
        "limitations": [
            "No WER, speaker similarity, MOS, blind listening or quality ranking",
            "Runtime hardware, thermal state, concurrency and warmup are not controlled",
            "Run manifest JSON is not signed or independently attested",
        ],
    }
    report = {**payload, "report_sha256": hashlib.sha256(_canonical(payload)).hexdigest()}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise ValueError(f"report already exists: {output}") from exc
    return report
