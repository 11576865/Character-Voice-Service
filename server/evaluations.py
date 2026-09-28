"""Versioned, local Evaluation v1 records for model comparisons."""

import hashlib
import json
import re
import threading
import uuid
from copy import deepcopy
from pathlib import Path

from server.model_registry import atomic_json, now_iso


EVALUATION_STATUSES = {"draft", "running", "completed", "cancelled", "failed"}
DECISIONS = {"pending", "promote", "keep_default", "reject", "inconclusive"}
SAMPLE_STATUSES = {"pending", "success", "failed", "unsupported"}
RATING_FIELDS = {"voice_similarity", "pronunciation", "naturalness", "emotion_match", "continuity"}


class EvaluationStore:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()

    def _path(self, evaluation_id: str) -> Path:
        if not re.fullmatch(r"eval-[0-9a-f]{24}", str(evaluation_id)):
            raise ValueError("Invalid evaluation ID")
        return self.root / f"{evaluation_id}.json"

    def create(self, *, character_id: str, candidate_revision_id: str,
               baseline_revision_id: str | None, sample_set: dict,
               engine: dict, reference_set_id: str = "", requested_parameters: dict | None = None) -> dict:
        if not isinstance(sample_set, dict) or not isinstance(sample_set.get("samples"), list) or not sample_set["samples"]:
            raise ValueError("Evaluation needs a non-empty sample set")
        sample_ids = [item.get("sample_id") for item in sample_set["samples"] if isinstance(item, dict)]
        if len(sample_ids) != len(sample_set["samples"]) or len(set(sample_ids)) != len(sample_ids) or any(not item for item in sample_ids):
            raise ValueError("Evaluation sample IDs must be unique and non-empty")
        encoded = json.dumps(sample_set, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        evaluation_id = "eval-" + uuid.uuid4().hex[:24]
        created = now_iso()
        record = {"schema_version": 1, "evaluation_id": evaluation_id,
                  "created_at": created, "updated_at": created, "status": "draft",
                  "character_id": character_id, "candidate_revision_id": candidate_revision_id,
                  "baseline_revision_id": baseline_revision_id,
                  "sample_set": {**deepcopy(sample_set), "sha256": hashlib.sha256(encoded).hexdigest()},
                  "reference_set_id": reference_set_id, "engine": deepcopy(engine),
                  "requested_parameters": deepcopy(requested_parameters or {}),
                  "effective_parameters": {}, "unsupported_parameters": [],
                  "results": [], "human_reviews": [],
                  "decision": {"value": "pending", "reason": "", "reviewed_at": None}}
        self.validate(record)
        with self.lock:
            atomic_json(self._path(evaluation_id), record)
        return record

    def get(self, evaluation_id: str) -> dict:
        with self.lock:
            record = json.loads(self._path(evaluation_id).read_text(encoding="utf-8-sig"))
        self.validate(record)
        return record

    def list(self) -> list[dict]:
        result = []
        with self.lock:
            for path in sorted(self.root.glob("eval-*.json"), reverse=True):
                try:
                    item = json.loads(path.read_text(encoding="utf-8-sig"))
                    self.validate(item)
                    result.append({key: item.get(key) for key in
                                   ("evaluation_id", "created_at", "updated_at", "status", "character_id",
                                    "candidate_revision_id", "baseline_revision_id", "decision")})
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        return result

    def update(self, evaluation_id: str, *, status: str | None = None,
               effective_parameters: dict | None = None,
               unsupported_parameters: list[str] | None = None,
               result: dict | None = None) -> dict:
        with self.lock:
            record = self.get(evaluation_id)
            if status is not None:
                if status not in EVALUATION_STATUSES:
                    raise ValueError("Invalid evaluation status")
                if status == "completed":
                    revisions = {record["candidate_revision_id"]}
                    if record.get("baseline_revision_id"):
                        revisions.add(record["baseline_revision_id"])
                    expected = {(sample["sample_id"], revision) for sample in record["sample_set"]["samples"]
                                for revision in revisions}
                    actual = {(item["sample_id"], item["model_revision_id"]) for item in record["results"]
                              if item.get("status") in SAMPLE_STATUSES - {"pending"}}
                    if actual != expected:
                        raise ValueError("Every sample and compared revision needs a terminal result before completion")
                record["status"] = status
                if status == "running" and not record.get("started_at"):
                    record["started_at"] = now_iso()
                if status in {"completed", "cancelled", "failed"}:
                    record["finished_at"] = now_iso()
            if effective_parameters is not None:
                record["effective_parameters"] = deepcopy(effective_parameters)
            if unsupported_parameters is not None:
                record["unsupported_parameters"] = list(unsupported_parameters)
            if result is not None:
                self._validate_result(record, result)
                record["results"] = [item for item in record["results"]
                                     if not (item["sample_id"] == result["sample_id"] and
                                             item["model_revision_id"] == result["model_revision_id"])]
                record["results"].append(deepcopy(result))
            record["updated_at"] = now_iso()
            self.validate(record)
            atomic_json(self._path(evaluation_id), record)
            return record

    def review(self, evaluation_id: str, *, blind_pair_id: str, preference: str,
               ratings: dict, notes: str = "") -> dict:
        if preference not in {"a", "b", "similar", "both_problematic"}:
            raise ValueError("Invalid A/B preference")
        if any(key not in RATING_FIELDS or value is not None and
               (not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5)
               for key, value in ratings.items()):
            raise ValueError("Ratings must use supported fields and values 1-5 or null")
        with self.lock:
            record = self.get(evaluation_id)
            record["human_reviews"].append({"review_id": "review-" + uuid.uuid4().hex,
                                             "created_at": now_iso(), "blind_pair_id": blind_pair_id,
                                             "preference": preference, "ratings": deepcopy(ratings),
                                             "notes": str(notes)})
            record["updated_at"] = now_iso()
            self.validate(record)
            atomic_json(self._path(evaluation_id), record)
            return record

    def decide(self, evaluation_id: str, *, decision: str, reason: str) -> dict:
        if decision not in DECISIONS - {"pending"} or not str(reason).strip():
            raise ValueError("A final decision and reason are required")
        with self.lock:
            record = self.get(evaluation_id)
            if record["status"] != "completed":
                raise ValueError("Complete the Evaluation before recording a final decision")
            record["decision"] = {"value": decision, "reason": str(reason).strip(),
                                  "reviewed_at": now_iso()}
            record["updated_at"] = now_iso()
            self.validate(record)
            atomic_json(self._path(evaluation_id), record)
            return record

    @staticmethod
    def _validate_result(record: dict, result: dict) -> None:
        samples = {item["sample_id"] for item in record["sample_set"]["samples"]}
        allowed_revisions = {record["candidate_revision_id"], record.get("baseline_revision_id")}
        if result.get("sample_id") not in samples or result.get("model_revision_id") not in allowed_revisions:
            raise ValueError("Result does not belong to this Evaluation")
        if result.get("status") not in SAMPLE_STATUSES:
            raise ValueError("Invalid sample result status")
        if result["status"] == "success" and not result.get("audio_sha256"):
            raise ValueError("Successful samples require an audio SHA-256")

    @staticmethod
    def validate(record: dict) -> None:
        if record.get("schema_version") != 1 or record.get("status") not in EVALUATION_STATUSES:
            raise ValueError("Invalid Evaluation v1 record")
        if record.get("decision", {}).get("value") not in DECISIONS:
            raise ValueError("Invalid Evaluation decision")
        if not isinstance(record.get("results"), list) or not isinstance(record.get("human_reviews"), list):
            raise ValueError("Invalid Evaluation result collections")

