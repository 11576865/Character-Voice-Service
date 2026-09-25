import json
from pathlib import Path

import pytest

from server.voice_profiles import (
    find_valid_profiles,
    iter_real_profile_paths,
    public_profile_summary,
    read_valid_profile,
    resolve_profile_selection,
)


def legacy_profile():
    return {
        "name": "March 7th",
        "reference_audio": "C:/private/reference.wav",
        "reference_text": "Exact reference text.",
        "reference_language": "en",
        "target_language": "en",
        "parameters": {"top_k": 9},
    }


def registry_profile():
    return {
        "schema_version": 2,
        "name": "March 7th",
        "target_language": "en",
        "default_model": "self-400-v2pro",
        "models": {
            "self-400-v2pro": {
                "name": "Self 400",
                "engine": "gpt-sovits",
                "version": "v2pro",
                "gpt_weights": "D:/models/march.ckpt",
                "sovits_weights": "D:/models/march.pth",
                "parameters": {"temperature": 0.8},
            },
            "downloaded-v2pro": {
                "name": "Downloaded",
                "engine": "gpt-sovits",
                "version": "v2pro",
                "gpt_weights": "D:/models/downloaded.ckpt",
                "sovits_weights": "D:/models/downloaded.pth",
            },
        },
        "default_reference": "neutral-01",
        "references": {
            "neutral-01": {
                "name": "Neutral",
                "audio": "D:/refs/neutral.wav",
                "text": "A neutral reference.",
                "language": "en",
                "emotion": "neutral",
                "intensity": 0.4,
                "quality": "good",
            },
            "surprised-01": {
                "name": "Surprised",
                "audio": "D:/refs/surprised.wav",
                "text": "A surprised reference!",
                "language": "en",
                "emotion": "surprised",
                "intensity": 0.8,
                "quality": "good",
            },
        },
        "parameters": {"top_k": 15, "temperature": 1.0},
    }


def test_example_profile_uses_registry_schema():
    profile_path = Path(__file__).resolve().parent.parent / "voices" / "example.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))

    assert profile["schema_version"] == 2
    assert profile["default_model"] in profile["models"]
    assert profile["default_reference"] in profile["references"]


def test_example_template_is_not_a_real_profile(tmp_path):
    (tmp_path / "example.json").write_text("{}", encoding="utf-8")
    assert list(iter_real_profile_paths(tmp_path)) == []
    assert find_valid_profiles(tmp_path) == []


def test_legacy_profile_remains_valid_and_normalizes_to_registry(tmp_path):
    path = tmp_path / "march-7th.json"
    path.write_text(json.dumps(legacy_profile()), encoding="utf-8")

    profile = read_valid_profile(path)

    assert profile["schema_version"] == 1
    assert profile["default_model"] == "loaded"
    assert profile["models"]["loaded"]["managed"] is False
    assert profile["default_reference"] == "default"
    assert profile["references"]["default"]["audio"] == "C:/private/reference.wav"

    selection = resolve_profile_selection(profile)
    assert selection["reference_audio"] == "C:/private/reference.wav"
    assert selection["reference_text"] == "Exact reference text."
    assert selection["parameters"]["top_k"] == 9


def test_registry_profile_selects_model_and_reference(tmp_path):
    path = tmp_path / "march-7th.json"
    path.write_text(json.dumps(registry_profile()), encoding="utf-8")
    profile = read_valid_profile(path)

    selection = resolve_profile_selection(
        profile,
        model_id="downloaded-v2pro",
        reference_id="surprised-01",
    )

    assert selection["selected_model"]["id"] == "downloaded-v2pro"
    assert selection["selected_model"]["managed"] is True
    assert selection["reference_audio"] == "D:/refs/surprised.wav"
    assert selection["selected_reference"]["emotion"] == "surprised"
    assert selection["parameters"]["temperature"] == 1.0


def test_model_parameters_override_character_defaults(tmp_path):
    raw = registry_profile()
    path = tmp_path / "march-7th.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    profile = read_valid_profile(path)

    selection = resolve_profile_selection(profile, model_id="self-400-v2pro")
    assert selection["parameters"]["temperature"] == 0.8
    assert selection["parameters"]["top_k"] == 15


def test_public_summary_does_not_expose_local_paths_or_transcripts(tmp_path):
    path = tmp_path / "march-7th.json"
    path.write_text(json.dumps(registry_profile()), encoding="utf-8")
    profile = read_valid_profile(path)

    summary = public_profile_summary("march-7th", profile)
    encoded = json.dumps(summary)

    assert summary["default_model"] == "self-400-v2pro"
    assert len(summary["models"]) == 2
    assert len(summary["references"]) == 2
    assert "D:/models" not in encoded
    assert "D:/refs" not in encoded
    assert "A surprised reference!" not in encoded


def test_unknown_model_and_reference_are_rejected(tmp_path):
    path = tmp_path / "march-7th.json"
    path.write_text(json.dumps(registry_profile()), encoding="utf-8")
    profile = read_valid_profile(path)

    with pytest.raises(KeyError, match="model not found"):
        resolve_profile_selection(profile, model_id="missing")
    with pytest.raises(KeyError, match="reference not found"):
        resolve_profile_selection(profile, reference_id="missing")


def test_invalid_registry_defaults_are_rejected(tmp_path):
    raw = registry_profile()
    raw["default_model"] = "missing"
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="default_model not found"):
        read_valid_profile(path)


def test_partial_model_weight_registration_is_rejected(tmp_path):
    raw = registry_profile()
    del raw["models"]["self-400-v2pro"]["sovits_weights"]
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="configured together"):
        read_valid_profile(path)


def test_invalid_real_profile_does_not_satisfy_startup_check(tmp_path):
    (tmp_path / "broken.json").write_text("{}", encoding="utf-8")
    assert find_valid_profiles(tmp_path) == []
    with pytest.raises(ValueError, match="missing fields"):
        read_valid_profile(tmp_path / "broken.json")
