"""Coordinate scanning, registry lifecycle, Evaluations, and profile activation."""

import re
import threading
from copy import deepcopy
from pathlib import Path

from server.evaluations import EvaluationStore
from server.model_registry import ModelRegistry, atomic_json
from server.model_scan import read_roots, scan


class ModelManager:
    def __init__(self, root: Path, voice_dir: Path):
        self.root = root
        self.voice_dir = voice_dir
        self.roots_path = root / "roots.json"
        self.scan_path = root / "scan-report.json"
        self.registry = ModelRegistry(root / "registry.json")
        self.evaluations = EvaluationStore(root / "evaluations")
        self.lock = threading.RLock()

    def scan(self, *, full_hash: bool, register: bool = False) -> dict:
        with self.lock:
            report = scan(read_roots(self.roots_path), self.voice_dir, full_hash=full_hash)
            atomic_json(self.scan_path, report)
            registration = self.registry.import_scan(report) if register else None
            return {"report": report, "registration": registration}

    def assign(self, revision_id: str, character_id: str, model_id: str,
               target_language: str, reason: str) -> dict:
        profile = self._profile(character_id)
        language = target_language or str(profile.get("target_language") or "")
        return self.registry.assign(revision_id, character_id, model_id, language, reason=reason)

    def promote(self, revision_id: str, *, reason: str,
                evaluation_id: str | None = None, allow_without_evaluation: bool = False) -> dict:
        with self.lock:
            before = self.registry.read()
            revision = before["revisions"].get(revision_id)
            if not revision:
                raise KeyError(revision_id)
            if evaluation_id:
                evaluation = self.evaluations.get(evaluation_id)
                if evaluation["candidate_revision_id"] != revision_id or \
                        evaluation["decision"]["value"] != "promote":
                    raise ValueError("Evaluation must recommend promotion for this candidate")
            elif not allow_without_evaluation:
                raise ValueError("Promotion requires an accepted Evaluation or explicit manual override")
            character_id = revision.get("character_id")
            profile = self._profile(character_id)
            if revision["engine"] != "gpt-sovits":
                raise ValueError("Production activation currently supports only gpt-sovits")
            artifacts = ModelRegistry.resolve_artifacts(revision, read_roots(self.roots_path))
            profile_model_id = revision.get("profile_model_id") or self._profile_model_id(
                revision.get("model_id") or revision["layout"], revision_id)
            models = profile.get("models")
            if not isinstance(models, dict):
                raise ValueError("Character profile must use schema v2 before model promotion")
            updated_profile = deepcopy(profile)
            updated_profile["models"][profile_model_id] = {
                "name": f'{revision["name"]} · {revision_id[-8:]}', "engine": "gpt-sovits",
                "version": revision["layout"], "revision_id": revision_id,
                "gpt_weights": str(artifacts["gpt"]), "sovits_weights": str(artifacts["sovits"])}
            updated_profile["default_model"] = profile_model_id
            profile_path = self.voice_dir / f"{character_id}.json"
            try:
                result = self.registry.promote(revision_id, reason=reason, evaluation_id=evaluation_id,
                                               profile_model_id=profile_model_id)
                atomic_json(profile_path, updated_profile)
            except Exception:
                atomic_json(self.registry.path, before)
                raise
            return {**result, "profile_model_id": profile_model_id,
                    "production_applied": True}

    def retire(self, revision_id: str, *, reason: str) -> dict:
        with self.lock:
            return self.registry.retire(revision_id, reason=reason)

    def _profile(self, character_id: str | None) -> dict:
        if not character_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", character_id):
            raise ValueError("Candidate must be assigned to a valid character")
        path = self.voice_dir / f"{character_id}.json"
        if not path.is_file():
            raise ValueError("Character profile does not exist")
        import json
        return json.loads(path.read_text(encoding="utf-8-sig"))

    @staticmethod
    def _profile_model_id(model_id: str, revision_id: str) -> str:
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", str(model_id)).strip(".-_") or "model"
        return f"{base[:48]}-r-{revision_id[-8:]}"

