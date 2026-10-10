import hashlib
import json
import wave
from io import BytesIO

import httpx
import pytest

from server import voicebench

MODEL_REVISION = "a" * 64
GENERATION_REVISION = "b" * 64


def wav(value=1):
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(bytes([value, 0]) * 800)
    return output.getvalue()


def corpus(tmp_path, monkeypatch):
    data = [
        {"id": "ref", "split": "train", "reference": True,
         "audio_sha256": hashlib.sha256(wav(1)).hexdigest()},
        {"id": "test-one", "split": "test-recorded", "reference": False,
         "text": "one", "audio_sha256": hashlib.sha256(wav(2)).hexdigest()},
        {"id": "test-two", "split": "test-recorded", "reference": False,
         "text": "two", "audio_sha256": hashlib.sha256(wav(3)).hexdigest()},
    ]
    manifest = tmp_path / "dataset.json"
    manifest.write_text(json.dumps({"items": data}), encoding="utf-8")
    monkeypatch.setattr(voicebench, "verify_dataset", lambda path, root: {
        "dataset_id": "dataset-v1", "dataset_sha256": "d" * 64,
    })
    return manifest


def api(*, ref=wav(1), fail_second=False, wrong_header=False, changed_identity=False):
    state = {"synth": 0, "resolve": 0}

    def handler(request):
        if request.url.path.endswith("/audio") and request.method == "GET":
            assert request.headers.get("x-cvs-token") == "secret"
            return httpx.Response(200, content=ref, headers={"content-type": "audio/wav"})
        payload = json.loads(request.content)
        if request.url.path == "/v1/audio/resolve":
            state["resolve"] += 1
            revision = ("c" * 64 if changed_identity and state["resolve"] > 1 else GENERATION_REVISION)
            return httpx.Response(200, json={
                "voice": "march-7th", "model": "model-real-1", "engine": "gpt-sovits",
                "model_revision": MODEL_REVISION, "generation_revision": revision,
                "reference": "neutral", "runtime": "runtime-1", "runtime_revision": "r" * 64,
                "binding": None, "binding_revision": None,
            })
        if request.url.path == "/v1/audio/speech":
            state["synth"] += 1
            if fail_second and payload["input"] == "two" and state["synth"] == 2:
                return httpx.Response(503, json={"detail": "GPU busy"})
            return httpx.Response(200, content=wav(state["synth"]), headers={
                "content-type": "audio/wav", "x-cvs-request-id": f"request-{state['synth']}",
                "x-cvs-voice": "march-7th", "x-cvs-model": "model-real-1",
                "x-cvs-engine": "gpt-sovits", "x-cvs-model-revision": MODEL_REVISION,
                "x-cvs-generation-revision": ("f" * 64 if wrong_header else GENERATION_REVISION),
                "x-selected-reference": "neutral",
            })
        return httpx.Response(404)

    client = httpx.Client(base_url="http://127.0.0.1:9881", transport=httpx.MockTransport(handler))
    return client, state


def run_kwargs(tmp_path, manifest, client, **kw):
    return dict(
        manifest_path=manifest, audio_root=tmp_path, output_dir=tmp_path / "run",
        voice="march-7th", model_id="model-alias", reference_id="neutral",
        reference_item_id="ref", admin_token="secret", client=client, **kw,
    )


def test_writes_two_verified_audio_results_and_no_quality_claim(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    client, stats = api()
    with client:
        record = voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client))
    assert record["status"] == "complete"
    assert record["quality_evaluation"] == "not_performed"
    assert record["identity"]["model_revision"] == MODEL_REVISION
    assert stats["synth"] == 2
    assert len(list((tmp_path / "run" / "audio").glob("*.wav"))) == 2
    for value in record["items"].values():
        assert value["status"] == "ok"
        assert value["duration_seconds"] == 0.1
        assert value["realtime_factor"] >= 0
        assert len(value["output_sha256"]) == 64
    assert (tmp_path / "run" / "run.json").is_file()
    assert not (tmp_path / "run" / "evaluation.json").exists()


def test_rejects_reference_mismatch_before_writing_any_artifact(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    client, _ = api(ref=wav(9))
    with client, pytest.raises(ValueError, match="reference WAV"):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client))
    assert not (tmp_path / "run").exists()


def test_partial_failure_resume_keeps_previous_output_and_checks_hash(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    client, _ = api(fail_second=True)
    with client, pytest.raises(RuntimeError, match="test-two"):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client))
    record = json.loads((tmp_path / "run" / "run.json").read_text())
    assert record["status"] == "partial"
    assert record["items"]["test-one"]["status"] == "ok"
    original = (tmp_path / "run" / "audio" / "test-one.wav").read_bytes()
    client2, stats2 = api()
    with client2:
        res = voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client2, resume=True))
    assert res["status"] == "complete"
    assert (tmp_path / "run" / "audio" / "test-one.wav").read_bytes() == original
    assert stats2["synth"] == 1


def test_resume_rejects_mutated_existing_audio(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    client, _ = api(fail_second=True)
    with client, pytest.raises(RuntimeError):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client))
    (tmp_path / "run" / "audio" / "test-one.wav").write_bytes(b"mutated")
    client2, _ = api()
    with client2, pytest.raises(ValueError, match="mutated"):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client2, resume=True))


def test_rejects_generation_revision_header_mismatch(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    client, _ = api(wrong_header=True)
    with client, pytest.raises(RuntimeError):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client))
    assert not list((tmp_path / "run" / "audio").glob("*.wav"))
    assert json.loads((tmp_path / "run" / "run.json").read_text())["status"] == "partial"


def test_rejects_mid_run_identity_drift(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    client, state = api(changed_identity=True)
    with client, pytest.raises(RuntimeError, match="test-one"):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client))
    assert state["synth"] == 0


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:9881", "http://example.com:9881", "http://127.0.0.1:9881/foo",
    "http://user:pw@localhost:9881", "http://127.0.0.1:9881?secret=1", "file:///tmp/socket",
])
def test_rejects_non_loopback_or_unsafe_origin(tmp_path, monkeypatch, url):
    manifest = corpus(tmp_path, monkeypatch)
    client, _ = api()
    with client, pytest.raises(ValueError, match="loopback"):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client, base_url=url))


def test_rejects_reusing_a_completed_output_directory(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    client, _ = api()
    with client:
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client))
    second, _ = api()
    with second, pytest.raises(ValueError, match="already exists"):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, second))


def test_resume_rejects_changed_model_alias(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    client, _ = api()
    with client:
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client))
    second, _ = api()
    with second, pytest.raises(ValueError, match="settings"):
        params = run_kwargs(tmp_path, manifest, second, resume=True)
        params["model_id"] = "another"
        voicebench.run_voicebench(**params)


def test_required_admin_token_is_not_optional(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    client, _ = api()
    with client, pytest.raises(ValueError, match="admin token"):
        params = run_kwargs(tmp_path, manifest, client)
        params["admin_token"] = ""
        voicebench.run_voicebench(**params)


def test_resume_rejects_changed_resolver_provenance(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    client, _ = api(fail_second=True)
    with client, pytest.raises(RuntimeError):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client))

    def mismatch(request):
        if request.url.path.endswith("/audio"):
            return httpx.Response(200, content=wav(1))
        if request.url.path == "/v1/audio/resolve":
            return httpx.Response(200, json={
                "voice": "march-7th", "model": "different-model", "engine": "gpt-sovits",
                "model_revision": MODEL_REVISION, "generation_revision": GENERATION_REVISION,
                "reference": "neutral", "runtime": "runtime-1", "runtime_revision": "r" * 64,
            })
        return httpx.Response(404)

    with httpx.Client(base_url="http://127.0.0.1:9881", transport=httpx.MockTransport(mismatch)) as second:
        with pytest.raises(ValueError, match="identity differs"):
            voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, second, resume=True))


def test_source_verification_failure_prevents_network_or_output(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)

    def fail_verification(path, audio_root):
        raise ValueError("original WAV digest mismatch")

    monkeypatch.setattr(voicebench, "verify_dataset", fail_verification)
    client, stats = api()
    with client, pytest.raises(ValueError, match="WAV digest"):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, client))
    assert stats["synth"] == 0
    assert not (tmp_path / "run").exists()


def test_truncated_pcm_payload_does_not_count_as_a_valid_output():
    data = wav(1)
    with pytest.raises(ValueError, match="truncated"):
        voicebench._assert_wav(data[:-200])


def test_resume_recovers_staged_wav_after_publish_crash(tmp_path, monkeypatch):
    from pathlib import Path

    manifest = corpus(tmp_path, monkeypatch)
    original_rename = Path.rename
    crashed = {"done": False}

    def interrupted_publish(self, target):
        if self.name.endswith(".wav.part") and not crashed["done"]:
            crashed["done"] = True
            raise OSError("simulated interruption before publishing WAV")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", interrupted_publish)
    first, _ = api()
    with first, pytest.raises(RuntimeError, match="test-one"):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, first))
    recorded = json.loads((tmp_path / "run" / "run.json").read_text())
    assert recorded["items"]["test-one"]["status"] == "prepared"
    assert (tmp_path / "run" / "audio" / "test-one.wav.part").is_file()

    monkeypatch.setattr(Path, "rename", original_rename)
    second, stats = api()
    with second:
        finished = voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, second, resume=True))
    assert finished["status"] == "complete"
    assert stats["synth"] == 1
    assert not (tmp_path / "run" / "audio" / "test-one.wav.part").exists()


def test_resume_recovers_published_wav_after_final_checkpoint_crash(tmp_path, monkeypatch):
    manifest = corpus(tmp_path, monkeypatch)
    actual_durable = voicebench._durable_json
    crashed = {"done": False}

    def interrupted_checkpoint(path, record):
        if (not crashed["done"] and record.get("items", {}).get("test-one", {}).get("status") == "ok"):
            crashed["done"] = True
            raise OSError("simulated interruption after WAV publication")
        return actual_durable(path, record)

    monkeypatch.setattr(voicebench, "_durable_json", interrupted_checkpoint)
    first, _ = api()
    with first, pytest.raises(RuntimeError, match="test-one"):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, first))
    journal = json.loads((tmp_path / "run" / "run.json").read_text())
    assert journal["items"]["test-one"]["status"] == "prepared"
    published = tmp_path / "run" / "audio" / "test-one.wav"
    assert published.is_file()
    digest = hashlib.sha256(published.read_bytes()).hexdigest()

    monkeypatch.setattr(voicebench, "_durable_json", actual_durable)
    second, stats = api()
    with second:
        finished = voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, second, resume=True))
    assert finished["status"] == "complete"
    assert finished["items"]["test-one"]["output_sha256"] == digest
    assert stats["synth"] == 1


def test_resume_rejects_mutated_prepared_audio(tmp_path, monkeypatch):
    from pathlib import Path

    manifest = corpus(tmp_path, monkeypatch)
    original_rename = Path.rename

    def fail_publish(self, target):
        if self.name.endswith(".wav.part"):
            raise OSError("simulated interruption")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_publish)
    first, _ = api()
    with first, pytest.raises(RuntimeError):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, first))
    pending_audio = tmp_path / "run" / "audio" / "test-one.wav.part"
    pending_audio.write_bytes(b"tampered")
    monkeypatch.setattr(Path, "rename", original_rename)
    second, _ = api()
    with second, pytest.raises(ValueError, match="mutated"):
        voicebench.run_voicebench(**run_kwargs(tmp_path, manifest, second, resume=True))
