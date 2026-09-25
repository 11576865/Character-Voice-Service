"""Import character folders containing reference_audios/<language>/emotions/*.wav."""

import argparse
import hashlib
import json
import re
import shutil
import wave
from pathlib import Path

from server.config import PROJECT_ROOT, VOICE_DIR
from server.voice_profiles import normalize_profile


EMOTIONS = {
    "中立": "neutral", "开心": "happy", "高兴": "happy", "难过": "sad",
    "悲伤": "sad", "生气": "angry", "愤怒": "angry", "恐惧": "fear",
    "害怕": "fear", "吃惊": "surprised", "惊讶": "surprised",
    "厌恶": "disgust", "其他": "other",
}
LANGUAGES = {"英语": "en", "中文": "zh", "日语": "ja", "English": "en", "Chinese": "zh", "Japanese": "ja"}
NAME_RE = re.compile(r"^【([^】]+)】\s*(.+)$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_ROLE_IDS = {
    "三月七": "march-7th", "开拓者(女)": "trailblazer-female",
    "砂金": "aventurine", "银狼": "silver-wolf",
}


def _role_id(name: str, source_root: Path, role_dir: Path, explicit: dict[str, str]) -> str:
    if name in explicit or name in DEFAULT_ROLE_IDS:
        value = explicit.get(name, DEFAULT_ROLE_IDS.get(name))
    else:
        value = None
        for path in sorted(source_root.glob("*.json")):
            if path.name == "example.json":
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                audio = Path(str(raw.get("reference_audio") or ""))
                if role_dir.name in audio.parts:
                    value = path.stem
                    break
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        if value is None:
            value = name if SAFE_ID.fullmatch(name) else "role-" + hashlib.sha256(name.encode()).hexdigest()[:10]
    if not SAFE_ID.fullmatch(value) or value == "example":
        raise ValueError(f"Invalid role ID for {name}: {value}")
    return value


def _one_weight(folder: Path, pattern: str, role: str) -> Path:
    matches = sorted(folder.glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"{role}: expected one weight in {folder} matching {pattern}, found {len(matches)}")
    return matches[0].resolve()


def _existing_profile(source_root: Path, target: Path) -> dict | None:
    path = target if target.is_file() else source_root / target.name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def plan_import(source_root: Path, model_root: Path, *, voice_dir: Path = VOICE_DIR,
                reference_dir: Path = PROJECT_ROOT / "references", role_ids: dict[str, str] | None = None) -> list[dict]:
    source_root = source_root.resolve(strict=True)
    model_root = model_root.resolve(strict=True)
    if not source_root.is_dir() or not model_root.is_dir():
        raise ValueError("Source voices and model root must be directories")
    role_ids = role_ids or {}
    result = []
    used_ids = set()
    for role_dir in sorted(source_root.iterdir()):
        if not role_dir.is_dir():
            continue
        wavs = sorted((role_dir / "reference_audios").glob("*/emotions/*.wav"))
        if not wavs:
            result.append({"role": role_dir.name, "status": "skipped: no WAV"})
            continue
        role_id = _role_id(role_dir.name, source_root, role_dir, role_ids)
        if role_id in used_ids:
            raise ValueError(f"Two roles map to {role_id}")
        used_ids.add(role_id)
        gpt = _one_weight(model_root / "GPT_weights_v4", f"{role_dir.name}-e*.ckpt", role_dir.name)
        sovits = _one_weight(model_root / "SoVITS_weights_v4", f"{role_dir.name}_e*.pth", role_dir.name)
        target_profile = voice_dir / f"{role_id}.json"
        original = _existing_profile(source_root, target_profile)
        if original is None:
            profile = {"schema_version": 2, "name": role_dir.name, "target_language": "en",
                       "default_model": "v4-local", "models": {}, "default_reference": "", "references": {}}
        elif "models" not in original and "references" not in original:
            legacy = normalize_profile(original)
            old_reference_exists = Path(str(original["reference_audio"])).is_file()
            profile = {"schema_version": 2, "name": legacy["name"], "target_language": legacy["target_language"],
                       "parameters": legacy["parameters"], "default_model": "v4-local",
                       "models": {"loaded": {"name": "Previously loaded model", "engine": "gpt-sovits"}},
                       "default_reference": "default" if old_reference_exists else "", "references": {"default": {
                           "name": "Previous default", "audio": original["reference_audio"],
                           "text": original["reference_text"], "language": original["reference_language"]}}
                       if old_reference_exists else {}}
        else:
            profile = dict(original)
            profile["models"] = dict(original["models"])
            profile["references"] = dict(original["references"])
        profile["models"]["v4-local"] = {"name": "Local v4", "engine": "gpt-sovits", "version": "v4",
                                           "gpt_weights": str(gpt), "sovits_weights": str(sovits)}
        refs = []
        for audio in wavs:
            language = LANGUAGES.get(audio.parent.parent.name)
            match = NAME_RE.fullmatch(audio.stem)
            if not language or not match or not match.group(2).strip():
                raise ValueError(f"Cannot read language, emotion or text from {audio}")
            try:
                with wave.open(str(audio), "rb") as wav:
                    if wav.getnframes() < 1:
                        raise ValueError("empty WAV")
            except (wave.Error, EOFError, ValueError) as exc:
                raise ValueError(f"Unreadable WAV: {audio}") from exc
            emotion = EMOTIONS.get(match.group(1), match.group(1))
            ref_id = "folder-" + hashlib.sha256(str(audio.relative_to(role_dir)).encode("utf-8")).hexdigest()[:12]
            target = reference_dir / role_id / f"{ref_id}.wav"
            record = {"name": audio.stem, "audio": str(target.resolve()), "text": match.group(2).strip(),
                      "language": language, "emotion": emotion, "quality": "unrated",
                      "source_project": str(source_root), "source_member_id": str(audio.relative_to(role_dir))}
            refs.append((audio, target, ref_id, record))
            profile["references"][ref_id] = record
        if not profile["default_reference"]:
            neutral = next((ref_id for _, _, ref_id, record in refs if record["emotion"] == "neutral"), None)
            profile["default_reference"] = neutral or refs[0][2]
        normalize_profile(profile)
        result.append({"role": role_dir.name, "id": role_id, "status": "ready", "count": len(refs),
                       "profile": target_profile, "data": profile, "files": refs})
    return result


def apply_import(plan: list[dict]) -> list[dict]:
    summary = []
    for item in plan:
        if item["status"] != "ready":
            summary.append({"role": item["role"], "status": item["status"]})
            continue
        for audio, target, _, _ in item["files"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            if audio.resolve() != target.resolve():
                shutil.copy2(audio, target)
        path = item["profile"]
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(".json.tmp")
        staging.write_text(json.dumps(item["data"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.replace(path)
        summary.append({"role": item["role"], "id": item["id"], "references": item["count"],
                        "profile": str(path)})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Import character folders with bracketed-emotion WAV filenames")
    parser.add_argument("source_voices", type=Path)
    parser.add_argument("model_root", type=Path, help="GPT-SoVITS directory containing GPT_weights_v4 and SoVITS_weights_v4")
    parser.add_argument("--role-id", action="append", default=[], metavar="NAME=ID")
    parser.add_argument("--apply", action="store_true", help="Write files; without this, only show the plan")
    args = parser.parse_args()
    try:
        mapping = dict(part.split("=", 1) for part in args.role_id)
        plan = plan_import(args.source_voices, args.model_root, role_ids=mapping)
        output = apply_import(plan) if args.apply else [
            {k: v for k, v in item.items() if k in {"role", "id", "status", "count"}} for item in plan]
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Import failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
