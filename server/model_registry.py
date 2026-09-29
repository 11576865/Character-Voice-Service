import json
import os
import re
import shutil
from pathlib import Path

from server.config import PROJECT_ROOT


REGISTRY_PATH = PROJECT_ROOT / "data" / "model-registry.json"
_SCHEMA_VERSION = 1
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _model_roots() -> list[Path]:
    roots: list[Path] = []

    configured = os.environ.get("CVS_MODEL_ROOTS", "")
    for item in configured.split(os.pathsep):
        item = item.strip()
        if item:
            roots.append(Path(item))

    configured_gpt = os.environ.get("CVS_GPT_SOVITS_ROOT", "").strip()
    if configured_gpt:
        roots.append(Path(configured_gpt))

    # The normal Windows layout used by this project keeps Character-Voice-Service
    # and GPT-SoVITS next to each other. This is only a discovery root; no profile
    # stores this absolute location.
    roots.append(PROJECT_ROOT.parent / "GPT-SoVITS")

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.expanduser()).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(root.expanduser())
    return unique


def _empty_registry() -> dict:
    return {"schema_version": _SCHEMA_VERSION, "models": {}}


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    if not path.is_file():
        return _empty_registry()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported model registry schema")
    models = data.get("models")
    if not isinstance(models, dict):
        raise ValueError("model registry models must be an object")
    return data


def save_registry(data: dict, path: Path = REGISTRY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".tmp")
    staging.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staging.replace(path)


def make_model_ref(voice_id: str, model_id: str) -> str:
    raw = f"{voice_id}--{model_id}"
    cleaned = _SAFE.sub("-", raw).strip(".-_")
    if not cleaned:
        raise ValueError("cannot derive model registry id")
    return cleaned[:120]


def _candidate_score(old: Path, candidate: Path) -> tuple[int, int]:
    old_parts = [p.casefold() for p in old.parts]
    new_parts = [p.casefold() for p in candidate.parts]
    suffix = 0
    for left, right in zip(reversed(old_parts), reversed(new_parts)):
        if left != right:
            break
        suffix += 1
    # Prefer the longest matching suffix, then the shorter absolute path.
    return suffix, -len(candidate.parts)


def _relocate(stale: str, suffix: str) -> str | None:
    old = Path(stale)
    filename = old.name
    if not filename:
        return None

    candidates: list[Path] = []
    for root in _model_roots():
        if not root.is_dir():
            continue
        try:
            candidates.extend(path for path in root.rglob(filename) if path.is_file() and path.suffix.lower() == suffix)
        except OSError:
            continue

    if not candidates:
        return None

    ranked = sorted(candidates, key=lambda item: _candidate_score(old, item), reverse=True)
    if len(ranked) > 1 and _candidate_score(old, ranked[0]) == _candidate_score(old, ranked[1]):
        raise ValueError(
            f"ambiguous relocated model weight {filename!r}: "
            + ", ".join(str(item) for item in ranked[:4])
        )
    return str(ranked[0].resolve())


def resolve_weight_path(value: str, *, suffix: str) -> tuple[str, bool]:
    path = Path(value)
    if path.is_file():
        return str(path.resolve()), False

    relocated = _relocate(value, suffix)
    if relocated is None:
        raise ValueError(
            f"model weight not found: {value}. "
            "Set CVS_MODEL_ROOTS/CVS_GPT_SOVITS_ROOT or run scripts/migrate_model_registry.cmd."
        )
    return relocated, True


def register_model(
    model_ref: str,
    gpt_weights: str,
    sovits_weights: str,
    *,
    registry_path: Path = REGISTRY_PATH,
) -> dict:
    gpt, _ = resolve_weight_path(gpt_weights, suffix=".ckpt")
    sovits, _ = resolve_weight_path(sovits_weights, suffix=".pth")

    data = load_registry(registry_path)
    data["models"][model_ref] = {
        "gpt_weights": gpt,
        "sovits_weights": sovits,
    }
    save_registry(data, registry_path)
    return dict(data["models"][model_ref])


def resolve_registered_model(
    model_ref: str,
    *,
    registry_path: Path = REGISTRY_PATH,
) -> dict:
    data = load_registry(registry_path)
    raw = data["models"].get(model_ref)
    if not isinstance(raw, dict):
        raise ValueError(f"model registry entry not found: {model_ref}")

    gpt_raw = str(raw.get("gpt_weights") or "").strip()
    sovits_raw = str(raw.get("sovits_weights") or "").strip()
    if not gpt_raw or not sovits_raw:
        raise ValueError(f"model registry entry {model_ref!r} is incomplete")

    gpt, gpt_changed = resolve_weight_path(gpt_raw, suffix=".ckpt")
    sovits, sovits_changed = resolve_weight_path(sovits_raw, suffix=".pth")

    if gpt_changed or sovits_changed:
        raw["gpt_weights"] = gpt
        raw["sovits_weights"] = sovits
        save_registry(data, registry_path)

    return {
        "gpt_weights": gpt,
        "sovits_weights": sovits,
    }


def migrate_profile(
    profile_path: Path,
    *,
    registry_path: Path = REGISTRY_PATH,
    backup: bool = True,
) -> dict:
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    models = raw.get("models")
    if not isinstance(models, dict):
        return {"profile": str(profile_path), "converted": 0, "unchanged": 0}

    converted = 0
    unchanged = 0
    for model_id, model in models.items():
        if not isinstance(model, dict):
            continue
        if model.get("model_ref"):
            unchanged += 1
            continue

        gpt = str(model.get("gpt_weights") or "").strip()
        sovits = str(model.get("sovits_weights") or "").strip()
        if not gpt or not sovits:
            unchanged += 1
            continue

        model_ref = make_model_ref(profile_path.stem, str(model_id))
        register_model(model_ref, gpt, sovits, registry_path=registry_path)
        model["model_ref"] = model_ref
        model.pop("gpt_weights", None)
        model.pop("sovits_weights", None)
        converted += 1

    if converted:
        if backup:
            backup_path = profile_path.with_suffix(".json.pre-model-registry.bak")
            if not backup_path.exists():
                shutil.copy2(profile_path, backup_path)
        staging = profile_path.with_suffix(".json.tmp")
        staging.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.replace(profile_path)

    return {"profile": str(profile_path), "converted": converted, "unchanged": unchanged}
