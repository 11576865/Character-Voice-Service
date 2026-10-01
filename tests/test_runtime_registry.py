import json
from pathlib import Path

import pytest

from server.runtime_registry import load_runtime_registry


def write_registry(path: Path, engines: dict) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "engines": engines}),
        encoding="utf-8",
    )


def test_missing_registry_is_empty(tmp_path):
    registry = load_runtime_registry(tmp_path / "missing.json")
    assert registry.schema_version == 1
    assert registry.runtimes == {}


def test_managed_runtime_parses_without_activation(tmp_path):
    python = tmp_path / "engine" / "python.exe"
    python.parent.mkdir()
    python.write_bytes(b"")
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    path = tmp_path / "runtimes.json"
    write_registry(
        path,
        {
            "index-tts": {
                "enabled": True,
                "mode": "managed",
                "executable": str(python),
                "cwd": str(cwd),
                "args": ["sidecar.py"],
                "health_url": "http://127.0.0.1:9882/health",
                "exclusive_group": "gpu0",
                "start_on_demand": True,
            }
        },
    )

    spec = load_runtime_registry(path).get("index-tts")
    assert spec is not None
    assert spec.managed is True
    assert spec.command() == [str(python), "sidecar.py"]
    assert spec.exclusive_group == "gpu0"
    assert spec.start_on_demand is True


def test_enabled_managed_runtime_requires_executable_and_cwd(tmp_path):
    path = tmp_path / "runtimes.json"
    write_registry(
        path,
        {
            "broken": {
                "enabled": True,
                "mode": "managed",
                "health_url": "http://127.0.0.1:9999/health",
            }
        },
    )
    with pytest.raises(ValueError, match="requires executable"):
        load_runtime_registry(path)


def test_external_runtime_does_not_require_local_process_command(tmp_path):
    path = tmp_path / "runtimes.json"
    write_registry(
        path,
        {
            "remote-engine": {
                "enabled": True,
                "mode": "external",
                "health_url": "http://127.0.0.1:9000/health",
            }
        },
    )
    spec = load_runtime_registry(path).get("remote-engine")
    assert spec is not None
    assert spec.managed is False
