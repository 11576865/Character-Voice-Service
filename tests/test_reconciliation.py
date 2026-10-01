import json
from pathlib import Path
from types import SimpleNamespace

import server.reconciliation as reconciliation


class Runtime:
    def __init__(self, runtime_id, dependencies):
        self.runtime_id = runtime_id
        self.dependencies = tuple(dependencies)


def test_reconciliation_detects_missing_and_multi_owned_dependencies(tmp_path, monkeypatch):
    shared = tmp_path / "shared-python.exe"
    missing = tmp_path / "missing.exe"

    monkeypatch.setattr(
        reconciliation,
        "load_runtime_registry",
        lambda: SimpleNamespace(
            runtimes={
                "engine-a": Runtime(
                    "runtime-a",
                    [
                        {
                            "id": "python",
                            "kind": "python-runtime",
                            "ownership": "engine-private",
                            "path": str(shared),
                        },
                        {
                            "id": "missing",
                            "kind": "tool",
                            "ownership": "engine-private",
                            "path": str(missing),
                        },
                    ],
                ),
                "engine-b": Runtime(
                    "runtime-b",
                    [
                        {
                            "id": "python",
                            "kind": "python-runtime",
                            "ownership": "engine-private",
                            "path": str(shared),
                        }
                    ],
                ),
            }
        ),
    )
    monkeypatch.setattr(
        reconciliation,
        "load_engine_descriptors",
        lambda: {"engine-a": object(), "engine-b": object()},
    )
    monkeypatch.setattr(reconciliation, "list_models", lambda: [])

    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps({"schema_version": 1, "items": []}),
        encoding="utf-8",
    )

    report = reconciliation.reconcile_system(inventory_path=inventory)
    codes = {item["code"] for item in report["issues"]}
    assert "declared-dependency-missing" in codes
    assert "private-dependency-multi-owner" in codes
    assert report["status"] == "error"


def test_reconciliation_flags_unclaimed_runtime_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reconciliation,
        "load_runtime_registry",
        lambda: SimpleNamespace(runtimes={}),
    )
    monkeypatch.setattr(reconciliation, "load_engine_descriptors", lambda: {})
    monkeypatch.setattr(reconciliation, "list_models", lambda: [])

    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "items": [
                    {
                        "kind": "conda-env",
                        "path": str(tmp_path / "old-env"),
                        "source": "conda-env-list",
                        "scope": "host-visible",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = reconciliation.reconcile_system(inventory_path=inventory)
    assert any(
        item["code"] == "unclaimed-runtime-candidate"
        for item in report["issues"]
    )
