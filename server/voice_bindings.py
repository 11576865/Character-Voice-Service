import hashlib
import json
from pathlib import Path

from server.config import DATA_DIR


BINDING_PATH = DATA_DIR / "voice-bindings.json"
SCHEMA_VERSION = 1
_ALLOWED_EMOTION_POLICIES = {"speaker", "separate", "vector", "none"}


def _canonical_revision(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _normalize_binding(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("voice binding must be an object")

    required = ("binding_id", "voice_id", "engine", "model_id", "speaker_reference_id")
    values = {field: str(raw.get(field) or "").strip() for field in required}
    missing = [field for field, value in values.items() if not value]
    if missing:
        raise ValueError(f"voice binding missing fields: {', '.join(missing)}")

    emotion_reference_id = str(raw.get("emotion_reference_id") or "").strip() or None
    emotion_policy = str(raw.get("emotion_policy") or "speaker").strip().lower()
    if emotion_policy not in _ALLOWED_EMOTION_POLICIES:
        raise ValueError(f"unsupported emotion_policy: {emotion_policy}")

    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError("voice binding parameters must be an object")

    normalized = {
        **values,
        "emotion_reference_id": emotion_reference_id,
        "emotion_policy": emotion_policy,
        "parameters": dict(parameters),
        "enabled": bool(raw.get("enabled", True)),
    }
    normalized["revision"] = _canonical_revision(normalized)
    return normalized


def load_bindings(path: Path = BINDING_PATH) -> dict:
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "bindings": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported voice binding schema")
    raw_bindings = data.get("bindings")
    if not isinstance(raw_bindings, dict):
        raise ValueError("voice bindings must be an object")
    bindings = {
        binding_id: _normalize_binding({**raw, "binding_id": binding_id})
        for binding_id, raw in raw_bindings.items()
    }
    return {"schema_version": SCHEMA_VERSION, "bindings": bindings}


def save_bindings(data: dict, path: Path = BINDING_PATH) -> None:
    serializable = {"schema_version": SCHEMA_VERSION, "bindings": {}}
    for binding_id, raw in data.get("bindings", {}).items():
        binding = _normalize_binding({**raw, "binding_id": binding_id})
        stored = dict(binding)
        stored.pop("revision", None)
        stored.pop("binding_id", None)
        serializable["bindings"][binding_id] = stored
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".tmp")
    staging.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    staging.replace(path)


def register_binding(binding: dict, path: Path = BINDING_PATH) -> dict:
    normalized = _normalize_binding(binding)
    data = load_bindings(path)
    data["bindings"][normalized["binding_id"]] = normalized
    save_bindings(data, path)
    return normalized


def list_bindings(voice_id: str | None = None, path: Path = BINDING_PATH) -> list[dict]:
    data = load_bindings(path)
    items = [
        binding
        for binding in data["bindings"].values()
        if binding.get("enabled", True)
        and (voice_id is None or binding["voice_id"] == voice_id)
    ]
    return sorted(items, key=lambda item: item["binding_id"])


def resolve_binding(
    voice_id: str,
    *,
    selector: str,
    path: Path = BINDING_PATH,
) -> dict | None:
    selector = str(selector or "").strip()
    if not selector:
        return None
    matches = [
        binding
        for binding in list_bindings(voice_id, path)
        if selector in {
            binding["binding_id"],
            binding["model_id"],
            binding["engine"],
        }
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(
            f"ambiguous voice binding selector {selector!r} for voice {voice_id!r}"
        )
    return matches[0]
