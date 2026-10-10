import hashlib
import json
import wave

import pytest

from server import evaluation_registry, model_registry
from server.benchmark_dataset import freeze_dataset


def _wav(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(bytes([value, 0]) * 800)


def _dataset(tmp_path):
    audio = tmp_path / "originals"
    _wav(audio / "speaker.wav", 1)
    _wav(audio / "test-a.wav", 2)
    _wav(audio / "test-b.wav", 3)
    rows = [
        {"id": "speaker", "audio": "speaker.wav", "source": "ORIGINAL",
         "text": "Speaker recording", "language": "en", "split": "train", "reference": True},
        {"id": "test-a", "audio": "test-a.wav", "source": "ORIGINAL",
         "text": "First test", "language": "en", "split": "test-recorded"},
        {"id": "test-b", "audio": "test-b.wav", "source": "ORIGINAL",
         "text": "Second test", "language": "en", "split": "test-recorded"},
    ]
    source = tmp_path / "curated.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    benchmarks = tmp_path / "benchmarks"
    manifest = benchmarks / "march7-en-v1.json"
    report = freeze_dataset(source, audio, manifest, "march7-en-v1")
    return benchmarks, manifest, report


def _evaluation(report, revision="a" * 64, eval_id="eval-1"):
    return {
        "evaluation_id": eval_id,
        "model_id": "march7-gsv-v4-a",
        "model_revision": revision,
        "generation_revision": "f" * 64,
        "dataset": {
            "dataset_id": "march7-en-v1",
            "dataset_sha256": report["dataset_sha256"],
            "test_item_ids": ["test-a", "test-b"],
            "references": [{"reference_id": "reference-default", "item_id": "speaker"}],
        },
        "decision": {"status": "validated", "promotable": True},
    }


def _promotable(record, tmp_path, benchmarks, *, revision="a" * 64):
    folder = tmp_path / "evaluations"
    evaluation_registry.write_evaluation(record, directory=folder)
    return evaluation_registry.model_is_promotable(
        "march7-gsv-v4-a", model_revision=revision,
        directory=folder, benchmark_dir=benchmarks,
    )


def test_valid_frozen_provenance_supports_promotion_gate(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    assert _promotable(_evaluation(report), tmp_path, benchmarks)


def test_evaluation_write_is_idempotent_and_preserves_creation_time(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    folder = tmp_path / "evaluations"
    record = _evaluation(report)
    path = evaluation_registry.write_evaluation(record, directory=folder)
    before = path.read_bytes()
    assert evaluation_registry.write_evaluation(record, directory=folder) == path
    assert path.read_bytes() == before
    modified = _evaluation(report)
    modified["dataset"]["references"][0]["reference_id"] = "changed"
    with pytest.raises(ValueError, match="already exists"):
        evaluation_registry.write_evaluation(modified, directory=folder)


@pytest.mark.parametrize("revision", [None, "", "b" * 64, "not-a-digest"])
def test_missing_or_wrong_model_revision_never_promotes(tmp_path, revision):
    benchmarks, _, report = _dataset(tmp_path)
    assert not _promotable(_evaluation(report), tmp_path, benchmarks, revision=revision)


def test_modified_dataset_manifest_invalidates_promotion(tmp_path):
    benchmarks, manifest, report = _dataset(tmp_path)
    record = _evaluation(report)
    assert _promotable(record, tmp_path, benchmarks)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["items"][0]["text"] = "Different"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    assert not evaluation_registry.model_is_promotable(
        "march7-gsv-v4-a", model_revision="a" * 64,
        directory=tmp_path / "evaluations", benchmark_dir=benchmarks,
    )


def test_missing_manifest_never_promotes(tmp_path):
    benchmarks, manifest, report = _dataset(tmp_path)
    manifest.unlink()
    assert not _promotable(_evaluation(report), tmp_path, benchmarks)


def test_incomplete_test_set_never_promotes(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    record = _evaluation(report)
    record["dataset"]["test_item_ids"] = ["test-a"]
    assert not _promotable(record, tmp_path, benchmarks)


def test_reference_bound_to_test_recording_never_promotes(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    record = _evaluation(report)
    record["dataset"]["references"][0]["item_id"] = "test-a"
    assert not _promotable(record, tmp_path, benchmarks)


def test_reference_bound_to_unknown_sample_never_promotes(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    record = _evaluation(report)
    record["dataset"]["references"][0]["item_id"] = "missing"
    assert not _promotable(record, tmp_path, benchmarks)


def test_forged_dataset_digest_never_promotes(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    record = _evaluation(report)
    record["dataset"]["dataset_sha256"] = "b" * 64
    assert not _promotable(record, tmp_path, benchmarks)


def test_decision_boolean_strings_are_not_trusted(tmp_path):
    _, _, report = _dataset(tmp_path)
    record = _evaluation(report)
    record["decision"]["promotable"] = "true"
    with pytest.raises(ValueError, match="boolean"):
        evaluation_registry.write_evaluation(record, directory=tmp_path / "evaluations")


def test_legacy_v10_records_can_be_read_but_never_promote(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    folder = tmp_path / "evaluations"
    folder.mkdir()
    legacy = {"schema_version": "1.0", "evaluation_id": "legacy", "model_id": "march7-gsv-v4-a",
              "model_sha256": "x", "decision": {"status": "validated", "promotable": True}}
    (folder / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
    assert len(evaluation_registry.list_evaluations("march7-gsv-v4-a", directory=folder)) == 1
    assert not evaluation_registry.model_is_promotable(
        "march7-gsv-v4-a", model_revision="a" * 64,
        directory=folder, benchmark_dir=benchmarks)
    with pytest.raises(ValueError, match="1.1"):
        evaluation_registry.write_evaluation(legacy, directory=folder)


@pytest.mark.parametrize("unsafe", ["../overwrite", "a/b", "x\\y", ""])
def test_evaluation_id_cannot_escape_evaluation_store(tmp_path, unsafe):
    _, _, report = _dataset(tmp_path)
    record = _evaluation(report, eval_id=unsafe)
    with pytest.raises(ValueError, match="evaluation_id"):
        evaluation_registry.write_evaluation(record, directory=tmp_path / "evaluations")


def test_cannot_promote_when_decision_is_pending(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    record = _evaluation(report)
    record["decision"] = {"status": "pending", "promotable": False}
    assert not _promotable(record, tmp_path, benchmarks)


def test_real_model_registry_promotion_requires_matching_revision(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    model_root = tmp_path / "models"
    registry = tmp_path / "model-registry.json"
    model_dir = model_root / "march-7th" / "gpt-sovits" / "march7-gsv-v4-a"
    (model_dir / "artifacts").mkdir(parents=True)
    artifacts = {}
    for role, filename in (("gpt", "gpt.ckpt"), ("sovits", "sovits.pth")):
        raw = role.encode("utf-8")
        (model_dir / "artifacts" / filename).write_bytes(raw)
        artifacts[role] = {"path": "artifacts/" + filename,
                           "sha256": hashlib.sha256(raw).hexdigest()}
    manifest = {"schema_version": "1.0", "model_id": "march7-gsv-v4-a",
                "voice_id": "march-7th", "name": "March v4",
                "engine": {"name": "gpt-sovits", "engine_version": "v4"},
                "artifacts": artifacts, "lifecycle": {"status": "candidate"}}
    (model_dir / "model.json").write_text(json.dumps(manifest), encoding="utf-8")
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    actual_revision = model_registry.load_registry(registry)["models"]["march7-gsv-v4-a"]["revision"]
    model_registry.set_status("march7-gsv-v4-a", "validated", registry_path=registry)
    evaluations = tmp_path / "evaluations"
    evaluation_registry.write_evaluation(_evaluation(report), directory=evaluations)
    with pytest.raises(ValueError, match="provenance"):
        model_registry.promote_model("march7-gsv-v4-a", registry_path=registry,
                                     evaluation_dir=evaluations, benchmark_dir=benchmarks, model_root=model_root)
    evaluation_registry.write_evaluation(_evaluation(report, revision=actual_revision, eval_id="correct"),
                                         directory=evaluations)
    model_registry.promote_model("march7-gsv-v4-a", registry_path=registry,
                                 evaluation_dir=evaluations, benchmark_dir=benchmarks, model_root=model_root)
    assert model_registry.load_registry(registry)["defaults"]["march-7th"] == "march7-gsv-v4-a"


def test_evaluation_v11_cannot_share_generation_revision_between_references(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    record = _evaluation(report)
    record["dataset"]["references"].append({
        "reference_id": "other-ref", "item_id": "speaker",
    })
    with pytest.raises(ValueError, match="exactly one"):
        evaluation_registry.write_evaluation(record, directory=tmp_path / "evaluations")


def test_forged_multi_reference_evaluation_never_promotes(tmp_path):
    benchmarks, _, report = _dataset(tmp_path)
    record = _evaluation(report)
    record["schema_version"] = "1.1"
    record["dataset"]["references"].append({
        "reference_id": "other-ref", "item_id": "speaker",
    })
    evaluations = tmp_path / "evaluations"
    evaluations.mkdir()
    (evaluations / "forged.json").write_text(json.dumps(record), encoding="utf-8")
    assert not evaluation_registry.model_is_promotable(
        "march7-gsv-v4-a", model_revision="a" * 64,
        directory=evaluations, benchmark_dir=benchmarks,
    )
