"""Coordinate scanning, registry lifecycle, Evaluations, and profile activation."""

import json
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
        self.pending_path = root / "pending-transition.json"
        self.recover_pending_transition()

    def recover_pending_transition(self) -> bool:
        if not self.pending_path.is_file():
            return False
        import json
        pending = json.loads(self.pending_path.read_text(encoding="utf-8-sig"))
        character_id = pending.get("character_id")
        if not character_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", character_id):
            raise ValueError("Invalid pending model transition")
        profile_path = self.voice_dir / f"{character_id}.json"
        atomic_json(self.registry.path, pending["before_registry"])
        atomic_json(profile_path, pending["before_profile"])
        self.pending_path.unlink()
        return True

    def scan(self, *, full_hash: bool, register: bool = False) -> dict:
        with self.lock:
            report = scan(read_roots(self.roots_path), self.voice_dir, full_hash=full_hash)
            atomic_json(self.scan_path, report)
            registration = self.registry.import_scan(report) if register else None
            if registration is not None:
                registration["profile_links_updated"] = self.sync_profile_revision_links()
            return {"report": report, "registration": registration}

    def sync_profile_revision_links(self) -> int:
        """Backfill immutable revision IDs into models already used by profiles."""
        registry = self.registry.read()
        changed = 0
        for revision in registry["revisions"].values():
            if revision.get("lifecycle") != "default" or not revision.get("character_id"):
                continue
            model_id = revision.get("profile_model_id") or revision.get("model_id")
            if not model_id:
                continue
            profile_path = self.voice_dir / f'{revision["character_id"]}.json'
            if not profile_path.is_file():
                continue
            profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
            models = profile.get("models")
            if not isinstance(models, dict) or not isinstance(models.get(model_id), dict):
                continue
            if models[model_id].get("revision_id") == revision["revision_id"]:
                continue
            models[model_id]["revision_id"] = revision["revision_id"]
            atomic_json(profile_path, profile)
            changed += 1
        return changed

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
            atomic_json(self.pending_path, {"schema_version": 1, "character_id": character_id,
                                            "before_registry": before,
                                            "before_profile": profile})
            try:
                result = self.registry.promote(revision_id, reason=reason, evaluation_id=evaluation_id,
                                               profile_model_id=profile_model_id)
                atomic_json(profile_path, updated_profile)
            except Exception:
                atomic_json(self.registry.path, before)
                atomic_json(profile_path, profile)
                self.pending_path.unlink(missing_ok=True)
                raise
            self.pending_path.unlink()
            return {**result, "profile_model_id": profile_model_id,
                    "production_applied": True}

    def retire(self, revision_id: str, *, reason: str) -> dict:
        with self.lock:
            return self.registry.retire(revision_id, reason=reason)

    def create_evaluation(self, *, character_id: str, candidate_revision_id: str,
                          baseline_revision_id: str | None, sample_set: dict,
                          engine: dict, reference_set_id: str = "",
                          requested_parameters: dict | None = None) -> dict:
        registry = self.registry.read()
        candidate = registry["revisions"].get(candidate_revision_id)
        baseline = registry["revisions"].get(baseline_revision_id) if baseline_revision_id else None
        if not candidate or candidate.get("character_id") != character_id:
            raise ValueError("Candidate is not registered for this character")
        if candidate["lifecycle"] != "candidate":
            raise ValueError("Evaluation candidate must have candidate lifecycle")
        if candidate.get("availability") != "available":
            raise ValueError("Evaluation candidate must be available")
        if baseline_revision_id and (not baseline or baseline.get("character_id") != character_id or
                                     baseline.get("scope") != candidate.get("scope")):
            raise ValueError("Baseline is not registered for the same character and language")
        if baseline and (baseline.get("lifecycle") != "default" or
                         baseline.get("engine") != candidate.get("engine")):
            raise ValueError("Evaluation baseline must be the current default for the same engine")
        if candidate.get("engine") != "gpt-sovits":
            raise ValueError("Automatic Evaluation currently supports only gpt-sovits")
        if reference_set_id:
            profile = self._profile(character_id)
            if reference_set_id not in (profile.get("references") or {}):
                raise ValueError("Evaluation reference does not exist in the character profile")
        return self.evaluations.create(
            character_id=character_id, candidate_revision_id=candidate_revision_id,
            baseline_revision_id=baseline_revision_id, sample_set=sample_set,
            engine=engine, reference_set_id=reference_set_id,
            requested_parameters=requested_parameters)

    def _profile(self, character_id: str | None) -> dict:
        if not character_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", character_id):
            raise ValueError("Candidate must be assigned to a valid character")
        path = self.voice_dir / f"{character_id}.json"
        if not path.is_file():
            raise ValueError("Character profile does not exist")
        return json.loads(path.read_text(encoding="utf-8-sig"))

    @staticmethod
    def _profile_model_id(model_id: str, revision_id: str) -> str:
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", str(model_id)).strip(".-_") or "model"
        return f"{base[:48]}-r-{revision_id[-8:]}"

