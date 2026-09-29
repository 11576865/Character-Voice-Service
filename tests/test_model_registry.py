import json
from pathlib import Path

from server import model_registry


def write_weight(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"weight")


def test_registered_model_relocates_and_rewrites_registry(tmp_path, monkeypatch):
    root = tmp_path / "GPT-SoVITS"
    gpt = root / "GPT_weights_v4" / "march.ckpt"
    sovits = root / "SoVITS_weights_v4" / "march.pth"
    write_weight(gpt)
    write_weight(sovits)

    registry = tmp_path / "model-registry.json"
    registry.write_text(json.dumps({
        "schema_version": 1,
        "models": {
            "march-v4": {
                "gpt_weights": "C:/old/GPT_weights_v4/march.ckpt",
                "sovits_weights": "C:/old/SoVITS_weights_v4/march.pth",
            }
        },
    }), encoding="utf-8")

    monkeypatch.setattr(model_registry, "_model_roots", lambda: [root])

    resolved = model_registry.resolve_registered_model("march-v4", registry_path=registry)
    assert Path(resolved["gpt_weights"]) == gpt.resolve()
    assert Path(resolved["sovits_weights"]) == sovits.resolve()

    persisted = json.loads(registry.read_text(encoding="utf-8"))
    assert Path(persisted["models"]["march-v4"]["gpt_weights"]) == gpt.resolve()
    assert Path(persisted["models"]["march-v4"]["sovits_weights"]) == sovits.resolve()


def test_migrate_profile_moves_paths_into_registry(tmp_path, monkeypatch):
    root = tmp_path / "GPT-SoVITS"
    gpt = root / "GPT_weights_v4" / "march.ckpt"
    sovits = root / "SoVITS_weights_v4" / "march.pth"
    write_weight(gpt)
    write_weight(sovits)

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
                "gpt_weights": str(gpt),
                "sovits_weights": str(sovits),
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
    registry = tmp_path / "model-registry.json"
    monkeypatch.setattr(model_registry, "_model_roots", lambda: [root])

    report = model_registry.migrate_profile(profile, registry_path=registry)

    assert report["converted"] == 1
    migrated = json.loads(profile.read_text(encoding="utf-8"))
    model = migrated["models"]["local-v4"]
    assert model["model_ref"] == "march-7th--local-v4"
    assert "gpt_weights" not in model
    assert "sovits_weights" not in model
    assert profile.with_suffix(".json.pre-model-registry.bak").exists()

    resolved = model_registry.resolve_registered_model(model["model_ref"], registry_path=registry)
    assert Path(resolved["gpt_weights"]) == gpt.resolve()
