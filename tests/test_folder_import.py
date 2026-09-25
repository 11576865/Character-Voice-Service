import json
import wave

from server.folder_import import apply_import, plan_import


def _wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 160)


def test_imports_character_folders_and_reimports_without_duplicates(tmp_path):
    source = tmp_path / "original" / "voices"
    source.mkdir(parents=True)
    model = tmp_path / "model"
    voices = tmp_path / "new" / "voices"
    refs = tmp_path / "new" / "references"
    for role in ("三月七", "银狼"):
        (model / "GPT_weights_v4").mkdir(parents=True, exist_ok=True)
        (model / "SoVITS_weights_v4").mkdir(parents=True, exist_ok=True)
        (model / "GPT_weights_v4" / f"{role}-e10.ckpt").write_bytes(b"gpt")
        (model / "SoVITS_weights_v4" / f"{role}_e10.pth").write_bytes(b"sovits")
        _wav(source / role / "reference_audios" / "英语" / "emotions" / "【中立】Hello there.wav")
        _wav(source / role / "reference_audios" / "英语" / "emotions" / "【开心】How lovely!.wav")
    (source / "march-7th.json").write_text(json.dumps({
        "name": "March 7th", "reference_audio": str(source / "三月七" / "old.wav"),
        "reference_text": "Old default.", "reference_language": "en", "target_language": "en",
    }), encoding="utf-8")
    _wav(source / "三月七" / "old.wav")
    plan = plan_import(source, model, voice_dir=voices, reference_dir=refs)
    assert [item["count"] for item in plan] == [2, 2]
    assert not voices.exists()
    apply_import(plan)
    apply_import(plan_import(source, model, voice_dir=voices, reference_dir=refs))
    march = json.loads((voices / "march-7th.json").read_text(encoding="utf-8"))
    wolf = json.loads((voices / "silver-wolf.json").read_text(encoding="utf-8"))
    assert len(march["references"]) == 3
    assert len(wolf["references"]) == 2
    assert march["default_reference"] == "default"
    assert wolf["references"][wolf["default_reference"]]["emotion"] == "neutral"
    assert any(ref["text"] == "How lovely!" and ref["quality"] == "unrated"
               for ref in wolf["references"].values())
    assert march["models"]["v4-local"]["gpt_weights"].endswith("三月七-e10.ckpt")
    assert len(list(refs.rglob("*.wav"))) == 4


def test_missing_old_default_is_replaced_by_imported_neutral(tmp_path):
    source = tmp_path / "voices"
    source.mkdir()
    model = tmp_path / "model"
    (model / "GPT_weights_v4").mkdir(parents=True)
    (model / "SoVITS_weights_v4").mkdir(parents=True)
    (model / "GPT_weights_v4" / "三月七-e10.ckpt").write_bytes(b"gpt")
    (model / "SoVITS_weights_v4" / "三月七_e10.pth").write_bytes(b"sovits")
    _wav(source / "三月七" / "reference_audios" / "英语" / "emotions" / "【中立】Hello.wav")
    (source / "march-7th.json").write_text(json.dumps({
        "name": "March 7th", "reference_audio": str(source / "missing.wav"),
        "reference_text": "Old.", "reference_language": "en", "target_language": "en",
    }), encoding="utf-8")
    plan = plan_import(source, model, voice_dir=tmp_path / "target", reference_dir=tmp_path / "refs")
    profile = plan[0]["data"]
    assert len(profile["references"]) == 1
    assert profile["references"][profile["default_reference"]]["text"] == "Hello"
