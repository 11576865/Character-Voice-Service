import json
import re
from pathlib import Path

from server.model_registry import default_model_id, resolve_model
from server.voice_bindings import list_bindings, resolve_binding


TEMPLATE_FILENAME = "example.json"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LEGACY_REQUIRED_FIELDS = (
    "reference_audio",
    "reference_text",
    "reference_language",
    "target_language",
)


def iter_real_profile_paths(voice_dir: Path):
    """Yield real character profiles, excluding the checked-in example template."""
    if not voice_dir.exists():
        return
    for path in sorted(voice_dir.glob("*.json")):
        if path.name.casefold() != TEMPLATE_FILENAME:
            yield path


def _require_id(value: object, *, field: str) -> str:
    item = str(value or "").strip()
    if not _ID_RE.fullmatch(item):
        raise ValueError(f"{field} must be a stable ID using letters, numbers, '.', '_' or '-'")
    return item


def _normalize_model(model_id: str, raw: object) -> dict:
    model_id = _require_id(model_id, field="model id")
    if not isinstance(raw, dict):
        raise ValueError(f"model {model_id} must be an object")

    registry_model_id = str(raw.get("model_id") or "").strip() or None
    explicit_gpt = str(raw.get("gpt_weights") or "").strip()
    explicit_sovits = str(raw.get("sovits_weights") or "").strip()

    if registry_model_id and (explicit_gpt or explicit_sovits):
        raise ValueError(
            f"model {model_id}: use model_id or explicit weight paths, not both"
        )

    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError(f"model {model_id}: parameters must be an object")

    if registry_model_id:
        resolved = resolve_model(registry_model_id)
        engine = resolved["engine"]["name"]
        engine_version = resolved["engine"]["engine_version"]
        effective_parameters = dict(resolved.get("parameters") or {})
        effective_parameters.update(parameters)

        gpt_weights = None
        sovits_weights = None
        if engine == "gpt-sovits":
            artifacts = resolved.get("artifacts") or {}
            gpt_weights = artifacts.get("gpt")
            sovits_weights = artifacts.get("sovits")
            if not gpt_weights or not sovits_weights:
                raise ValueError(
                    f"model {model_id}: GPT-SoVITS manifest requires gpt and sovits artifacts"
                )

        return {
            "id": model_id,
            "model_id": registry_model_id,
            "name": str(raw.get("name") or resolved.get("name") or model_id).strip() or model_id,
            "engine": engine,
            "version": str(raw.get("version") or engine_version).strip(),
            "revision": resolved.get("revision"),
            "scope": resolved.get("scope", "voice-bound"),
            "adapter_api_version": resolved.get("engine", {}).get("adapter_api_version", "1"),
            "status": resolved.get("status"),
            "gpt_weights": gpt_weights,
            "sovits_weights": sovits_weights,
            "managed": True,
            "parameters": effective_parameters,
        }

    engine = str(raw.get("engine") or "gpt-sovits").strip().lower()
    if engine not in {"gpt-sovits", "index-tts"}:
        raise ValueError(f"model {model_id}: unsupported engine {engine!r}")

    if engine == "index-tts" and (explicit_gpt or explicit_sovits):
        raise ValueError(
            f"model {model_id}: IndexTTS shared runtime does not accept GPT/SoVITS weight paths"
        )

    if engine == "gpt-sovits" and bool(explicit_gpt) != bool(explicit_sovits):
        raise ValueError(
            f"model {model_id}: gpt_weights and sovits_weights must be configured together"
        )

    return {
        "id": model_id,
        "model_id": None,
        "name": str(raw.get("name") or model_id).strip() or model_id,
        "engine": engine,
        "version": str(raw.get("version") or "").strip(),
        "revision": None,
        "scope": "shared" if engine == "index-tts" else "voice-bound",
        "adapter_api_version": "1",
        "status": None,
        "gpt_weights": explicit_gpt or None,
        "sovits_weights": explicit_sovits or None,
        "managed": engine == "gpt-sovits" and bool(explicit_gpt and explicit_sovits),
        "parameters": dict(parameters),
    }


def _normalize_reference(reference_id: str, raw: object) -> dict:
    reference_id = _require_id(reference_id, field="reference id")
    if not isinstance(raw, dict):
        raise ValueError(f"reference {reference_id} must be an object")

    audio = str(raw.get("audio") or raw.get("reference_audio") or "").strip()
    text = str(raw.get("text") or raw.get("reference_text") or "").strip()
    language = str(raw.get("language") or raw.get("reference_language") or "").strip()
    missing = [
        field
        for field, value in (("audio", audio), ("text", text), ("language", language))
        if not value
    ]
    if missing:
        raise ValueError(f"reference {reference_id}: missing fields: {', '.join(missing)}")

    aux = raw.get("aux_audio")
    if aux is None:
        aux = raw.get("aux_reference_audio", [])
    if not isinstance(aux, list) or any(not isinstance(item, str) for item in aux):
        raise ValueError(f"reference {reference_id}: aux_audio must be a string list")

    intensity = raw.get("intensity")
    if intensity is not None:
        try:
            intensity = float(intensity)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"reference {reference_id}: intensity must be numeric") from exc
        if not 0.0 <= intensity <= 1.0:
            raise ValueError(f"reference {reference_id}: intensity must be between 0 and 1")

    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError(f"reference {reference_id}: parameters must be an object")

    roles = raw.get("roles")
    if roles is None:
        role = str(raw.get("role") or "").strip()
        roles = [role] if role else ["speaker"]
    if not isinstance(roles, list) or not roles or any(
        not isinstance(item, str) or not item.strip() for item in roles
    ):
        raise ValueError(f"reference {reference_id}: roles must be a non-empty string list")
    roles = sorted({item.strip().lower() for item in roles})

    return {
        "id": reference_id,
        "name": str(raw.get("name") or reference_id).strip() or reference_id,
        "audio": audio,
        "text": text,
        "language": language,
        "aux_audio": list(aux),
        "emotion": str(raw.get("emotion") or "").strip(),
        "intensity": intensity,
        "quality": str(raw.get("quality") or "").strip(),
        "roles": roles,
        "parameters": dict(parameters),
    }


def _normalize_legacy_profile(raw: dict) -> dict:
    missing = [field for field in LEGACY_REQUIRED_FIELDS if not raw.get(field)]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")

    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")

    reference = _normalize_reference(
        "default",
        {
            "name": "Default",
            "audio": raw["reference_audio"],
            "text": raw["reference_text"],
            "language": raw["reference_language"],
            "aux_audio": raw.get("aux_reference_audio", []),
        },
    )
    model = _normalize_model(
        "loaded",
        {
            "name": "Externally loaded GPT-SoVITS model",
            "engine": "gpt-sovits",
        },
    )
    return {
        "schema_version": 1,
        "name": str(raw.get("name") or "").strip() or "Unnamed Character",
        "target_language": str(raw["target_language"]).strip(),
        "parameters": dict(parameters),
        "default_model": "loaded",
        "models": {"loaded": model},
        "default_reference": "default",
        "references": {"default": reference},
    }


def normalize_profile(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("profile root must be an object")

    if "models" not in raw and "references" not in raw:
        return _normalize_legacy_profile(raw)

    name = str(raw.get("name") or "").strip()
    target_language = str(raw.get("target_language") or "").strip()
    if not name:
        raise ValueError("missing field: name")
    if not target_language:
        raise ValueError("missing field: target_language")

    raw_models = raw.get("models")
    raw_references = raw.get("references")
    if not isinstance(raw_models, dict) or not raw_models:
        raise ValueError("models must be a non-empty object")
    if not isinstance(raw_references, dict) or not raw_references:
        raise ValueError("references must be a non-empty object")

    models = {str(key): _normalize_model(str(key), value) for key, value in raw_models.items()}
    references = {
        str(key): _normalize_reference(str(key), value)
        for key, value in raw_references.items()
    }

    default_model = _require_id(raw.get("default_model"), field="default_model")
    default_reference = _require_id(raw.get("default_reference"), field="default_reference")
    if default_model not in models:
        raise ValueError(f"default_model not found in models: {default_model}")
    if default_reference not in references:
        raise ValueError(f"default_reference not found in references: {default_reference}")

    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")

    return {
        "schema_version": 2,
        "name": name,
        "target_language": target_language,
        "parameters": dict(parameters),
        "default_model": default_model,
        "models": models,
        "default_reference": default_reference,
        "references": references,
    }


def read_valid_profile(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        profile = normalize_profile(json.load(handle))
    profile["_voice_id"] = path.stem
    return profile


def resolve_profile_selection(
    profile: dict,
    *,
    model_id: str | None = None,
    reference_id: str | None = None,
) -> dict:
    voice_id = str(profile.get("_voice_id") or "").strip() or None
    selected_model_id = model_id
    if selected_model_id is None:
        registry_default = None
        if voice_id:
            try:
                registry_default = default_model_id(voice_id)
            except (OSError, ValueError, json.JSONDecodeError):
                registry_default = None
        if registry_default:
            selected_model_id = next(
                (
                    alias
                    for alias, item in profile["models"].items()
                    if item.get("model_id") == registry_default
                ),
                None,
            )
        selected_model_id = selected_model_id or profile["default_model"]

    binding = None
    if voice_id and selected_model_id:
        try:
            binding = resolve_binding(voice_id, selector=str(selected_model_id))
        except (OSError, json.JSONDecodeError):
            binding = None

    if binding is not None:
        resolved = resolve_model(binding["model_id"])
        engine = resolved["engine"]["name"]
        artifacts = resolved.get("artifacts") or {}
        model = {
            "id": selected_model_id,
            "model_id": resolved["model_id"],
            "name": resolved["name"],
            "engine": engine,
            "version": resolved["engine"]["engine_version"],
            "revision": resolved["revision"],
            "scope": resolved.get("scope", "shared"),
            "adapter_api_version": resolved["engine"].get("adapter_api_version", "1"),
            "status": resolved.get("status"),
            "gpt_weights": artifacts.get("gpt") if engine == "gpt-sovits" else None,
            "sovits_weights": artifacts.get("sovits") if engine == "gpt-sovits" else None,
            "managed": True,
            "parameters": dict(resolved.get("parameters") or {}),
        }
        selected_reference_id = reference_id or binding["speaker_reference_id"]
    else:
        if selected_model_id not in profile["models"]:
            raise KeyError(f"model not found: {selected_model_id}")
        model = profile["models"][selected_model_id]
        selected_reference_id = reference_id or profile["default_reference"]

    if selected_reference_id not in profile["references"]:
        raise KeyError(f"reference not found: {selected_reference_id}")

    reference = profile["references"][selected_reference_id]
    emotion_reference = None
    parameters = dict(profile.get("parameters") or {})
    parameters.update(model.get("parameters") or {})

    if binding is not None:
        parameters.update(binding.get("parameters") or {})
        emotion_reference_id = binding.get("emotion_reference_id")
        if emotion_reference_id:
            if emotion_reference_id not in profile["references"]:
                raise KeyError(f"reference not found: {emotion_reference_id}")
            emotion_reference = profile["references"][emotion_reference_id]
        if binding.get("emotion_policy") == "separate" and emotion_reference is not None:
            parameters["emotion_audio"] = emotion_reference["audio"]

    parameters.update(reference.get("parameters") or {})

    return {
        "name": profile["name"],
        "target_language": profile["target_language"],
        "parameters": parameters,
        "reference_audio": reference["audio"],
        "reference_text": reference["text"],
        "reference_language": reference["language"],
        "aux_reference_audio": list(reference.get("aux_audio") or []),
        "selected_model": dict(model),
        "selected_reference": dict(reference),
        "selected_emotion_reference": dict(emotion_reference) if emotion_reference else None,
        "selected_binding": dict(binding) if binding else None,
    }


def public_profile_summary(voice_id: str, profile: dict) -> dict:
    effective_default = profile["default_model"]
    try:
        bindings = list_bindings(voice_id)
    except (OSError, ValueError, json.JSONDecodeError):
        bindings = []
    try:
        registry_default = default_model_id(voice_id)
    except (OSError, ValueError, json.JSONDecodeError):
        registry_default = None
    if registry_default:
        effective_default = next(
            (
                alias
                for alias, item in profile["models"].items()
                if item.get("model_id") == registry_default
            ),
            effective_default,
        )

    return {
        "id": voice_id,
        "name": profile["name"],
        "schema_version": profile["schema_version"],
        "reference_language": profile["references"][profile["default_reference"]]["language"],
        "target_language": profile["target_language"],
        "default_model": effective_default,
        "models": [
            {
                "id": model["id"],
                "name": model["name"],
                "engine": model["engine"],
                "version": model["version"],
                "managed": model["managed"],
                "model_id": model.get("model_id"),
                "revision": model.get("revision"),
                "scope": model.get("scope", "voice-bound"),
                "status": model.get("status"),
            }
            for model in profile["models"].values()
        ],
        "bindings": [
            {
                "id": binding["binding_id"],
                "engine": binding["engine"],
                "model_id": binding["model_id"],
                "speaker_reference_id": binding["speaker_reference_id"],
                "emotion_reference_id": binding.get("emotion_reference_id"),
                "emotion_policy": binding["emotion_policy"],
                "revision": binding["revision"],
            }
            for binding in bindings
        ],
        "default_reference": profile["default_reference"],
        "references": [
            {
                "id": reference["id"],
                "name": reference["name"],
                "language": reference["language"],
                "emotion": reference["emotion"],
                "intensity": reference["intensity"],
                "quality": reference["quality"],
                "roles": reference.get("roles", ["speaker"]),
            }
            for reference in profile["references"].values()
        ],
    }


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
            "ERROR: No valid real character profile was found in voices/.\n"
            "Copy voices\\example.json to a stable character ID such as "
            "voices\\march-7th.json, then register at least one model and "
            "one reference audio."
        )
        return 2

    ids = ", ".join(path.stem for path, _ in profiles)
    print(f"Character registry check passed: {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
