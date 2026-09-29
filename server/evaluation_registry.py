import json
from datetime import datetime, timezone
from pathlib import Path

from server.config import DATA_DIR


EVALUATION_DIR = DATA_DIR / "evaluations"
SCHEMA_VERSION = "1.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_evaluation(record: dict, *, directory: Path = EVALUATION_DIR) -> Path:
    if not isinstance(record, dict):
        raise ValueError("evaluation record must be an object")
    model_id = str(record.get("model_id") or "").strip()
    evaluation_id = str(record.get("evaluation_id") or "").strip()
    if not model_id or not evaluation_id:
        raise ValueError("evaluation_id and model_id are required")

    payload = dict(record)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("created_at", _now_iso())

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{evaluation_id}.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise ValueError(f"evaluation_id already exists with different content: {evaluation_id}")
        return path

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def list_evaluations(model_id: str, *, directory: Path = EVALUATION_DIR) -> list[dict]:
    if not directory.is_dir():
        return []
    items = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("model_id") == model_id:
            items.append(record)
    return items


def model_is_promotable(model_id: str, *, directory: Path = EVALUATION_DIR) -> bool:
    for record in reversed(list_evaluations(model_id, directory=directory)):
        decision = record.get("decision") or {}
        if decision.get("status") == "validated" and bool(decision.get("promotable")):
            return True
    return False
