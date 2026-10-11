"""Public CVS discovery and health must not leak local host paths.

Tests exercise actual temp Model Registry failure modes and the public
FastAPI endpoints, rather than treating exception strings as safe data.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
import server.model_registry as model_registry
from test_model_registry import make_manifest


client = TestClient(app_module.app)


@pytest.mark.parametrize("break_manifest", ["deleted", "invalid-json", "changed-manifest"])
def test_unauthenticated_model_discovery_never_exposes_private_manifest_path(
    tmp_path, monkeypatch, break_manifest
):
    root = tmp_path / "private" / "models-with-internal-name"
    registry = tmp_path / "private" / "model-registry.json"
    manifest = make_manifest(root)
    model_registry.scan_model_root(model_root=root, registry_path=registry)
    if break_manifest == "deleted":
        manifest.unlink()
    elif break_manifest == "invalid-json":
        manifest.write_text("{private host path secret", encoding="utf-8")
    else:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["name"] = "changed after scan"
        manifest.write_text(json.dumps(raw), encoding="utf-8")

    monkeypatch.setattr(
        app_module, "list_models",
        lambda: model_registry.list_models(model_root=root, registry_path=registry),
    )
    response = client.get("/v1/models")
    assert response.status_code == 200
    models = response.json()["models"]
    assert len(models) == 1
    assert models[0]["model_id"] == "march7-gsv-v4-a"
    assert models[0]["error_code"] == "model-metadata-unavailable"
    assert models[0]["error"] == "model metadata unavailable"
    wire = response.text
    assert str(root) not in wire
    assert str(manifest) not in wire
    assert "artifacts/gpt.ckpt" not in wire
    assert "private host path secret" not in wire


def test_unreadable_registry_does_not_reflect_private_absolute_path(tmp_path, monkeypatch):
    private_path = tmp_path / "private" / "model-registry.json"
    def fail():
        raise FileNotFoundError(2, "No such file", str(private_path))
    monkeypatch.setattr(app_module, "list_models", fail)
    response = client.get("/v1/models")
    assert response.status_code == 500
    assert response.json()["detail"] == "Model Registry unavailable"
    assert str(private_path) not in response.text


def test_public_health_omits_backend_exception_path_and_arbitrary_diagnostics(
    tmp_path, monkeypatch
):
    secret_path = str(tmp_path / "private" / "weights" / "character.ckpt")
    monkeypatch.setattr(app_module, "list_engines", lambda: [{"engine": "index-tts"}])
    class FakeAdapter:
        def health(self):
            return {
                "status": "offline",
                "error": f"FileNotFoundError: {secret_path}",
                "model": secret_path,
                "debug": {"model_path": secret_path},
                "active_request_id": "sensitive-request-id",
                "busy": True,
                "precision": "fp16",
                "active_seconds": 1.5,
            }
    monkeypatch.setattr(app_module, "get_adapter", lambda _: FakeAdapter())
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == "character-voice-service"
    assert payload["engines"] == [{
        "engine": "index-tts", "status": "offline",
        "error": "backend health check failed", "busy": True,
        "precision": "fp16", "active_seconds": 1.5,
    }]
    assert secret_path not in response.text
    assert "sensitive-request-id" not in response.text


def test_public_health_sanitizes_arbitrary_status_and_invalid_metrics(monkeypatch):
    monkeypatch.setattr(app_module, "list_engines", lambda: [{"engine": "third-party"}])
    class FakeAdapter:
        def health(self):
            return {
                "status": "/home/engine/private-weights.ckpt",
                "busy": "true", "active_seconds": float("nan"),
                "precision": "fp32/private/internal",
                "error": "C:/Users/private/path",
            }
    monkeypatch.setattr(app_module, "get_adapter", lambda _: FakeAdapter())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["engines"] == [{
        "engine": "third-party", "status": "unknown",
        "error": "backend health check failed",
    }]
    assert "/home/" not in response.text
    assert "C:/Users/" not in response.text


def test_health_exception_is_observational_failure_not_private_traceback(monkeypatch):
    monkeypatch.setattr(app_module, "list_engines", lambda: [{"engine": "gpt-sovits"}])
    class RaisingAdapter:
        def health(self):
            raise OSError("private runtime at D:/private/models/weights.pth")
    monkeypatch.setattr(app_module, "get_adapter", lambda _: RaisingAdapter())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["engines"] == [{
        "engine": "gpt-sovits",
        "status": "offline",
        "error": "backend health check failed",
    }]
    assert "D:/private" not in response.text


def test_public_profile_validation_error_does_not_reflect_private_path(tmp_path, monkeypatch):
    voice_root = tmp_path / "voices"
    voice_root.mkdir()
    (voice_root / "character.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app_module, "VOICE_DIR", voice_root)
    private = str(tmp_path / "models" / "private.ckpt")
    monkeypatch.setattr(
        app_module, "read_valid_profile",
        lambda _: (_ for _ in ()).throw(ValueError("invalid artifact " + private)),
    )
    response = client.post("/v1/audio/resolve", json={
        "voice": "character", "input": "Hello", "speed": 1.0,
    })
    assert response.status_code == 500
    assert response.json()["detail"] == "Invalid voice profile: character"
    assert private not in response.text


def test_public_profile_missing_reference_does_not_echo_untrusted_exception(monkeypatch):
    monkeypatch.setattr(
        app_module, "load_voice_profile", lambda _: {"references": {}},
    )
    private = "D:/models/my-personal-reference.wav"
    monkeypatch.setattr(
        app_module, "resolve_profile_selection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyError(private)),
    )
    response = client.post("/v1/audio/resolve", json={"voice": "abc", "input": "Hello"})
    assert response.status_code == 404
    assert response.json()["detail"] == "requested model or reference not found"
    assert private not in response.text
