import pytest

from server.evaluations import EvaluationStore


SAMPLES = {"sample_set_id": "cvs-core-v1", "version": 1, "samples": [
    {"sample_id": "en-short-001", "text": "A calm morning begins.",
     "language": "en", "category": "short"}]}


def test_evaluation_record_tracks_results_reviews_and_decision(tmp_path):
    store = EvaluationStore(tmp_path)
    record = store.create(character_id="role", candidate_revision_id="rev-candidate",
                          baseline_revision_id="rev-default", sample_set=SAMPLES,
                          engine={"id": "gpt-sovits", "adapter_version": "1"},
                          reference_set_id="role-en-v1", requested_parameters={"speed": 1})
    assert record["sample_set"]["sha256"]
    evaluation_id = record["evaluation_id"]
    store.update(evaluation_id, status="running", effective_parameters={"speed": 1},
                 unsupported_parameters=["seed"])
    result = {"sample_id": "en-short-001", "model_revision_id": "rev-candidate",
              "status": "success", "audio_artifact_id": "audio-1", "audio_sha256": "a" * 64,
              "queue_ms": 1, "load_ms": 2, "synthesis_ms": 3, "audio_duration_ms": 1000,
              "real_time_factor": 0.003, "error_code": None, "error_message": None}
    store.update(evaluation_id, result=result)
    # Updating the same model/sample is resumable and replaces the prior result.
    result["synthesis_ms"] = 4
    assert len(store.update(evaluation_id, result=result)["results"]) == 1
    store.update(evaluation_id, result={**result, "model_revision_id": "rev-default",
                                       "audio_artifact_id": "audio-2", "audio_sha256": "b" * 64})
    store.review(evaluation_id, blind_pair_id="pair-1", preference="a",
                 ratings={"voice_similarity": 4, "pronunciation": 5, "continuity": None},
                 notes="candidate clearer")
    with pytest.raises(ValueError, match="Complete"):
        store.decide(evaluation_id, decision="promote", reason="accepted")
    store.update(evaluation_id, status="completed")
    final = store.decide(evaluation_id, decision="promote", reason="accepted")
    assert final["decision"]["value"] == "promote"
    assert store.list()[0]["evaluation_id"] == evaluation_id


def test_evaluation_validation_rejects_bad_samples_results_and_ratings(tmp_path):
    store = EvaluationStore(tmp_path)
    with pytest.raises(ValueError, match="unique"):
        store.create(character_id="role", candidate_revision_id="rev-a", baseline_revision_id=None,
                     sample_set={"samples": [{"sample_id": "x"}, {"sample_id": "x"}]}, engine={})
    record = store.create(character_id="role", candidate_revision_id="rev-a",
                          baseline_revision_id=None, sample_set=SAMPLES, engine={})
    with pytest.raises(ValueError, match="belong"):
        store.update(record["evaluation_id"], result={"sample_id": "wrong",
                     "model_revision_id": "rev-a", "status": "failed"})
    with pytest.raises(ValueError, match="SHA"):
        store.update(record["evaluation_id"], result={"sample_id": "en-short-001",
                     "model_revision_id": "rev-a", "status": "success"})
    with pytest.raises(ValueError, match="Ratings"):
        store.review(record["evaluation_id"], blind_pair_id="x", preference="a",
                     ratings={"naturalness": 6})

