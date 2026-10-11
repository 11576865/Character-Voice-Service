"""Freeze and verify a curated, engine-neutral ORIGINAL WAV benchmark corpus.

The split is a human-reviewed input, never assigned by this module. No WAV is
copied, resampled, re-transcribed, or modified.
"""

import hashlib
import json
import re
import wave
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 1
SPLITS = frozenset({"train", "dev", "test-recorded", "reference"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a stable ASCII ID")
    return value


def _audio_path(root: Path, name: object) -> Path:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("audio must be a canonical relative POSIX WAV path")
    relative = PurePosixPath(name)
    if (relative.is_absolute() or ".." in relative.parts
            or relative.as_posix() != name or relative.suffix.lower() != ".wav"):
        raise ValueError(f"invalid audio path: {name}")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"audio escapes audio root: {name}")
    if not path.is_file():
        raise ValueError(f"missing original WAV: {name}")
    return path


def _wav_metadata(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getcomptype() != "NONE" or not audio.getnframes():
                raise ValueError("WAV must contain uncompressed, nonempty PCM audio")
            if audio.getframerate() <= 0 or audio.getnchannels() <= 0:
                raise ValueError("WAV sample rate and channel count must be positive")
            return {
                "sample_rate": audio.getframerate(),
                "channels": audio.getnchannels(),
                "frames": audio.getnframes(),
                "duration_sec": round(audio.getnframes() / audio.getframerate(), 6),
            }
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"unreadable PCM WAV: {path.name}: {exc}") from exc


def _record(row: dict, root: Path) -> dict:
    if not isinstance(row, dict):
        raise ValueError("dataset item must be an object")
    item_id = _require_id(row.get("id"), "item id")
    split = row.get("split")
    if split not in SPLITS:
        raise ValueError(f"{item_id}: invalid split {split!r}")
    if row.get("source") != "ORIGINAL":
        raise ValueError(f"{item_id}: only provenance source ORIGINAL is accepted")
    text = row.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{item_id}: human-reviewed text is required")
    language = row.get("language")
    if not isinstance(language, str) or not language.strip():
        raise ValueError(f"{item_id}: language is required")
    reference = row.get("reference", False)
    if not isinstance(reference, bool):
        raise ValueError(f"{item_id}: reference must be a boolean")
    if split == "test-recorded" and reference:
        raise ValueError(f"{item_id}: held-out test WAV cannot also be a reference")
    reference = reference or split == "reference"
    style = row.get("style", "")
    if not isinstance(style, str):
        raise ValueError(f"{item_id}: style must be a string")
    name = row.get("audio")
    path = _audio_path(root, name)
    return {
        "id": item_id,
        "split": split,
        "source": "ORIGINAL",
        "reference": reference,
        "audio": name,
        "audio_sha256": _sha256_file(path),
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "language": language,
        "style": style,
        **_wav_metadata(path),
    }


def _check_uniqueness(items: list[dict]) -> None:
    for field in ("id", "audio", "audio_sha256"):
        seen = {}
        for item in items:
            value = item[field]
            if value in seen:
                raise ValueError(f"duplicate {field}: {item['id']} and {seen[value]}")
            seen[value] = item["id"]


def _text_overlaps(items: list[dict]) -> int:
    texts = defaultdict(set)
    for item in items:
        normalized = " ".join(item["text"].casefold().split())
        texts[normalized].add(item["split"])
    return sum(len(splits) > 1 for splits in texts.values())


def _summary(items: list[dict]) -> dict:
    counts = Counter(item["split"] for item in items)
    return {
        "count": len(items),
        "reference_pool": sum(item["reference"] for item in items),
        "splits": {split: counts[split] for split in sorted(SPLITS)},
        "cross_split_text_overlaps": _text_overlaps(items),
    }


def freeze_dataset(input_jsonl: Path, audio_root: Path, output: Path, dataset_id: str) -> dict:
    """Freeze reviewed JSONL into an immutable content-addressed manifest."""
    _require_id(dataset_id, "dataset id")
    audio_root = audio_root.resolve()
    items = []
    for lineno, line in enumerate(input_jsonl.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            items.append(_record(json.loads(line), audio_root))
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"line {lineno}: {exc}") from exc
    if not items:
        raise ValueError("dataset must contain at least one item")
    _check_uniqueness(items)
    items.sort(key=lambda item: item["id"])
    payload = {"schema_version": SCHEMA_VERSION, "dataset_id": dataset_id, "items": items}
    manifest = {**payload, "dataset_sha256": hashlib.sha256(_canonical(payload)).hexdigest()}
    contents = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8", newline="\n") as target:
            target.write(contents)
    except FileExistsError:
        if output.read_text(encoding="utf-8") != contents:
            raise ValueError(f"frozen dataset already exists with different content: {output}")
    return {"dataset_id": dataset_id, "dataset_sha256": manifest["dataset_sha256"], **_summary(items)}


def verify_dataset(manifest_path: Path, audio_root: Path) -> dict:
    """Verify a frozen manifest and every original WAV without modifying either."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported frozen dataset manifest")
    _require_id(manifest.get("dataset_id"), "dataset id")
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("dataset items must be a nonempty list")
    payload = {"schema_version": SCHEMA_VERSION, "dataset_id": manifest["dataset_id"], "items": items}
    expected = hashlib.sha256(_canonical(payload)).hexdigest()
    if manifest.get("dataset_sha256") != expected:
        raise ValueError("dataset manifest fingerprint mismatch")
    checked = []
    for index, frozen in enumerate(items, 1):
        try:
            actual = _record(frozen, audio_root.resolve())
        except (ValueError, OSError) as exc:
            raise ValueError(f"item {index}: {exc}") from exc
        if frozen != actual:
            raise ValueError(f"item {index}: WAV, transcript or metadata changed: {frozen.get('id')}")
        checked.append(frozen)
    _check_uniqueness(checked)
    if [item["id"] for item in checked] != sorted(item["id"] for item in checked):
        raise ValueError("dataset items are not sorted by stable ID")
    return {"dataset_id": manifest["dataset_id"], "dataset_sha256": expected, **_summary(checked)}
