"""Provenance-linked evaluation records and conservative model promotion gate.

Legacy 1.0 records remain readable but never qualify for model promotion.
This module authenticates declared *identities*, not subjective listening quality.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from server.config import DATA_DIR


EVALUATION_DIR = DATA_DIR / "evaluations"
BENCHMARK_DIR = DATA_DIR / "benchmarks"
SCHEMA_VERSION = "1.1"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(value: object, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"{name} must be a stable ASCII ID")
    return value


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _validate_record(record: dict) -> None:
    _id(record.get("evaluation_id"), "evaluation_id")
    _id(record.get("model_id"), "model_id")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("new evaluation records require schema_version 1.1")
    _sha(record.get("model_revision"), "model_revision")
    _sha(record.get("generation_revision"), "generation_revision")

    dataset = record.get("dataset")
    if not isinstance(dataset, dict):
        raise ValueError("dataset provenance is required")
    _id(dataset.get("dataset_id"), "dataset_id")
    _sha(dataset.get("dataset_sha256"), "dataset_sha256")
    test_ids = dataset.get("test_item_ids")
    if not isinstance(test_ids, list) or not test_ids:
        raise ValueError("test_item_ids must be a nonempty list")
    if any(_id(item, "test item ID") != item for item in test_ids) or len(set(test_ids)) != len(test_ids):
        raise ValueError("test_item_ids must contain unique stable IDs")

    references = dataset.get("references")
    if not isinstance(references, list) or not references:
        raise ValueError("references must contain at least one bound voice reference")
    ref_ids = set()
    for ref in references:
        if not isinstance(ref, dict):
            raise ValueError("references must be objects")
        reference_id = _id(ref.get("reference_id"), "reference_id")
        _id(ref.get("item_id"), "reference item_id")
        if reference_id in ref_ids:
            raise ValueError("duplicate reference_id in evaluation")
        ref_ids.add(reference_id)

    decision = record.get("decision")
    if not isinstance(decision, dict):
        raise ValueError("decision must be an object")
    if decision.get("status") not in {"pending", "validated", "rejected"}:
        raise ValueError("unsupported evaluation decision status")
    if type(decision.get("promotable")) is not bool:
        raise ValueError("decision.promotable must be a boolean")
    if decision["promotable"] and decision["status"] != "validated":
        raise ValueError("only validated evaluations may be promotable")


def write_evaluation(record: dict, *, directory: Path = EVALUATION_DIR) -> Path:
    """Write a v1.1 record once; retrying an identical record keeps created_at."""
    if not isinstance(record, dict):
        raise ValueError("evaluation record must be an object")
    payload = dict(record)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    _validate_record(payload)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{payload['evaluation_id']}.json"

    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        comparison = dict(payload)
        if "created_at" not in comparison:
            comparison["created_at"] = existing.get("created_at")
        if existing != comparison:
            raise ValueError(f"evaluation_id already exists with different content: {payload['evaluation_id']}")
        return path

    payload.setdefault("created_at", _now_iso())
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError as exc:
        raise ValueError(f"evaluation_id concurrently created: {payload['evaluation_id']}") from exc
    return path


def list_evaluations(model_id: str, *, directory: Path = EVALUATION_DIR) -> list[dict]:
    _id(model_id, "model_id")
    if not directory.is_dir():
        return []
    items = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("model_id") == model_id:
            items.append(record)
    return items


def _matches_frozen_dataset(record: dict, directory: Path) -> bool:
    provenance = record["dataset"]
    dataset_id = provenance["dataset_id"]
    path = directory / f"{dataset_id}.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                or manifest.get("dataset_id") != dataset_id
                or manifest.get("dataset_sha256") != provenance["dataset_sha256"]):
            return False
        items = manifest.get("items")
        if not isinstance(items, list) or not items:
            return False
        payload = {"schema_version": 1, "dataset_id": dataset_id, "items": items}
        if hashlib.sha256(_canonical(payload)).hexdigest() != manifest["dataset_sha256"]:
            return False
        by_id = {}
        hashes = set()
        for item in items:
            if not isinstance(item, dict):
                return False
            item_id = _id(item.get("id"), "item ID")
            _sha(item.get("audio_sha256"), "audio_sha256")
            if item.get("source") != "ORIGINAL" or item_id in by_id or item["audio_sha256"] in hashes:
                return False
            if item.get("split") not in {"train", "dev", "test-recorded", "reference"}:
                return False
            if type(item.get("reference")) is not bool:
                return False
            if item["split"] == "test-recorded" and item["reference"]:
                return False
            hashes.add(item["audio_sha256"])
            by_id[item_id] = item

        all_test = {item_id for item_id, item in by_id.items() if item["split"] == "test-recorded"}
        if not all_test or set(provenance["test_item_ids"]) != all_test:
            return False
        for ref in provenance["references"]:
            item = by_id.get(ref["item_id"])
            if not item or not (item["reference"] or item["split"] == "reference"):
                return False
            if item["split"] == "test-recorded":
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def model_is_promotable(
    model_id: str, *, model_revision: str | None = None,
    directory: Path = EVALUATION_DIR, benchmark_dir: Path = BENCHMARK_DIR,
) -> bool:
    """Fail closed unless a v1.1 validated record matches actual model/dataset IDs.

    Exact source WAV freshness and listening quality require separate evidence.
    """
    if not isinstance(model_revision, str) or not _SHA_RE.fullmatch(model_revision):
        return False
    for record in reversed(list_evaluations(model_id, directory=directory)):
        try:
            _validate_record(record)
        except (ValueError, TypeError):
            continue
        decision = record["decision"]
        if (record["model_revision"] == model_revision
                and decision["status"] == "validated"
                and decision["promotable"] is True
                and _matches_frozen_dataset(record, benchmark_dir)):
            return True
    return False
