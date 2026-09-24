import json
from pathlib import Path


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
