import json
import shutil

import pytest

from server.model_scan import contained_file, read_roots, scan


def fixture_tree(tmp_path):
    root = tmp_path / "models"
    voices = tmp_path / "voices"
    voices.mkdir()
    gpt = root / "GPT_weights_v4" / "角色(女)_EN-e10.ckpt"
    sovits = root / "SoVITS_weights_v4" / "角色(女)_EN_e8_s99.pth"
    for path in (gpt, sovits):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.suffix.encode())
    profile = {"models": {"old-id": {"gpt_weights": str(gpt), "sovits_weights": str(sovits)}}}
    (voices / "test-role.json").write_text(json.dumps(profile), encoding="utf-8")
    return root, voices, gpt, sovits


def roots(root):
    return [{"id": "test-root", "engine": "gpt-sovits", "path": str(root)}]


def test_read_only_scan_and_content_identity_survive_move(tmp_path):
    root, voices, gpt, sovits = fixture_tree(tmp_path)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    metadata = scan(roots(root), voices)
    assert metadata["models"][0]["revision_id"] is None
    assert metadata["profile_links"][0]["status"] == "registered"
    full = scan(roots(root), voices, full_hash=True)
    revision = full["models"][0]["revision_id"]
    assert revision.startswith("rev-")
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    moved = tmp_path / "moved"
    shutil.copytree(root, moved)
    assert scan(roots(moved), voices, full_hash=True)["models"][0]["revision_id"] == revision
    gpt.write_bytes(b"new content")
    assert scan(roots(root), voices, full_hash=True)["models"][0]["revision_id"] != revision


def test_multiple_epochs_require_confirmation_but_preserve_explicit_pairs(tmp_path):
    root, voices, gpt, sovits = fixture_tree(tmp_path)
    (gpt.parent / "角色(女)_EN-e20.ckpt").write_bytes(b"another epoch")
    report = scan(roots(root), voices, full_hash=True)
    assert report["models"][0]["status"] == "ambiguous"
    assert report["models"][0]["revision_id"] is None
    assert report["profile_links"][0]["status"] == "registered_explicit_pair"
    assert report["profile_links"][0]["revision_id"]
    sovits.unlink()
    report = scan(roots(root), voices)
    assert report["models"][0]["status"] == "incomplete"
    assert report["profile_links"][0]["status"] == "missing"


def test_invalid_extra_weight_cannot_silently_make_a_unique_pair(tmp_path):
    root, voices, gpt, sovits = fixture_tree(tmp_path)
    (gpt.parent / "角色(女)_EN-e20.ckpt").write_bytes(b"")
    (voices / "broken.json").write_text("invalid", encoding="utf-8")
    report = scan(roots(root), voices)
    assert report["models"][0]["status"] == "invalid"
    assert report["summary"]["issues"] == 2
    assert scan(roots(tmp_path / "offline"), voices)["roots"][0]["status"] == "root_unavailable"
    with pytest.raises(ValueError):
        contained_file(root, "../voices/test-role.json")


def test_config_validation_and_legacy_profiles(tmp_path):
    root, voices, gpt, sovits = fixture_tree(tmp_path)
    config = tmp_path / "roots.json"
    config.write_text(json.dumps({"schema_version": 1, "roots": roots(root)}), encoding="utf-8-sig")
    assert read_roots(config) == roots(root)
    config.write_text(json.dumps({"schema_version": 1, "roots": roots(root) * 2}), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        read_roots(config)
    (voices / "legacy.json").write_text('{"reference_audio": "old.wav"}', encoding="utf-8")
    report = scan(roots(root), voices)
    assert next(item for item in report["profile_links"] if item["character_id"] == "legacy")["status"] == "unmanaged"


def test_export_manifest_assigns_new_training_line_without_filename_guessing(tmp_path):
    root = tmp_path / "models"
    voices = tmp_path / "voices"
    voices.mkdir()
    gpt = root / "outputs" / "model-final.bin"
    sovits = root / "outputs" / "voice-final.data"
    for path, value in ((gpt, b"gpt"), (sovits, b"sovits")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    manifest = {"schema_version": 1, "engine": "gpt-sovits", "character_id": "march-7th",
                "model_id": "trained-en", "target_language": "en", "layout": "v-next",
                "name": "March 7th next", "artifacts": {
                    "gpt": "outputs/model-final.bin", "sovits": "outputs/voice-final.data"}}
    (root / "march-next.cvs-model.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = scan(roots(root), voices, full_hash=True)
    assert report["summary"]["paired"] == 1
    assert report["models"][0]["association"] == "declared"
    assert report["models"][0]["declared_assignment"]["character_id"] == "march-7th"
    assert report["models"][0]["revision_id"].startswith("rev-")
