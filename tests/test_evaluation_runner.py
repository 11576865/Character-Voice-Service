import io
import threading
import wave
from types import SimpleNamespace

from server.evaluation_runner import EvaluationRunner
from server.evaluations import EvaluationStore


def wav_bytes(frames=800):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * frames)
    return output.getvalue()


def test_runner_saves_provenance_audio_and_blind_pairs(tmp_path, monkeypatch):
    store = EvaluationStore(tmp_path / "evaluations")
    record = store.create(
        character_id="role", candidate_revision_id="candidate", baseline_revision_id="baseline",
        sample_set={"name": "test", "samples": [
            {"sample_id": "short", "text": "Hello", "category": "short"},
            {"sample_id": "emotion", "text": "Wonderful!", "category": "emotion"},
        ]}, engine={"id": "gpt-sovits"}, reference_set_id="happy",
        requested_parameters={"speed": 1.1})
    registry = SimpleNamespace(read=lambda: {"revisions": {
        "candidate": {"revision_id": "candidate"}, "baseline": {"revision_id": "baseline"}}})
    manager = SimpleNamespace(evaluations=store, registry=registry)
    calls = []
    runner = EvaluationRunner(manager, lambda text, speed, selection:
                              calls.append((text, speed, selection["selected_model"]["revision_id"])) or wav_bytes())
    monkeypatch.setattr(runner, "_selection", lambda current, revision:
                        ({"selected_model": {"revision_id": revision["revision_id"]}},
                         "happy", {"speed": 1.1, "seed": 7}))

    completed = runner.run(record["evaluation_id"], threading.Event())
    assert completed["status"] == "completed"
    assert len(calls) == 4
    assert all(item["actual_reference_id"] == "happy" for item in completed["results"])
    assert all(item["effective_parameters"]["seed"] == 7 for item in completed["results"])
    assert all(store.audio_is_valid(record["evaluation_id"], item) for item in completed["results"])

    review = store.review_pairs(record["evaluation_id"])
    assert len(review["pairs"]) == 2
    assert "revision" not in str(review["pairs"])
    store.review(record["evaluation_id"], blind_pair_id="short", preference="a",
                 ratings={"naturalness": 4})
    saved = store.get(record["evaluation_id"])["human_reviews"][-1]
    assert {saved["a_revision_id"], saved["b_revision_id"]} == {"candidate", "baseline"}
    assert "a_revision_id" not in store.review_pairs(record["evaluation_id"])["pairs"][0]["review"]


def test_runner_can_resume_successful_audio(tmp_path, monkeypatch):
    store = EvaluationStore(tmp_path / "evaluations")
    record = store.create(character_id="role", candidate_revision_id="candidate",
                          baseline_revision_id="baseline",
                          sample_set={"samples": [{"sample_id": "one", "text": "One"}]},
                          engine={"id": "gpt-sovits"})
    audio = wav_bytes()
    artifact_id, digest = store.write_audio(record["evaluation_id"], audio)
    store.update(record["evaluation_id"], result={"sample_id": "one",
                 "model_revision_id": "candidate", "status": "success",
                 "audio_artifact_id": artifact_id, "audio_sha256": digest})
    manager = SimpleNamespace(evaluations=store, registry=SimpleNamespace(read=lambda: {"revisions": {
        "candidate": {"revision_id": "candidate"}, "baseline": {"revision_id": "baseline"}}}))
    calls = []
    runner = EvaluationRunner(manager, lambda *args: calls.append(args) or audio)
    monkeypatch.setattr(runner, "_selection", lambda current, revision:
                        ({"selected_model": {"revision_id": revision["revision_id"]}},
                         "default", {"speed": 1.0}))
    completed = runner.run(record["evaluation_id"], threading.Event())
    assert completed["status"] == "completed"
    assert len(calls) == 1
