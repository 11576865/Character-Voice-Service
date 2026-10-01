import json
from pathlib import Path

import pytest

from server.engine_registry import load_engine_descriptors


def test_engine_registry_loads_builtin_and_sidecar_descriptors(tmp_path):
    (tmp_path / "gpt.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine_id": "gpt-sovits",
                "name": "GPT",
                "capability_kind": "speech-synthesis",
                "adapter_kind": "builtin:gpt-sovits",
                "capabilities": {"zero_shot": True},
                "protocol": {},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "future.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine_id": "future-tts",
                "name": "Future",
                "capability_kind": "speech-synthesis",
                "adapter_kind": "cvs-sidecar-v1",
                "capabilities": {"zero_shot": True},
                "protocol": {
                    "health_path": "/health",
                    "synthesize_path": "/v1/synthesize",
                },
            }
        ),
        encoding="utf-8",
    )

    items = load_engine_descriptors(tmp_path)
    assert sorted(items) == ["future-tts", "gpt-sovits"]
    assert items["future-tts"].adapter_kind == "cvs-sidecar-v1"
    assert items["future-tts"].capability_kind == "speech-synthesis"


def test_engine_registry_rejects_unknown_adapter_kind(tmp_path):
    (tmp_path / "bad.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine_id": "bad-engine",
                "adapter_kind": "python-module-import",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported adapter_kind"):
        load_engine_descriptors(tmp_path)
