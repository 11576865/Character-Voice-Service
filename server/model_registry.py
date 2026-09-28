"""Persistent local model revisions and candidate/default/retired lifecycle."""

import json
import hashlib
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


LIFECYCLES = {"candidate", "default", "retired"}
AVAILABILITIES = {"unknown", "available", "missing", "invalid", "engine_unavailable"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def empty_registry() -> dict:
    return {"schema_version": 1, "revisions": {}, "defaults": {}, "events": []}


class ModelRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return empty_registry()
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            self._validate_registry(value)
            return value

    @staticmethod
    def _validate_registry(value: dict) -> None:
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("Unsupported model registry schema")
        if not all(isinstance(value.get(key), expected) for key, expected in
                   (("revisions", dict), ("defaults", dict), ("events", list))):
            raise ValueError("Invalid model registry structure")
        seen_defaults = set()
        for revision_id, revision in value["revisions"].items():
            if revision_id != revision.get("revision_id") or revision.get("lifecycle") not in LIFECYCLES:
                raise ValueError(f"Invalid registry revision {revision_id}")
            if revision.get("availability") not in AVAILABILITIES:
                raise ValueError(f"Invalid availability for {revision_id}")
            if revision["lifecycle"] == "default":
                scope = revision.get("scope")
                if not scope or scope in seen_defaults or value["defaults"].get(scope) != revision_id:
                    raise ValueError(f"Inconsistent default revision {revision_id}")
                seen_defaults.add(scope)
        if set(value["defaults"]) != seen_defaults:
            raise ValueError("Default index does not match revision lifecycles")

    def _write_event(self, registry: dict, action: str, revision_id: str, **details) -> None:
        registry["events"].append({"event_id": "event-" + uuid.uuid4().hex,
                                   "created_at": now_iso(), "action": action,
                                   "revision_id": revision_id, **details})

    def import_scan(self, report: dict) -> dict:
        if report.get("mode") != "sha256":
            raise ValueError("Candidate registration requires a full SHA-256 scan")
        with self.lock:
            registry = self.read()
            added, existing = [], []
            discovery_assignments = {}
            for revision in registry["revisions"].values():
                if revision.get("character_id"):
                    discovery_assignments[revision["discovery_key"]] = {
                        "character_id": revision["character_id"], "model_id": revision.get("model_id"),
                        "target_language": revision.get("target_language") or ""}
            for model in report.get("models", []):
                revision_id = model.get("revision_id")
                if model.get("status") != "paired" or not revision_id:
                    continue
                if revision_id in registry["revisions"]:
                    existing.append(revision_id)
                    continue
                matches = model.get("profile_matches") or []
                discovery_key = model.get("discovery_key") or f'{model["root_id"]}:{model["layout"]}:{model["name"]}'
                assignment = None
                declared = model.get("declared_assignment")
                if declared:
                    assignment = {"character_id": declared["character_id"],
                                  "model_id": declared["model_id"],
                                  "target_language": declared.get("target_language") or ""}
                elif len(matches) == 1:
                    assignment = {"character_id": matches[0]["character_id"],
                                  "model_id": matches[0]["model_id"],
                                  "target_language": matches[0].get("target_language") or ""}
                elif discovery_key in discovery_assignments:
                    assignment = discovery_assignments[discovery_key]
                scope = self.scope(assignment["character_id"], assignment["target_language"]) if assignment else None
                bootstrapped = bool(assignment and matches and matches[0].get("is_default") and scope not in registry["defaults"])
                item = {"revision_id": revision_id, "engine": "gpt-sovits",
                        "root_id": model["root_id"], "layout": model["layout"], "name": model["name"],
                        "discovery_key": discovery_key, "artifacts": deepcopy(model["artifacts"]),
                        "character_id": assignment["character_id"] if assignment else None,
                        "model_id": assignment["model_id"] if assignment else None,
                        "target_language": assignment["target_language"] if assignment else "",
                        "profile_model_id": assignment["model_id"] if bootstrapped else None,
                        "scope": scope, "lifecycle": "default" if bootstrapped else "candidate",
                        "availability": "available", "discovered_at": report.get("created_at"),
                        "registered_at": now_iso()}
                registry["revisions"][revision_id] = item
                if bootstrapped:
                    registry["defaults"][scope] = revision_id
                self._write_event(registry, "bootstrap_default" if bootstrapped else "register_candidate",
                                  revision_id, source="scan")
                added.append(revision_id)
            self._validate_registry(registry)
            atomic_json(self.path, registry)
            return {"added": added, "existing": existing, "registry": registry}

    @staticmethod
    def scope(character_id: str, target_language: str = "") -> str:
        if not ID_RE.fullmatch(str(character_id or "")):
            raise ValueError("Invalid character ID")
        language = str(target_language or "").strip().lower() or "und"
        if not re.fullmatch(r"[a-z0-9-]{2,16}", language):
            raise ValueError("Invalid target language")
        return f"{character_id}:{language}"

    def assign(self, revision_id: str, character_id: str, model_id: str,
               target_language: str = "", *, reason: str) -> dict:
        if not ID_RE.fullmatch(str(model_id or "")) or not str(reason).strip():
            raise ValueError("A stable model ID and reason are required")
        with self.lock:
            registry = self.read()
            revision = registry["revisions"].get(revision_id)
            if not revision:
                raise KeyError(revision_id)
            if revision["lifecycle"] == "default":
                raise ValueError("A default revision cannot be reassigned")
            revision.update(character_id=character_id, model_id=model_id,
                            target_language=str(target_language).strip().lower(),
                            scope=self.scope(character_id, target_language))
            self._write_event(registry, "assign", revision_id, reason=str(reason).strip(),
                              character_id=character_id, model_id=model_id)
            self._validate_registry(registry)
            atomic_json(self.path, registry)
            return deepcopy(revision)

    def promote(self, revision_id: str, *, reason: str, evaluation_id: str | None = None,
                profile_model_id: str | None = None) -> dict:
        if not str(reason).strip():
            raise ValueError("Promotion reason is required")
        with self.lock:
            registry = self.read()
            revision = registry["revisions"].get(revision_id)
            if not revision:
                raise KeyError(revision_id)
            if not revision.get("scope") or revision["availability"] != "available":
                raise ValueError("Revision must be assigned and available before promotion")
            previous_id = registry["defaults"].get(revision["scope"])
            if previous_id == revision_id:
                return {"revision": deepcopy(revision), "previous_revision_id": previous_id, "changed": False}
            if previous_id:
                registry["revisions"][previous_id]["lifecycle"] = "retired"
                registry["revisions"][previous_id]["retired_at"] = now_iso()
            revision["lifecycle"] = "default"
            if profile_model_id:
                revision["profile_model_id"] = profile_model_id
            revision["promoted_at"] = now_iso()
            revision.pop("retired_at", None)
            registry["defaults"][revision["scope"]] = revision_id
            self._write_event(registry, "promote", revision_id, reason=str(reason).strip(),
                              evaluation_id=evaluation_id, previous_revision_id=previous_id)
            self._validate_registry(registry)
            atomic_json(self.path, registry)
            return {"revision": deepcopy(revision), "previous_revision_id": previous_id, "changed": True}

    def retire(self, revision_id: str, *, reason: str) -> dict:
        if not str(reason).strip():
            raise ValueError("Retirement reason is required")
        with self.lock:
            registry = self.read()
            revision = registry["revisions"].get(revision_id)
            if not revision:
                raise KeyError(revision_id)
            if revision["lifecycle"] == "default":
                raise ValueError("Promote a replacement before retiring the current default")
            revision["lifecycle"] = "retired"
            revision["retired_at"] = now_iso()
            self._write_event(registry, "retire", revision_id, reason=str(reason).strip())
            self._validate_registry(registry)
            atomic_json(self.path, registry)
            return deepcopy(revision)

    def mark_availability(self, revision_id: str, availability: str) -> None:
        if availability not in AVAILABILITIES:
            raise ValueError("Invalid availability")
        with self.lock:
            registry = self.read()
            if revision_id not in registry["revisions"]:
                raise KeyError(revision_id)
            registry["revisions"][revision_id]["availability"] = availability
            registry["revisions"][revision_id]["availability_checked_at"] = now_iso()
            self._write_event(registry, "availability", revision_id, availability=availability)
            self._validate_registry(registry)
            atomic_json(self.path, registry)

    @staticmethod
    def resolve_artifacts(revision: dict, roots: list[dict]) -> dict[str, Path]:
        root_config = next((item for item in roots if item["id"] == revision["root_id"]), None)
        if not root_config:
            raise ValueError("Model root is not configured")
        root = Path(root_config["path"]).resolve()
        result = {}
        for artifact in revision["artifacts"]:
            path = (root / artifact["relative_path"]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(f'Model artifact unavailable: {artifact["relative_path"]}')
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != artifact.get("sha256"):
                raise ValueError(f'Model artifact content changed: {artifact["relative_path"]}')
            result[artifact["kind"]] = path
        if set(result) != {"gpt", "sovits"}:
            raise ValueError("GPT-SoVITS revision needs GPT and SoVITS artifacts")
        return result

