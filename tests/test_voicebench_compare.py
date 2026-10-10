"""Offline comparison tests exercise real frozen corpus and generated WAV bytes."""
import hashlib
import json
import wave
from io import BytesIO

import pytest

from server.benchmark_dataset import freeze_dataset
from server.voicebench_compare import compare_runs


def wav(value):
    out = BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(bytes([value, 0]) * 800)
    return out.getvalue()


def dataset(tmp_path):
    root = tmp_path / "originals"
    root.mkdir()
    rows = []
    for idx, split, ref, text in [
        ("ref", "train", True, "Neutral prompt"),
        ("test-one", "test-recorded", False, "Test one"),
        ("test-two", "test-recorded", False, "Test two"),
    ]:
        (root / f"{idx}.wav").write_bytes(wav({"ref": 1, "test-one": 2, "test-two": 3}[idx]))
        rows.append({
            "id": idx, "audio": f"{idx}.wav", "text": text, "language": "en",
            "source": "ORIGINAL", "split": split, "reference": ref,
        })
    source = tmp_path / "curated.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    manifest = tmp_path / "benchmarks" / "data-v1.json"
    frozen = freeze_dataset(source, root, manifest, "data-v1")
    return root, manifest, frozen


def run(tmp_path, frozen, *, name, engine, value):
    folder = tmp_path / name
    audio_dir = folder / "audio"
    audio_dir.mkdir(parents=True)
    records = {}
    for index, item_id in enumerate(("test-one", "test-two")):
        content = wav(value + index)
        (audio_dir / f"{item_id}.wav").write_bytes(content)
        elapsed = 0.2 + index * 0.1
        records[item_id] = {
            "status": "ok", "request_id": name + "-" + item_id,
            "output_sha256": hashlib.sha256(content).hexdigest(),
            "output_bytes": len(content),
            "frames": 800, "channels": 1, "sample_rate": 8000,
            "duration_seconds": 0.1,
            "elapsed_seconds": round(elapsed, 6),
            "realtime_factor": round(elapsed / 0.1, 6),
        }
    data = {
        "schema_version": 1, "run_id": name,
        "status": "complete", "quality_evaluation": "not_performed",
        "settings": {
            "dataset_id": "data-v1", "dataset_sha256": frozen["dataset_sha256"],
            "voice": "march-7th", "requested_model_id": "model-" + engine,
            "reference_id": "neutral", "reference_item_id": "ref",
            "reference_audio_sha256": hashlib.sha256(wav(1)).hexdigest(),
            "speed": 1.0,
        },
        "identity": {
            "voice": "march-7th", "reference": "neutral",
            "engine": engine, "model": "model-" + engine,
            "model_revision": "a" * 64 if engine == "gpt-sovits" else "b" * 64,
            "generation_revision": "c" * 64 if engine == "gpt-sovits" else "d" * 64,
        },
        "items": records,
    }
    (folder / "run.json").write_text(json.dumps(data), encoding="utf-8")
    return folder


def compare(tmp_path, root, manifest, a, b, name="comparison.json"):
    return compare_runs(
        manifest_path=manifest, audio_root=root,
        first_run=a, second_run=b, output=tmp_path / name,
    )


def mutate_run(folder, fn):
    path = folder / "run.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    fn(record)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_complete_a_b_report_audits_audio_and_has_no_quality_score(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    result = compare(tmp_path, root, manifest, a, b)
    assert result["quality_evaluation"] == "not_performed"
    assert result["kind"] == "generation_path_observational_comparison"
    assert result["dataset"]["dataset_sha256"] == frozen["dataset_sha256"]
    assert [x["item_id"] for x in result["paired_items"]] == ["test-one", "test-two"]
    assert result["runs"]["A"]["summary"]["aggregate_rtf"] == 2.5
    assert result["runs"]["B"]["summary"]["median_item_rtf"] == 2.5
    assert "wer" not in json.dumps(result).lower()
    # For identical inputs the content fingerprint does not depend on report output path.
    again = compare(tmp_path, root, manifest, a, b, "comparison-2.json")
    assert again["report_sha256"] == result["report_sha256"]


def test_refuses_changed_generated_audio(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    (a / "audio" / "test-one.wav").write_bytes(wav(21))
    with pytest.raises(ValueError, match="hash or size"):
        compare(tmp_path, root, manifest, a, b)


def test_refuses_modified_original_source(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    (root / "test-two.wav").write_bytes(wav(30))
    with pytest.raises(ValueError, match="changed"):
        compare(tmp_path, root, manifest, a, b)


def test_refuses_incomplete_run_even_with_audio(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    mutate_run(b, lambda rec: rec.update({"status": "partial"}))
    with pytest.raises(ValueError, match="complete"):
        compare(tmp_path, root, manifest, a, b)


def test_refuses_missing_held_out_item(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    mutate_run(b, lambda rec: rec["items"].pop("test-two"))
    with pytest.raises(ValueError, match="exactly cover"):
        compare(tmp_path, root, manifest, a, b)


def test_refuses_controlled_reference_changes(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    mutate_run(b, lambda rec: rec["settings"].update({"reference_audio_sha256": "f" * 64}))
    with pytest.raises(ValueError, match="reference identity"):
        compare(tmp_path, root, manifest, a, b)


def test_refuses_different_speed_settings(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    mutate_run(b, lambda rec: rec["settings"].update({"speed": 1.2}))
    with pytest.raises(ValueError, match="controlled speed"):
        compare(tmp_path, root, manifest, a, b)


@pytest.mark.parametrize("field,value", [
    ("realtime_factor", 300.0),
    ("elapsed_seconds", -1.0),
    ("duration_seconds", 2.0),
    ("output_bytes", True),
])
def test_refuses_invalid_recorded_metrics(tmp_path, field, value):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    mutate_run(a, lambda rec: rec["items"]["test-one"].update({field: value}))
    with pytest.raises(ValueError):
        compare(tmp_path, root, manifest, a, b)


def test_refuses_identity_mismatch(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    mutate_run(b, lambda rec: rec["identity"].update({"voice": "other-voice"}))
    with pytest.raises(ValueError, match="identity mismatch"):
        compare(tmp_path, root, manifest, a, b)


def test_report_never_overwrites_existing_evidence(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    compare(tmp_path, root, manifest, a, b)
    with pytest.raises(ValueError, match="already exists"):
        compare(tmp_path, root, manifest, a, b)


def test_unpublished_pending_wav_refuses_comparison(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    (a / "audio" / "test-one.wav.part").write_bytes(b"incomplete")
    with pytest.raises(ValueError, match="unpublished"):
        compare(tmp_path, root, manifest, a, b)


def test_refuses_same_run_identity(tmp_path):
    root, manifest, frozen = dataset(tmp_path)
    a = run(tmp_path, frozen, name="run-a", engine="gpt-sovits", value=4)
    b = run(tmp_path, frozen, name="run-b", engine="index-tts", value=6)
    mutate_run(b, lambda rec: rec.update({"run_id": "run-a"}))
    with pytest.raises(ValueError, match="same run identity"):
        compare(tmp_path, root, manifest, a, b)
