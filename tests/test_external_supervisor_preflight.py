"""Read-only preflight contract: no token generation, commands or engine launches."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scripts.external_supervisor_preflight import inspect


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "external_supervisor_preflight.py"


def _write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _env(tmp_path, *, port=19999):
    root = tmp_path / "supervisor"
    requests = root / "control" / "requests"
    status = root / "control" / "status.json"
    requests.mkdir(parents=True)
    _write(status, {
        "session_id": "fixture-session", "services": [
            {"key": "IndexTTS", "state": "ready", "root_pid": 1234},
        ],
    })
    cfg = _write(root / "supervisor.config.json", {
        "supervisor": {
            "control_bridge": {
                "request_dir": "control/requests",
                "status_file": "control/status.json",
            },
        },
        "services": [{
            "key": "IndexTTS",
            "enabled": True,
            "system_identity": {"entity_kind": "runtime", "engine_id": "index-tts"},
        }],
    })
    registry = _write(tmp_path / "runtimes.local.json", {
        "schema_version": 1,
        "engines": {
            "index-tts": {
                "enabled": True, "mode": "external",
                "lifecycle_owner": "system-supervisor",
                "health_url": f"http://127.0.0.1:{port}/health",
                "external_control": {
                    "mode": "file",
                    "request_dir": str(requests),
                    "status_file": str(status),
                    "service_key": "IndexTTS",
                },
            },
        },
    })
    return registry, cfg, requests, status


def _receipt(requests, request_id="a" * 32, **overrides):
    record = {
        "schema_version": 1, "phase": "pending",
        "request_id": request_id, "engine_id": "index-tts",
        "service_key": "IndexTTS", "created_unix": 12.5,
    }
    record.update(overrides)
    digest = hashlib.sha256(b"IndexTTS").hexdigest()[:32]
    path = requests / ".cvs-activation-state" / (digest + ".pending.json")
    _write(path, record)
    return path, record


def test_consistent_live_config_is_read_only_and_does_not_initialize_server_admin_token(tmp_path):
    registry, cfg, requests, status = _env(tmp_path)
    before = {str(p.relative_to(tmp_path)): p.read_bytes()
              for p in tmp_path.rglob("*") if p.is_file()}
    token_dir = tmp_path / "forbidden-admin-token"
    env = os.environ.copy()
    env["CVS_DATA_DIR"] = str(token_dir)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--registry", str(registry),
         "--supervisor-config", str(cfg)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=10, env=env,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    body = json.loads(result.stdout)
    assert body["read_only"] is True
    assert body["health_probe_requested"] is False
    assert body["overall"] == "pass"
    assert body["correlated_acknowledgment"] == "not-established"
    assert body["engines"][0]["pending_activation"] == "none"
    assert body["engines"][0]["supervisor_observation"] == "ready"
    assert body["engines"][0]["health"]["state"] == "not-probed"
    assert not token_dir.exists()
    after = {str(p.relative_to(tmp_path)): p.read_bytes()
             for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_unresolved_command_does_not_get_implicitly_acknowledged_or_removed(tmp_path):
    registry, cfg, requests, status = _env(tmp_path)
    path, record = _receipt(requests)
    report = inspect(registry, cfg)
    assert report["overall"] == "warning"
    assert report["engines"][0]["pending_request_id"] == record["request_id"]
    assert report["engines"][0]["request_id_acknowledged"] is False
    assert report["engines"][0]["command_artifact"] == "not-present"
    assert "activation-unresolved" in {f["code"] for f in report["findings"]}
    assert "command-file-absent" in {f["code"] for f in report["findings"]}
    assert path.exists()


def test_matching_command_receipt_still_does_not_prove_external_acknowledgement(tmp_path):
    registry, cfg, requests, status = _env(tmp_path)
    path, record = _receipt(requests)
    _write(requests / (record["request_id"] + ".json"), {
        "schema_version": 1, "action": "activate-engine",
        "engine_id": "index-tts", "service_key": "IndexTTS",
        "request_id": record["request_id"],
    })
    report = inspect(registry, cfg)
    assert report["engines"][0]["command_artifact"] == "matching-file"
    assert report["engines"][0]["pending_activation"] == "unresolved"
    assert report["engines"][0]["request_id_acknowledged"] is False
    assert path.exists()


@pytest.mark.parametrize("override", [
    {"request_id": "../not-a-safe-file"},
    {"engine_id": "wrong-engine"},
    {"phase": "ready"},
])
def test_invalid_pending_receipt_fails_closed(tmp_path, override):
    registry, cfg, requests, _ = _env(tmp_path)
    path, _ = _receipt(requests, **override)
    report = inspect(registry, cfg)
    assert report["overall"] == "error"
    assert "pending-receipt-invalid" in {f["code"] for f in report["findings"]}
    assert path.exists()


def test_corrupt_receipt_and_conflicting_command_are_errors(tmp_path):
    registry, cfg, requests, _ = _env(tmp_path)
    path, rec = _receipt(requests)
    path.write_text("{broken", encoding="utf-8")
    assert "pending-receipt-unreadable" in {
        f["code"] for f in inspect(registry, cfg)["findings"]
    }
    _write(path, rec)
    _write(requests / (rec["request_id"] + ".json"), {
        "schema_version": 1, "action": "activate-engine",
        "request_id": rec["request_id"],
        "engine_id": "another-engine", "service_key": "IndexTTS",
    })
    assert "command-receipt-mismatch" in {
        f["code"] for f in inspect(registry, cfg)["findings"]
    }


def test_registry_and_supervisor_bridge_path_mismatch_rejected(tmp_path):
    registry, cfg, requests, status = _env(tmp_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["engines"]["index-tts"]["external_control"]["request_dir"] = str(tmp_path / "wrong-requests")
    _write(registry, payload)
    result = inspect(registry, cfg)
    assert result["overall"] == "error"
    assert "request-dir-mismatch" in {f["code"] for f in result["findings"]}
    assert not (tmp_path / "wrong-requests").exists()


def test_supervisor_identity_mismatch_and_disabled_service_are_errors(tmp_path):
    registry, cfg, _, _ = _env(tmp_path)
    payload = json.loads(cfg.read_text(encoding="utf-8"))
    payload["services"][0]["system_identity"]["engine_id"] = "other"
    payload["services"][0]["enabled"] = False
    _write(cfg, payload)
    codes = {f["code"] for f in inspect(registry, cfg)["findings"]}
    assert "engine-identity-mismatch" in codes
    assert "disabled-supervisor-service" in codes


def test_stale_ready_status_is_observational_not_live_health(tmp_path):
    registry, cfg, requests, status = _env(tmp_path)
    report = inspect(registry, cfg)
    assert report["engines"][0]["supervisor_observation"] == "ready"
    assert report["engines"][0]["health"]["state"] == "not-probed"
    assert report["correlated_acknowledgment"] == "not-established"


def test_remote_health_probe_is_refused_before_any_request(tmp_path):
    registry, cfg, _, _ = _env(tmp_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["engines"]["index-tts"]["health_url"] = "http://example.com/health"
    _write(registry, payload)
    report = inspect(registry, cfg, check_health=True)
    assert report["overall"] == "error"
    assert report["engines"][0]["health"]["state"] == "refused"
    assert "unsafe-health-url" in {x["code"] for x in report["findings"]}


def test_opt_in_loopback_probe_observes_real_health_endpoint(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"status":"ready"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        registry, cfg, _, _ = _env(tmp_path, port=server.server_port)
        report = inspect(registry, cfg, check_health=True)
        assert report["overall"] == "pass"
        assert report["engines"][0]["health"] == {
            "state": "responsive", "http_status": 200, "reported_status": "ready",
        }
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_http_redirect_does_not_escape_loopback_allowed_scope(tmp_path):
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "https://example.com/private")
            self.end_headers()

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Redirect)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        registry, cfg, _, _ = _env(tmp_path, port=server.server_port)
        report = inspect(registry, cfg, check_health=True)
        assert report["overall"] == "warning"
        assert report["engines"][0]["health"]["state"] == "unavailable"
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_unavailable_registry_fails_without_creating_it(tmp_path):
    file = tmp_path / "absent.json"
    report = inspect(file)
    assert report["overall"] == "error"
    assert "invalid-registry" in {x["code"] for x in report["findings"]}
    assert not file.exists()
