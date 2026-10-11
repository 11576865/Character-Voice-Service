#!/usr/bin/env python3
"""Read-only CVS/System Supervisor external runtime preflight.

No server imports (which can initialize admin credentials), no file writes,
no activation commands, no process starts. HTTP checks are opt-in and
restricted to loopback without following redirects.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_SEVERITY = {"info": 0, "warning": 1, "error": 2}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except (OSError, ValueError, UnicodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _path(value: str, base: Path | None = None) -> Path:
    expanded = Path(os.path.expandvars(value)).expanduser()
    if base is not None and not expanded.is_absolute():
        expanded = base / expanded
    return expanded.resolve()


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _loopback_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.hostname.lower() == "localhost":
            return True
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _probe(url: str, timeout: float):
    if not _loopback_url(url):
        return {"state": "refused", "detail": "health probe must use loopback http://"}
    try:
        opener = build_opener(_NoRedirect)
        with opener.open(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            raw = response.read(4096)
            status = response.status
            content_type = response.headers.get("Content-Type", "")
        if not 200 <= status < 300:
            return {"state": "unavailable", "http_status": status}
        result = {"state": "responsive", "http_status": status}
        if "json" in content_type.lower():
            try:
                body = json.loads(raw.decode("utf-8"))
                if isinstance(body, dict):
                    reported = body.get("status")
                    if isinstance(reported, str):
                        result["reported_status"] = reported[:80]
                        if reported.lower() in {"failed", "stopped", "unhealthy", "error"}:
                            result["state"] = "unhealthy"
            except (UnicodeError, ValueError):
                result["note"] = "invalid JSON health response"
        return result
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return {"state": "unavailable", "detail": type(exc).__name__}


def inspect(registry_path: Path, supervisor_path: Path | None = None,
            *, check_health: bool = False, timeout: float = 1.0) -> dict:
    findings = []
    engines = []

    def note(severity: str, code: str, detail: str, engine: str | None = None):
        item = {"severity": severity, "code": code, "detail": detail}
        if engine:
            item["engine"] = engine
        findings.append(item)

    report = {
        "schema_version": 1,
        "tool": "cvs-external-supervisor-preflight",
        "read_only": True,
        "health_probe_requested": check_health,
        "checked_at_unix": time.time(),
        "registry": str(registry_path),
        "supervisor_config": str(supervisor_path) if supervisor_path else None,
        "engines": engines,
        "findings": findings,
        "correlated_acknowledgment": "not-established",
    }

    registry, error = _read_json(registry_path)
    if error or not isinstance(registry, dict) or registry.get("schema_version") != 1:
        note("error", "invalid-registry", error or "expected a schema_version=1 registry object")
        return _finalize(report)
    entries = registry.get("engines")
    if not isinstance(entries, dict):
        note("error", "invalid-registry-engines", "engines must be a JSON object")
        return _finalize(report)

    supervisor = None
    expected_request_dir = expected_status_file = None
    if supervisor_path is not None:
        supervisor, error = _read_json(supervisor_path)
        if error or not isinstance(supervisor, dict):
            note("error", "invalid-supervisor-config", error or "expected a JSON object")
        else:
            settings = supervisor.get("supervisor") or {}
            bridge = settings.get("control_bridge") or {} if isinstance(settings, dict) else {}
            if not isinstance(bridge, dict):
                bridge = {}
            expected_request_dir = _path(
                str(bridge.get("request_dir") or "character_voice_supervisor/control/requests"),
                supervisor_path.parent,
            )
            expected_status_file = _path(
                str(bridge.get("status_file") or "character_voice_supervisor/control/status.json"),
                supervisor_path.parent,
            )

    for engine_id, spec in sorted(entries.items()):
        if not isinstance(spec, dict) or spec.get("mode") != "external":
            continue
        control = spec.get("external_control") or {}
        if not isinstance(control, dict):
            note("error", "invalid-external-control", "external_control must be an object", engine_id)
            continue
        service_key = str(control.get("service_key") or "").strip()
        request_text = str(control.get("request_dir") or "").strip()
        status_text = str(control.get("status_file") or "").strip()
        health_url = str(spec.get("health_url") or "").strip()
        result = {
            "engine": engine_id,
            "service_key": service_key,
            "enabled": spec.get("enabled") is True,
            "lifecycle_owner": spec.get("lifecycle_owner"),
            "request_dir": None,
            "status_file": None,
            "supervisor_observation": "not-inspected",
            "pending_activation": "not-inspected",
            "health": {"state": "not-probed"},
            "request_id_acknowledged": False,
        }
        engines.append(result)
        if str(control.get("mode") or "").lower().strip() != "file":
            note("warning", "unsupported-control-mode", "no file-bridge preflight available", engine_id)
            continue
        if not service_key or not request_text or not status_text:
            note("error", "incomplete-file-bridge", "service_key, request_dir and status_file are required", engine_id)
            continue
        request_dir = _path(request_text, registry_path.parent)
        status_file = _path(status_text, registry_path.parent)
        result["request_dir"] = str(request_dir)
        result["status_file"] = str(status_file)

        if expected_request_dir and not _same_path(request_dir, expected_request_dir):
            note("error", "request-dir-mismatch", "registry directory differs from Supervisor control_bridge", engine_id)
        if expected_status_file and not _same_path(status_file, expected_status_file):
            note("error", "status-file-mismatch", "registry file differs from Supervisor control_bridge", engine_id)

        if isinstance(supervisor, dict):
            services = supervisor.get("services")
            matches = [
                svc for svc in services
                if isinstance(svc, dict) and svc.get("key") == service_key
            ] if isinstance(services, list) else []
            if len(matches) != 1:
                note("error", "service-key-mismatch", "expected exactly one matching Supervisor service", engine_id)
            else:
                svc = matches[0]
                identity = svc.get("system_identity") or {}
                if not isinstance(identity, dict) or identity.get("entity_kind") != "runtime" or identity.get("engine_id") != engine_id:
                    note("error", "engine-identity-mismatch", "Supervisor system_identity does not match Registry engine", engine_id)
                if result["enabled"] and svc.get("enabled") is False:
                    note("error", "disabled-supervisor-service", "CVS engine enabled but Supervisor service disabled", engine_id)

        if not request_dir.is_dir():
            note("warning", "request-dir-missing", "external inbox directory does not exist; no directory was created", engine_id)
        if status_file.is_file():
            payload, status_error = _read_json(status_file)
            if status_error or not isinstance(payload, dict) or not isinstance(payload.get("services"), list):
                result["supervisor_observation"] = "unreadable"
                note("warning", "status-file-unreadable", "cannot parse external status_file", engine_id)
            else:
                matches = [svc for svc in payload["services"] if isinstance(svc, dict) and svc.get("key") == service_key]
                if len(matches) == 1:
                    result["supervisor_observation"] = str(matches[0].get("state") or "unknown")[:80]
                    result["supervisor_session"] = str(payload.get("session_id") or "")[:100]
                else:
                    result["supervisor_observation"] = "service-not-found"
                    note("warning", "status-service-absent", "no exact service key in status_file", engine_id)
        else:
            result["supervisor_observation"] = "missing"
            note("warning", "status-file-missing", "no Supervisor status file observed", engine_id)

        # Match the producer's fixed per-key SHA256 receipt namespace.
        digest = hashlib.sha256(service_key.encode("utf-8")).hexdigest()[:32]
        receipt = request_dir / ".cvs-activation-state" / (digest + ".pending.json")
        if receipt.exists():
            pending, error = _read_json(receipt)
            if error or not isinstance(pending, dict):
                result["pending_activation"] = "unreadable"
                note("error", "pending-receipt-unreadable", "pending receipt cannot be parsed; do not resubmit", engine_id)
            elif (
                pending.get("schema_version") != 1 or pending.get("phase") != "pending"
                or pending.get("engine_id") != engine_id or pending.get("service_key") != service_key
                or not isinstance(pending.get("request_id"), str)
                or not _REQUEST_ID.fullmatch(pending["request_id"])
            ):
                result["pending_activation"] = "invalid"
                note("error", "pending-receipt-invalid", "receipt identity mismatched; do not resubmit", engine_id)
            else:
                request_id = pending["request_id"]
                result["pending_activation"] = "unresolved"
                result["pending_request_id"] = request_id
                command_path = request_dir / (request_id + ".json")
                if command_path.exists():
                    command, error = _read_json(command_path)
                    if error or not isinstance(command, dict) or not all([
                        command.get("action") == "activate-engine",
                        command.get("schema_version") == 1,
                        command.get("request_id") == request_id,
                        command.get("engine_id") == engine_id,
                        command.get("service_key") == service_key,
                    ]):
                        note("error", "command-receipt-mismatch", "matching top-level command file is invalid", engine_id)
                    else:
                        result["command_artifact"] = "matching-file"
                else:
                    result["command_artifact"] = "not-present"
                    note("warning", "command-file-absent", "may be consumed or never published; outcome unknown", engine_id)
                note("warning", "activation-unresolved", "do not replay an unconfirmed external activation", engine_id)
        else:
            result["pending_activation"] = "none"

        if check_health:
            if not health_url or not _loopback_url(health_url):
                result["health"] = {"state": "refused"}
                note("error", "unsafe-health-url", "only localhost/127.0.0.0/8/::1 HTTP health probes are allowed", engine_id)
            else:
                result["health"] = _probe(health_url, timeout)
                if result["health"]["state"] != "responsive":
                    note("warning", "health-not-confirmed", "no live healthy loopback response confirmed", engine_id)
    if not engines:
        note("warning", "no-external-runtimes", "registry has no external-mode engines")
    return _finalize(report)


def _finalize(report: dict) -> dict:
    highest = max((_SEVERITY[f["severity"]] for f in report["findings"]), default=0)
    report["overall"] = ("pass", "warning", "error")[highest]
    report["exit_code"] = highest
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only CVS external Supervisor preflight (JSON)")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--supervisor-config", type=Path)
    parser.add_argument("--probe-health", action="store_true", help="opt-in loopback HTTP GET only; no redirects")
    parser.add_argument("--timeout", type=float, default=1.0)
    args = parser.parse_args(argv)
    if not (0.05 <= args.timeout <= 5):
        parser.error("timeout must be between 0.05 and 5 seconds")
    report = inspect(
        args.registry.expanduser().resolve(),
        args.supervisor_config.expanduser().resolve() if args.supervisor_config else None,
        check_health=args.probe_health, timeout=args.timeout,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
