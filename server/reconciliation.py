from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from server.config import DATA_DIR
from server.engine_registry import load_engine_descriptors
from server.model_registry import list_models
from server.runtime_registry import load_runtime_registry


DEFAULT_HOST_INVENTORY = DATA_DIR / "host-runtime-inventory.json"


def _norm_path(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return os.path.normcase(os.path.abspath(os.path.expandvars(os.path.expanduser(text))))
    except (OSError, ValueError):
        return os.path.normcase(text)


def _exists(value: object) -> bool | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Path(os.path.expandvars(text)).expanduser().exists()
    except OSError:
        return False


def load_host_inventory(path: str | Path | None = None) -> dict:
    inventory_path = (
        Path(path).expanduser()
        if path is not None
        else Path(
            os.environ.get("CVS_HOST_INVENTORY", str(DEFAULT_HOST_INVENTORY))
        ).expanduser()
    )
    if not inventory_path.is_file():
        return {
            "schema_version": 1,
            "path": str(inventory_path),
            "present": False,
            "items": [],
            "metadata": {},
        }

    raw = json.loads(inventory_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("unsupported host runtime inventory schema")
    items = raw.get("items") or []
    if not isinstance(items, list):
        raise ValueError("host runtime inventory items must be an array")
    return {
        **raw,
        "path": str(inventory_path),
        "present": True,
        "items": [item for item in items if isinstance(item, dict)],
    }


def reconcile_system(*, inventory_path: str | Path | None = None) -> dict:
    runtimes = load_runtime_registry()
    engines = load_engine_descriptors()
    inventory = load_host_inventory(inventory_path)

    issues: list[dict[str, Any]] = []
    claims: dict[str, list[dict[str, Any]]] = {}
    dependencies: list[dict[str, Any]] = []

    for engine_id, runtime in sorted(runtimes.runtimes.items()):
        if engine_id not in engines:
            issues.append({
                "severity": "error",
                "code": "runtime-without-engine-descriptor",
                "engine_id": engine_id,
                "runtime_id": runtime.runtime_id,
                "message": f"Runtime {runtime.runtime_id} has no Engine Registry descriptor.",
            })

        for dep in runtime.dependencies:
            path = dep.get("path")
            normalized = _norm_path(path)
            record = {
                "engine_id": engine_id,
                "runtime_id": runtime.runtime_id,
                "dependency_id": dep.get("id"),
                "kind": dep.get("kind"),
                "ownership": dep.get("ownership", "unspecified"),
                "path": path,
                "normalized_path": normalized,
                "exists": _exists(path),
            }
            dependencies.append(record)
            if normalized:
                claims.setdefault(normalized, []).append(record)
            if path and record["exists"] is False:
                issues.append({
                    "severity": "error",
                    "code": "declared-dependency-missing",
                    **{
                        key: record[key]
                        for key in (
                            "engine_id",
                            "runtime_id",
                            "dependency_id",
                            "kind",
                            "path",
                        )
                    },
                    "message": (
                        f"{runtime.runtime_id} declares {dep.get('id')} at {path}, "
                        "but that path does not exist."
                    ),
                })

    for engine_id, descriptor in sorted(engines.items()):
        if engine_id not in runtimes.runtimes:
            issues.append({
                "severity": "warning",
                "code": "engine-without-runtime",
                "engine_id": engine_id,
                "message": f"Engine {engine_id} is registered but has no Runtime Registry entry.",
            })

    try:
        models = list_models()
    except Exception as exc:
        models = []
        issues.append({
            "severity": "error",
            "code": "model-registry-unreadable",
            "message": f"Model Registry could not be read: {exc}",
        })

    for model in models:
        engine_id = str(model.get("engine") or "").strip()
        if engine_id and engine_id not in engines:
            issues.append({
                "severity": "error",
                "code": "model-engine-unregistered",
                "model_id": model.get("model_id"),
                "engine_id": engine_id,
                "message": (
                    f"Model {model.get('model_id')} uses engine {engine_id}, "
                    "but that engine has no descriptor."
                ),
            })

    for normalized, owners in sorted(claims.items()):
        private = [
            item
            for item in owners
            if str(item.get("ownership") or "").casefold() == "engine-private"
        ]
        runtime_ids = sorted({str(item["runtime_id"]) for item in private})
        if len(runtime_ids) > 1:
            issues.append({
                "severity": "error",
                "code": "private-dependency-multi-owner",
                "path": owners[0].get("path"),
                "runtime_ids": runtime_ids,
                "message": (
                    f"Engine-private dependency {owners[0].get('path')} is claimed "
                    f"by multiple runtimes: {', '.join(runtime_ids)}."
                ),
            })

    inventory_items: list[dict[str, Any]] = []
    path_versions: dict[tuple[str, str], set[str]] = {}
    for item in inventory.get("items", []):
        path = item.get("path")
        normalized = _norm_path(path)
        matched_claims = claims.get(normalized, []) if normalized else []
        out = {
            **item,
            "normalized_path": normalized,
            "claimed": bool(matched_claims),
            "claims": [
                {
                    "runtime_id": claim["runtime_id"],
                    "dependency_id": claim["dependency_id"],
                    "ownership": claim["ownership"],
                }
                for claim in matched_claims
            ],
        }
        inventory_items.append(out)

        kind = str(item.get("kind") or "")
        version = str(item.get("version") or "").strip()
        source = str(item.get("source") or "")
        if kind in {"python", "ffmpeg", "ffprobe", "conda"} and source == "PATH":
            path_versions.setdefault((kind, source), set()).add(version or str(path))

        if (
            inventory.get("present")
            and not matched_claims
            and kind in {"python", "conda-env", "engine-runtime"}
            and str(item.get("scope") or "host-visible") != "system"
        ):
            issues.append({
                "severity": "info",
                "code": "unclaimed-runtime-candidate",
                "kind": kind,
                "path": path,
                "version": item.get("version"),
                "message": (
                    f"Discovered {kind} is not claimed by any registered Runtime: {path}"
                ),
            })

    for (kind, source), versions in sorted(path_versions.items()):
        clean = {item for item in versions if item}
        if len(clean) > 1:
            issues.append({
                "severity": "warning",
                "code": "multiple-path-tool-versions",
                "kind": kind,
                "source": source,
                "versions": sorted(clean),
                "message": (
                    f"Multiple {kind} versions/locations are visible through PATH; "
                    "runtime-specific ownership should not depend on PATH ordering."
                ),
            })

    counts = {
        "errors": sum(1 for item in issues if item["severity"] == "error"),
        "warnings": sum(1 for item in issues if item["severity"] == "warning"),
        "info": sum(1 for item in issues if item["severity"] == "info"),
        "registered_engines": len(engines),
        "registered_runtimes": len(runtimes.runtimes),
        "declared_dependencies": len(dependencies),
        "inventory_items": len(inventory_items),
    }

    return {
        "schema_version": 1,
        "status": (
            "error"
            if counts["errors"]
            else "warning"
            if counts["warnings"]
            else "ok"
        ),
        "counts": counts,
        "inventory": {
            "path": inventory.get("path"),
            "present": bool(inventory.get("present")),
            "metadata": inventory.get("metadata") or {},
            "items": inventory_items,
        },
        "dependencies": dependencies,
        "issues": issues,
    }
