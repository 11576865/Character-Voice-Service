import hashlib
import json
from pathlib import Path

import pytest

from server import model_registry


def write_weight(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def make_manifest(model_root: Path, model_id: str = "march7-gsv-v4-a"):
    model_dir = model_root / "march-7th" / "gpt-sovits" / model_id
    gpt = model_dir / "artifacts" / "gpt.ckpt"
    sovits = model_dir / "artifacts" / "sovits.pth"
    write_weight(gpt, b"gpt")
    write_weight(sovits, b"sovits")
    manifest = {
        "schema_version": "1.0",
        "model_id": model_id,
        "voice_id": "march-7th",
        "name": "March 7th v4",
        "language": ["en"],
        "engine": {
            "name": "gpt-sovits",
            "engine_version": "v4",
            "adapter_api_version": "1",
        },
        "artifacts": {
            "gpt": {
                "path": "artifacts/gpt.ckpt",
                "sha256": hashlib.sha256(b"gpt").hexdigest(),
            },
            "sovits": {
                "path": "artifacts/sovits.pth",
                "sha256": hashlib.sha256(b"sovits").hexdigest(),
            },
        },
        "training": {"dataset_id": "march7-en-v1"},
        "runtime": {"precision": "fp16"},
        "capabilities": {"fine_tuned_model": True},
        "serving": {"parameters": {"top_k": 15}},
        "lifecycle": {"status": "candidate"},
    }
    path = model_dir / "model.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_scan_registers_immutable_manifest_and_resolves_artifacts(tmp_path):
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    make_manifest(model_root)

    report = model_registry.scan_model_root(model_root=model_root, registry_path=registry)

    assert report["discovered"] == 1
    resolved = model_registry.resolve_model(
        "march7-gsv-v4-a",
        model_root=model_root,
        registry_path=registry,
    )
    assert resolved["engine"]["name"] == "gpt-sovits"
    assert resolved["parameters"]["top_k"] == 15
    assert Path(resolved["artifacts"]["gpt"]).name == "gpt.ckpt"
    assert len(resolved["revision"]) == 64


def test_changed_manifest_is_quarantined_on_rescan(tmp_path):
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    manifest_path = make_manifest(model_root)
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["name"] = "Mutated after registration"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    report = model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    data = model_registry.load_registry(registry)

    assert report["invalid"]
    assert data["models"]["march7-gsv-v4-a"]["status"] == "quarantined"
    assert "integrity_error" in data["models"]["march7-gsv-v4-a"]


def test_promotion_requires_validated_status_and_promotable_evaluation(tmp_path):
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    evaluations = tmp_path / "evaluations"
    make_manifest(model_root)
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)

    with pytest.raises(ValueError, match="validated"):
        model_registry.promote_model(
            "march7-gsv-v4-a",
            registry_path=registry,
            evaluation_dir=evaluations,
            model_root=model_root,
        )

    model_registry.set_status("march7-gsv-v4-a", "validated", registry_path=registry)

    with pytest.raises(ValueError, match="promotable"):
        model_registry.promote_model(
            "march7-gsv-v4-a",
            registry_path=registry,
            evaluation_dir=evaluations,
            model_root=model_root,
        )

    # Legacy records are readable for history but cannot authorize promotion.
    evaluations.mkdir()
    (evaluations / "legacy.json").write_text(json.dumps({
        "schema_version": "1.0",
        "evaluation_id": "legacy",
        "model_id": "march7-gsv-v4-a",
        "model_sha256": "x",
        "decision": {"status": "validated", "promotable": True},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="provenance"):
        model_registry.promote_model(
            "march7-gsv-v4-a",
            registry_path=registry,
            evaluation_dir=evaluations,
            model_root=model_root,
        )
    assert model_registry.load_registry(registry)["defaults"] == {}


def test_migrate_profile_imports_legacy_weights_as_immutable_artifacts(tmp_path, monkeypatch):
    source_root = tmp_path / "GPT-SoVITS"
    gpt = source_root / "GPT_weights_v4" / "march.ckpt"
    sovits = source_root / "SoVITS_weights_v4" / "march.pth"
    write_weight(gpt, b"gpt")
    write_weight(sovits, b"sovits")

    profile = tmp_path / "march-7th.json"
    profile.write_text(json.dumps({
        "schema_version": 2,
        "name": "March 7th",
        "target_language": "en",
        "default_model": "local-v4",
        "models": {
            "local-v4": {
                "name": "Local v4",
                "engine": "gpt-sovits",
                "version": "v4",
                "gpt_weights": "C:/old/GPT_weights_v4/march.ckpt",
                "sovits_weights": "C:/old/SoVITS_weights_v4/march.pth",
            }
        },
        "default_reference": "default",
        "references": {
            "default": {
                "audio": "D:/ref.wav",
                "text": "Hello.",
                "language": "en",
            }
        },
    }), encoding="utf-8")

    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(model_registry, "_legacy_search_roots", lambda: [source_root])

    report = model_registry.migrate_profile(
        profile,
        model_root=model_root,
        registry_path=registry,
    )

    assert report["converted"] == 1
    migrated = json.loads(profile.read_text(encoding="utf-8"))
    entry = migrated["models"]["local-v4"]
    assert "model_id" in entry
    assert "gpt_weights" not in entry
    assert "sovits_weights" not in entry
    assert profile.with_suffix(".json.pre-model-registry.bak").exists()

    resolved = model_registry.resolve_model(
        entry["model_id"],
        model_root=model_root,
        registry_path=registry,
    )
    assert Path(resolved["artifacts"]["gpt"]).read_bytes() == b"gpt"
    assert Path(resolved["artifacts"]["sovits"]).read_bytes() == b"sovits"


def test_shared_v11_manifest_allows_external_runtime_without_local_artifacts(tmp_path):
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    model_dir = model_root / "_shared" / "index-tts" / "index-tts-2.5"
    model_dir.mkdir(parents=True)
    manifest = {
        "schema_version": "1.1",
        "scope": "shared",
        "model_id": "index-tts-2.5",
        "name": "IndexTTS 2.5",
        "language": ["en", "zh", "ja", "es", "ar"],
        "engine": {
            "name": "index-tts",
            "engine_version": "2.5",
            "adapter_api_version": "1",
        },
        "artifacts": {},
        "runtime": {"ownership": "external-sidecar"},
        "capabilities": {"shared_engine_model": True},
        "serving": {"parameters": {}},
        "lifecycle": {"status": "validated"},
    }
    path = model_dir / "model.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    report = model_registry.scan_model_root(
        model_root=model_root, registry_path=registry
    )
    resolved = model_registry.resolve_model(
        "index-tts-2.5", model_root=model_root, registry_path=registry
    )

    assert report["discovered"] == 1
    assert resolved["scope"] == "shared"
    assert resolved["voice_id"] is None
    assert resolved["artifacts"] == {}
    assert resolved["engine"]["name"] == "index-tts"

    listed = model_registry.list_models(
        model_root=model_root, registry_path=registry
    )
    assert listed[0]["scope"] == "shared"
    assert listed[0]["default_for_voice"] is False


def test_shared_model_cannot_be_promoted_as_voice_default(tmp_path):
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    model_dir = model_root / "_shared" / "index-tts" / "index-tts-2.5"
    model_dir.mkdir(parents=True)
    (model_dir / "model.json").write_text(json.dumps({
        "schema_version": "1.1",
        "scope": "shared",
        "model_id": "index-tts-2.5",
        "name": "IndexTTS 2.5",
        "engine": {
            "name": "index-tts",
            "engine_version": "2.5",
            "adapter_api_version": "1",
        },
        "artifacts": {},
        "lifecycle": {"status": "validated"},
    }), encoding="utf-8")
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)

    with pytest.raises(ValueError, match="shared models cannot be promoted"):
        model_registry.promote_model(
            "index-tts-2.5",
            registry_path=registry,
            require_evaluation=False,
        )


def test_promotion_override_still_validates_model_root_artifacts(tmp_path):
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    make_manifest(model_root)
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    model_registry.set_status("march7-gsv-v4-a", "validated", registry_path=registry)

    # An explicit evaluation override does not waive immutable artifact checks.
    artifact = model_root / "march-7th" / "gpt-sovits" / "march7-gsv-v4-a" / "artifacts" / "gpt.ckpt"
    artifact.write_bytes(b"tampered gpt weight")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        model_registry.promote_model(
            "march7-gsv-v4-a", registry_path=registry,
            model_root=model_root, require_evaluation=False,
        )
    assert model_registry.load_registry(registry)["defaults"] == {}


def test_promotion_override_rejects_mutated_immutable_manifest(tmp_path):
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    manifest_path = make_manifest(model_root)
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    model_registry.set_status("march7-gsv-v4-a", "validated", registry_path=registry)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = "silently modified"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable model manifest changed"):
        model_registry.promote_model(
            "march7-gsv-v4-a", registry_path=registry,
            model_root=model_root, require_evaluation=False,
        )
    assert model_registry.load_registry(registry)["defaults"] == {}


def test_promotion_rejects_disappeared_model_even_if_status_manually_validated(tmp_path):
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    manifest_path = make_manifest(model_root)
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    manifest_path.unlink()
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    assert model_registry.load_registry(registry)["models"]["march7-gsv-v4-a"]["present"] is False
    model_registry.set_status("march7-gsv-v4-a", "validated", registry_path=registry)

    with pytest.raises(ValueError, match="not present"):
        model_registry.promote_model(
            "march7-gsv-v4-a", registry_path=registry,
            model_root=model_root, require_evaluation=False,
        )
    assert model_registry.load_registry(registry)["defaults"] == {}


def test_promotion_rejects_integrity_error_even_if_status_changed(tmp_path):
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    manifest_path = make_manifest(model_root)
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = "violates immutable manifest"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    model_registry.set_status("march7-gsv-v4-a", "validated", registry_path=registry)

    with pytest.raises(ValueError, match="integrity error"):
        model_registry.promote_model(
            "march7-gsv-v4-a", registry_path=registry,
            model_root=model_root, require_evaluation=False,
        )


def test_promotion_override_accepts_valid_unchanged_registered_model(tmp_path):
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    make_manifest(model_root)
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    model_registry.set_status("march7-gsv-v4-a", "validated", registry_path=registry)
    model_registry.promote_model(
        "march7-gsv-v4-a", registry_path=registry,
        model_root=model_root, require_evaluation=False,
    )
    assert model_registry.load_registry(registry)["defaults"]["march-7th"] == "march7-gsv-v4-a"


def test_atomic_registry_snapshot_preserves_old_file_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "data" / "registry.json"
    before = {"schema_version": 1, "models": {"old": {"status": "candidate"}}, "defaults": {}}
    model_registry.save_registry(before, path)

    def injected_failure(source, destination):
        raise OSError("simulated failure before atomic replace")

    monkeypatch.setattr(model_registry.os, "replace", injected_failure)
    with pytest.raises(OSError, match="simulated failure"):
        model_registry.save_registry(
            {"schema_version": 1, "models": {"new": {}}, "defaults": {}}, path,
        )
    assert json.loads(path.read_text(encoding="utf-8")) == before
    assert list(path.parent.glob("registry.json.*.tmp")) == []


def test_successive_registry_writes_never_leave_staging_files(tmp_path):
    path = tmp_path / "registry.json"
    for number in range(4):
        model_registry.save_registry({"schema_version": 1, "models": {
            "model": {"revision": number},
        }, "defaults": {}}, path)
        assert model_registry.load_registry(path)["models"]["model"]["revision"] == number
    assert list(tmp_path.glob("registry.json.*.tmp")) == []


@pytest.mark.parametrize("different_manifest", [False, True])
def test_duplicate_model_ids_are_ambiguous_even_if_manifests_match(tmp_path, different_manifest):
    import shutil

    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    original_manifest = make_manifest(model_root)
    original_dir = original_manifest.parent
    second_dir = model_root / "another-voice" / "gpt-sovits" / "same-model-id"
    shutil.copytree(original_dir, second_dir)
    if different_manifest:
        duplicate = second_dir / "model.json"
        document = json.loads(duplicate.read_text(encoding="utf-8"))
        document["name"] = "same ID, different model manifest"
        duplicate.write_text(json.dumps(document), encoding="utf-8")

    report = model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    assert report["discovered"] == 0
    assert any("duplicate model_id" in error["error"] for error in report["invalid"])
    assert "march7-gsv-v4-a" not in model_registry.load_registry(registry)["models"]


def test_duplicate_models_quarantine_existing_default_and_disable_auto_selection(tmp_path):
    import shutil

    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    original_manifest = make_manifest(model_root)
    model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    model_registry.set_status("march7-gsv-v4-a", "validated", registry_path=registry)
    model_registry.promote_model(
        "march7-gsv-v4-a", registry_path=registry,
        model_root=model_root, require_evaluation=False,
    )
    assert model_registry.default_model_id("march-7th", registry_path=registry) == "march7-gsv-v4-a"
    duplicate_dir = model_root / "conflict" / "same-id"
    shutil.copytree(original_manifest.parent, duplicate_dir)

    report = model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    data = model_registry.load_registry(registry)
    assert report["invalid"]
    assert data["models"]["march7-gsv-v4-a"]["status"] == "quarantined"
    assert data["models"]["march7-gsv-v4-a"]["present"] is False
    assert data["defaults"]["march-7th"] == "march7-gsv-v4-a"  # retained for human inspection
    assert model_registry.default_model_id("march-7th", registry_path=registry) is None
    listed = model_registry.list_models(model_root=model_root, registry_path=registry)
    assert listed[0]["default_for_voice"] is False
    with pytest.raises(ValueError, match="not present"):
        model_registry.resolve_model("march7-gsv-v4-a", model_root=model_root, registry_path=registry)


def test_scan_reports_external_manifest_symlink_without_aborting(tmp_path):
    external_root = tmp_path / "external"
    external_manifest = make_manifest(external_root)
    model_root = tmp_path / "models"
    registry = tmp_path / "registry.json"
    link = model_root / "foreign" / "model.json"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(external_manifest)
    except (OSError, NotImplementedError):
        pytest.skip("manifest symlinks not supported by this test filesystem")

    report = model_registry.scan_model_root(model_root=model_root, registry_path=registry)
    assert report["discovered"] == 0
    assert report["invalid"]
    assert not model_registry.load_registry(registry)["models"]
