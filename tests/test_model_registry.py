import json

import pytest

from server.model_management import ModelManager
from server.model_registry import ModelRegistry
from server.model_scan import scan


def setup_manager(tmp_path):
    root = tmp_path / "models"
    voices = tmp_path / "voices"
    data = tmp_path / "data"
    voices.mkdir()
    gpt = root / "GPT_weights_v4" / "Role-e10.ckpt"
    sovits = root / "SoVITS_weights_v4" / "Role_e10_s20.pth"
    for path, content in ((gpt, b"gpt-old"), (sovits, b"sovits-old")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    profile = {"schema_version": 2, "name": "Role", "target_language": "en",
               "default_model": "trained", "models": {"trained": {
                   "name": "Old", "engine": "gpt-sovits", "version": "v4",
                   "gpt_weights": str(gpt), "sovits_weights": str(sovits)}},
               "default_reference": "ref", "references": {"ref": {
                   "audio": "unused.wav", "text": "hello", "language": "en"}}}
    (voices / "role.json").write_text(json.dumps(profile), encoding="utf-8")
    data.mkdir()
    roots = {"schema_version": 1, "roots": [{"id": "root", "engine": "gpt-sovits", "path": str(root)}]}
    (data / "roots.json").write_text(json.dumps(roots), encoding="utf-8")
    return ModelManager(data, voices), root, voices, gpt, sovits


def test_scan_import_bootstraps_default_and_new_content_becomes_candidate(tmp_path):
    manager, root, voices, gpt, sovits = setup_manager(tmp_path)
    first = manager.scan(full_hash=True, register=True)["registration"]
    old_id = first["added"][0]
    assert first["registry"]["revisions"][old_id]["lifecycle"] == "default"
    assert first["registry"]["defaults"] == {"role:en": old_id}
    assert manager.scan(full_hash=True, register=True)["registration"]["existing"] == [old_id]
    gpt.write_bytes(b"gpt-next")
    second = manager.scan(full_hash=True, register=True)["registration"]
    new_id = second["added"][0]
    candidate = second["registry"]["revisions"][new_id]
    assert candidate["lifecycle"] == "candidate"
    assert candidate["character_id"] == "role"
    assert second["registry"]["defaults"]["role:en"] == old_id
    with pytest.raises(ValueError, match="content changed"):
        manager.promote(old_id, reason="rollback", allow_without_evaluation=True)


def test_assignment_lifecycle_profile_activation_and_audit(tmp_path):
    manager, root, voices, gpt, sovits = setup_manager(tmp_path)
    old_id = manager.scan(full_hash=True, register=True)["registration"]["added"][0]
    new_gpt = root / "GPT_weights_v4" / "Other-e10.ckpt"
    new_sovits = root / "SoVITS_weights_v4" / "Other_e10_s20.pth"
    new_gpt.write_bytes(b"new gpt")
    new_sovits.write_bytes(b"new sovits")
    report = scan(json.loads((manager.root / "roots.json").read_text())["roots"], voices, full_hash=True)
    registration = manager.registry.import_scan(report)
    candidate_id = registration["added"][0]
    assert registration["registry"]["revisions"][candidate_id]["character_id"] is None
    manager.assign(candidate_id, "role", "next", "en", "new training line")
    with pytest.raises(ValueError, match="Evaluation"):
        manager.promote(candidate_id, reason="not reviewed")
    result = manager.promote(candidate_id, reason="manual acceptance", allow_without_evaluation=True)
    assert result["production_applied"] is True
    registry = manager.registry.read()
    assert registry["revisions"][candidate_id]["lifecycle"] == "default"
    assert registry["revisions"][old_id]["lifecycle"] == "retired"
    profile = json.loads((voices / "role.json").read_text(encoding="utf-8"))
    assert profile["default_model"] == result["profile_model_id"]
    assert profile["models"][profile["default_model"]]["revision_id"] == candidate_id
    assert [event["action"] for event in registry["events"]] == [
        "bootstrap_default", "register_candidate", "assign", "promote"]
    with pytest.raises(ValueError, match="replacement"):
        manager.retire(candidate_id, reason="cannot retire active")


def test_registry_rejects_corruption_and_missing_root(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text('{"schema_version": 1, "revisions": {}, "defaults": {"x": "missing"}, "events": []}')
    with pytest.raises(ValueError, match="Default index"):
        ModelRegistry(path).read()


def test_incomplete_profile_transition_is_rolled_back_on_restart(tmp_path):
    manager, root, voices, gpt, sovits = setup_manager(tmp_path)
    manager.scan(full_hash=True, register=True)
    before_registry = manager.registry.read()
    before_profile = json.loads((voices / "role.json").read_text(encoding="utf-8"))
    from server.model_registry import atomic_json
    atomic_json(manager.pending_path, {"schema_version": 1, "character_id": "role",
                                       "before_registry": before_registry,
                                       "before_profile": before_profile})
    atomic_json(manager.registry.path, {"schema_version": 1, "revisions": {}, "defaults": {}, "events": []})
    broken = {**before_profile, "default_model": "half-written"}
    atomic_json(voices / "role.json", broken)
    recovered = ModelManager(manager.root, voices)
    assert recovered.registry.read() == before_registry
    assert json.loads((voices / "role.json").read_text(encoding="utf-8")) == before_profile
    assert not recovered.pending_path.exists()

