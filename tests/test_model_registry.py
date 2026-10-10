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
        )

    model_registry.set_status("march7-gsv-v4-a", "validated", registry_path=registry)

    with pytest.raises(ValueError, match="promotable"):
        model_registry.promote_model(
            "march7-gsv-v4-a",
            registry_path=registry,
            evaluation_dir=evaluations,
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
