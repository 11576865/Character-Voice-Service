from fastapi.testclient import TestClient
from types import SimpleNamespace

import server.app as api
from tests.test_evaluations import SAMPLES
from tests.test_model_registry import setup_manager


def test_registry_and_evaluation_endpoints_are_private_and_connected(tmp_path, monkeypatch):
    manager, root, voices, gpt, sovits = setup_manager(tmp_path)
    monkeypatch.setattr(api, "model_manager", manager)
    public = TestClient(api.app)
    client = TestClient(api.app)
    client.headers["X-CVS-Token"] = api.ADMIN_TOKEN
    assert public.get("/v1/model-registry").status_code == 401
    assert client.post("/v1/model-registry/scan", json={"register": True}).status_code == 400
    first = client.post("/v1/model-registry/scan",
                        json={"full_hash": True, "register": True}).json()["registration"]
    baseline_id = first["added"][0]
    gpt.write_bytes(b"candidate")
    candidate = client.post("/v1/model-registry/scan",
                            json={"full_hash": True, "register": True}).json()["registration"]
    candidate_id = candidate["added"][0]
    assert client.get("/v1/model-registry").json()["revisions"][candidate_id]["lifecycle"] == "candidate"
    created = client.post("/v1/evaluations", json={
        "character_id": "role", "candidate_revision_id": candidate_id,
        "baseline_revision_id": baseline_id, "sample_set": SAMPLES,
        "engine": {"id": "gpt-sovits", "adapter_version": "1"}})
    assert created.status_code == 200
    evaluation_id = created.json()["evaluation_id"]
    runs = []
    class ImmediatePool:
        def submit(self, function, *args):
            function(*args)
    monkeypatch.setattr(api, "evaluation_runner", SimpleNamespace(
        run=lambda selected_id, cancel: runs.append(selected_id)))
    monkeypatch.setattr(api, "evaluation_pool", ImmediatePool())
    assert runs == []
    assert client.post(f"/v1/evaluations/{evaluation_id}/run").status_code == 200
    assert runs == [evaluation_id]
    for revision, digest in ((candidate_id, "a" * 64), (baseline_id, "b" * 64)):
        assert client.put(f"/v1/evaluations/{evaluation_id}", json={"result": {
            "sample_id": "en-short-001", "model_revision_id": revision,
            "status": "success", "audio_sha256": digest}}).status_code == 200
    assert client.put(f"/v1/evaluations/{evaluation_id}", json={"status": "completed"}).status_code == 200
    assert client.post(f"/v1/evaluations/{evaluation_id}/decision",
                       json={"decision": "promote", "reason": "reviewed"}).status_code == 200
    promoted = client.post(f"/v1/model-registry/{candidate_id}/promote",
                           json={"reason": "reviewed", "evaluation_id": evaluation_id})
    assert promoted.status_code == 200
    assert promoted.json()["production_applied"] is True
    assert client.get("/v1/evaluations").json()["evaluations"][0]["decision"]["value"] == "promote"


def test_assignment_and_transition_errors_are_explicit(tmp_path, monkeypatch):
    manager, root, voices, gpt, sovits = setup_manager(tmp_path)
    monkeypatch.setattr(api, "model_manager", manager)
    client = TestClient(api.app)
    client.headers["X-CVS-Token"] = api.ADMIN_TOKEN
    assert client.post("/v1/model-registry/missing/assign", json={
        "character_id": "role", "model_id": "next", "reason": "test"}).status_code == 404
    assert client.post("/v1/model-registry/missing/promote", json={
        "reason": "test", "allow_without_evaluation": True}).status_code == 404

