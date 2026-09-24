import json
from pathlib import Path


TEMPLATE_FILENAME = "example.json"
REQUIRED_FIELDS = (
    "reference_audio",
    "reference_text",
    "reference_language",
    "target_language",
)


def iter_real_profile_paths(voice_dir: Path):
    """Yield real voice profiles, excluding the checked-in example template."""
    if not voice_dir.exists():
        return
    for path in sorted(voice_dir.glob("*.json")):
        if path.name.casefold() != TEMPLATE_FILENAME:
            yield path


def read_valid_profile(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        profile = json.load(handle)
    missing = [field for field in REQUIRED_FIELDS if not profile.get(field)]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    return profile


def find_valid_profiles(voice_dir: Path) -> list[tuple[Path, dict]]:
    valid = []
    for path in iter_real_profile_paths(voice_dir):
        try:
            valid.append((path, read_valid_profile(path)))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            continue
    return valid


def main() -> int:
    from server.config import VOICE_DIR

    profiles = find_valid_profiles(VOICE_DIR)
    if not profiles:
        print(
            "ERROR: No valid real voice profile was found in voices/.\n"
            "Copy voices\\example.json to a stable voice ID such as "
            "voices\\march-7th.json, then replace the placeholders with "
            "your local reference audio path and exact transcript."
        )
        return 2

    ids = ", ".join(path.stem for path, _ in profiles)
    print(f"Voice profile check passed: {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
