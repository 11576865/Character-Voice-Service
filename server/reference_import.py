"""Import a human-curated HSR Reference Pack into one CVS character."""

import argparse
import json
import re
import shutil
import wave
from pathlib import Path

from server.config import PROJECT_ROOT, VOICE_DIR
from server.voice_profiles import normalize_profile


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REFERENCE_DIR = PROJECT_ROOT / "references"


def import_reference_pack(pack: Path, voice_id: str, *, voice_dir: Path = VOICE_DIR,
                          reference_dir: Path = REFERENCE_DIR) -> dict:
    if not _SAFE_ID.fullmatch(voice_id) or voice_id == "example":
        raise ValueError("Invalid target character ID")
    pack = pack.resolve(strict=True)
    catalog_path = pack / "reference_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entries = catalog.get("references")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Reference Pack contains no references")
    profile_path = voice_dir / f"{voice_id}.json"
    if not profile_path.is_file():
        raise ValueError(f"Create the target character profile first: {profile_path}")
    original = json.loads(profile_path.read_text(encoding="utf-8"))
    profile = normalize_profile(original)
    if profile["schema_version"] != 2:
        raise ValueError("Convert the target profile to registry schema v2 first")

    source_key = str(catalog.get("source_project") or catalog.get("speaker") or pack.name).strip()
    if not source_key:
        raise ValueError("Reference Pack has no source identity")
    source_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", source_key).strip(".-_")[:32] or "pack"
    prepared = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Reference entry must be an object")
        source_member = str(entry.get("source_member_id") or entry.get("id") or "").strip()
        if not source_member or source_member in seen:
            raise ValueError("Missing or duplicate Reference Pack source_member_id/id")
        seen.add(source_member)
        text = str(entry.get("text") or "").strip()
        language = str(entry.get("language") or "").strip()
        if not text or not language:
            raise ValueError(f"Reference {source_member} lacks text or language")
        audio_rel = Path(str(entry.get("audio") or ""))
        audio = (pack / audio_rel).resolve()
        if audio_rel.is_absolute() or pack not in audio.parents or audio.suffix.lower() != ".wav" or not audio.is_file():
            raise ValueError(f"Reference {source_member} has an invalid WAV path")
        try:
            with wave.open(str(audio), "rb") as wav:
                if wav.getnframes() < 1:
                    raise ValueError("empty WAV")
        except (wave.Error, EOFError, ValueError) as exc:
            raise ValueError(f"Reference {source_member} has an unreadable WAV") from exc
        # A stable source ID survives a fresh Pack export whose numbered audio filenames change.
        import hashlib
        ref_id = f"{source_slug}-{hashlib.sha256(source_member.encode()).hexdigest()[:12]}"
        target = reference_dir / voice_id / f"{ref_id}.wav"
        intensity = entry.get("intensity")
        if intensity is not None:
            intensity = float(intensity)
            if not 0 <= intensity <= 1:
                raise ValueError(f"Reference {source_member} has invalid intensity")
        prepared.append((audio, target, ref_id, {
            "name": str(entry.get("filename") or source_member),
            "audio": str(target.resolve()),
            "text": text,
            "language": language,
            "emotion": str(entry.get("emotion") or "unmarked"),
            "intensity": intensity,
            "quality": str(entry.get("quality") or "unrated"),
            "source_project": source_key,
            "source_member_id": source_member,
        }))
    for audio, target, _, _ in prepared:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audio, target)
    references = dict(original["references"])
    for _, _, ref_id, record in prepared:
        references[ref_id] = record
    updated = dict(original, references=references)
    normalize_profile(updated)
    staging = profile_path.with_suffix(".json.tmp")
    staging.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staging.replace(profile_path)
    return {"voice": voice_id, "source": source_key, "imported": len(prepared),
            "profile": str(profile_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Import HSR Reference Pack into an existing CVS character")
    parser.add_argument("pack", type=Path)
    parser.add_argument("voice_id")
    args = parser.parse_args()
    try:
        print(json.dumps(import_reference_pack(args.pack, args.voice_id), ensure_ascii=False))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Import failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
