import json
import wave

import pytest

from server.reference_import import import_reference_pack


def write_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 160)


def test_import_is_idempotent_and_keeps_model_and_default(tmp_path):
    voices = tmp_path / "voices"
    voices.mkdir()
    refs = tmp_path / "references"
    pack = tmp_path / "pack"
    pack.mkdir()
    profile = {
        "schema_version": 2, "name": "Test", "target_language": "en",
        "default_model": "loaded", "models": {"loaded": {"engine": "gpt-sovits"}},
        "default_reference": "default", "references": {
            "default": {"audio": "C:/old.wav", "text": "Old.", "language": "en"}
        },
    }
    (voices / "test.json").write_text(json.dumps(profile), encoding="utf-8")
    write_wav(pack / "audio" / "000001.wav")
    catalog = {"source_project": "HSR", "references": [{
        "id": "000001", "source_member_id": "folder/line.wav",
        "audio": "audio/000001.wav", "text": "First line.", "language": "en",
        "emotion": "happy", "intensity": 0.7, "quality": "A",
    }]}
    (pack / "reference_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    import_reference_pack(pack, "test", voice_dir=voices, reference_dir=refs)
    catalog["references"][0]["text"] = "Corrected line."
    (pack / "reference_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    import_reference_pack(pack, "test", voice_dir=voices, reference_dir=refs)
    result = json.loads((voices / "test.json").read_text(encoding="utf-8"))
    assert len(result["references"]) == 2
    assert result["default_reference"] == "default"
    assert result["models"] == profile["models"]
    imported = next(ref for key, ref in result["references"].items() if key != "default")
    assert imported["text"] == "Corrected line."
    assert len(list((refs / "test").glob("*.wav"))) == 1


def test_import_rejects_path_escape_without_changing_profile(tmp_path):
    voices = tmp_path / "voices"
    voices.mkdir()
    pack = tmp_path / "pack"
    pack.mkdir()
    original = {"schema_version": 2, "name": "Test", "target_language": "en",
                "default_model": "loaded", "models": {"loaded": {}},
                "default_reference": "default", "references": {
                    "default": {"audio": "C:/old.wav", "text": "Old.", "language": "en"}}}
    raw = json.dumps(original)
    (voices / "test.json").write_text(raw, encoding="utf-8")
    (pack / "reference_catalog.json").write_text(json.dumps({"references": [{
        "id": "1", "audio": "../escape.wav", "text": "Hi", "language": "en"
    }]}), encoding="utf-8")
    with pytest.raises(ValueError):
        import_reference_pack(pack, "test", voice_dir=voices, reference_dir=tmp_path / "refs")
    assert (voices / "test.json").read_text(encoding="utf-8") == raw
