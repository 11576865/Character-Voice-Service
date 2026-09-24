import json
from pathlib import Path

import pytest

from server.voice_profiles import find_valid_profiles, iter_real_profile_paths, read_valid_profile


def test_example_profile_has_required_fields():
    profile_path = Path(__file__).resolve().parent.parent / "voices" / "example.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))

    for key in (
        "reference_audio",
        "reference_text",
        "reference_language",
        "target_language",
    ):
        assert profile.get(key)


def test_example_template_is_not_a_real_profile(tmp_path):
    (tmp_path / "example.json").write_text("{}", encoding="utf-8")
    assert list(iter_real_profile_paths(tmp_path)) == []
    assert find_valid_profiles(tmp_path) == []


def test_valid_profile_uses_filename_stem_as_voice_id(tmp_path):
    profile = {
        "name": "March 7th",
        "reference_audio": "C:/private/reference.wav",
        "reference_text": "Exact reference text.",
        "reference_language": "en",
        "target_language": "en",
    }
    path = tmp_path / "march-7th.json"
    path.write_text(json.dumps(profile), encoding="utf-8")

    profiles = find_valid_profiles(tmp_path)

    assert len(profiles) == 1
    assert profiles[0][0].stem == "march-7th"
    assert profiles[0][1]["name"] == "March 7th"


def test_invalid_real_profile_does_not_satisfy_startup_check(tmp_path):
    (tmp_path / "broken.json").write_text("{}", encoding="utf-8")
    assert find_valid_profiles(tmp_path) == []
    with pytest.raises(ValueError, match="missing fields"):
        read_valid_profile(tmp_path / "broken.json")
