import json
import wave

import pytest

from server.benchmark_dataset import freeze_dataset, verify_dataset


def _wav(path, *, frames=b"\x00\x00" * 800):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(frames)


def _write_rows(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _row(item_id, split, wav):
    return {
        "id": item_id, "split": split, "source": "ORIGINAL", "audio": wav,
        "text": "Human-corrected " + item_id, "language": "en", "style": "neutral",
    }


def _fixture(tmp_path):
    audio = tmp_path / "audio"
    _wav(audio / "a.wav", frames=b"\x01\x00" * 800)
    _wav(audio / "sub" / "b.wav", frames=b"\x02\x00" * 1600)
    rows = [_row("b", "test-recorded", "sub/b.wav"), _row("a", "reference", "a.wav")]
    input_file = tmp_path / "curated.jsonl"
    _write_rows(input_file, rows)
    return audio, input_file, rows


def test_freeze_is_deterministic_and_verifies_original_wavs(tmp_path):
    audio, input_file, rows = _fixture(tmp_path)
    manifest = tmp_path / "frozen.json"
    first = freeze_dataset(input_file, audio, manifest, "march7-en-v1")
    original = manifest.read_bytes()
    assert first["splits"]["reference"] == 1
    assert first["splits"]["test-recorded"] == 1
    assert first["cross_split_text_overlaps"] == 0
    assert verify_dataset(manifest, audio) == first
    assert freeze_dataset(input_file, audio, manifest, "march7-en-v1") == first
    assert manifest.read_bytes() == original
    assert [item["id"] for item in json.loads(original)["items"]] == ["a", "b"]
    assert str(audio.resolve()) not in original.decode("utf-8")
    assert json.loads(original)["items"][0]["duration_sec"] == 0.1
    assert len(json.loads(original)["dataset_sha256"]) == 64


def test_rejects_source_mutation_after_freeze(tmp_path):
    audio, input_file, _ = _fixture(tmp_path)
    manifest = tmp_path / "frozen.json"
    freeze_dataset(input_file, audio, manifest, "dataset-v1")
    _wav(audio / "a.wav", frames=b"\x03\x00" * 800)
    with pytest.raises(ValueError, match="changed"):
        verify_dataset(manifest, audio)


def test_freeze_refuses_rewrite_with_different_split_or_transcript(tmp_path):
    audio, input_file, rows = _fixture(tmp_path)
    manifest = tmp_path / "frozen.json"
    freeze_dataset(input_file, audio, manifest, "dataset-v1")
    rows[0]["split"] = "train"
    _write_rows(input_file, rows)
    with pytest.raises(ValueError, match="already exists"):
        freeze_dataset(input_file, audio, manifest, "dataset-v1")


@pytest.mark.parametrize("bad_path", ["../outside.wav", "/tmp/outside.wav", "C:\\test.wav", "./a.wav", "a.mp3"])
def test_rejects_unsafe_or_non_wav_paths(tmp_path, bad_path):
    audio, input_file, rows = _fixture(tmp_path)
    rows[0]["audio"] = bad_path
    _write_rows(input_file, rows)
    with pytest.raises(ValueError, match="audio|line"):
        freeze_dataset(input_file, audio, tmp_path / "frozen.json", "dataset-v1")


def test_rejects_symlink_escape(tmp_path):
    audio, input_file, rows = _fixture(tmp_path)
    outside = tmp_path / "outside.wav"
    _wav(outside)
    (audio / "alias.wav").symlink_to(outside)
    rows[0]["audio"] = "alias.wav"
    _write_rows(input_file, rows)
    with pytest.raises(ValueError, match="escapes"):
        freeze_dataset(input_file, audio, tmp_path / "frozen.json", "dataset-v1")


def test_duplicate_pcm_even_under_different_names_is_rejected(tmp_path):
    audio, input_file, rows = _fixture(tmp_path)
    (audio / "copy.wav").write_bytes((audio / "a.wav").read_bytes())
    rows[0]["audio"] = "copy.wav"
    _write_rows(input_file, rows)
    with pytest.raises(ValueError, match="duplicate audio_sha256"):
        freeze_dataset(input_file, audio, tmp_path / "frozen.json", "dataset-v1")


def test_synthetic_audio_cannot_enter_original_corpus(tmp_path):
    audio, input_file, rows = _fixture(tmp_path)
    rows[0]["source"] = "SYNTHETIC"
    _write_rows(input_file, rows)
    with pytest.raises(ValueError, match="ORIGINAL"):
        freeze_dataset(input_file, audio, tmp_path / "frozen.json", "dataset-v1")


def test_manifest_fingerprint_rejects_post_freeze_split_tampering(tmp_path):
    audio, input_file, _ = _fixture(tmp_path)
    manifest = tmp_path / "frozen.json"
    freeze_dataset(input_file, audio, manifest, "dataset-v1")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["items"][0]["split"] = "train"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        verify_dataset(manifest, audio)


def test_text_overlap_is_reported_not_misidentified_as_audio_leak(tmp_path):
    audio, input_file, rows = _fixture(tmp_path)
    rows[1]["text"] = rows[0]["text"].upper()
    _write_rows(input_file, rows)
    result = freeze_dataset(input_file, audio, tmp_path / "frozen.json", "dataset-v1")
    assert result["cross_split_text_overlaps"] == 1


def test_reference_pool_can_overlap_train_but_never_held_out_test(tmp_path):
    audio, input_file, rows = _fixture(tmp_path)
    rows[1]["split"] = "train"
    rows[1]["reference"] = True
    _write_rows(input_file, rows)
    manifest = tmp_path / "frozen.json"
    result = freeze_dataset(input_file, audio, manifest, "dataset-v1")
    assert result["splits"]["train"] == 1
    assert result["reference_pool"] == 1
    assert verify_dataset(manifest, audio) == result
    rows[0]["reference"] = True
    _write_rows(input_file, rows)
    with pytest.raises(ValueError, match="held-out test"):
        freeze_dataset(input_file, audio, tmp_path / "other.json", "dataset-v2")
