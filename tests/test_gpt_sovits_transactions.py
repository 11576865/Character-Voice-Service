"""Cross-process GPT-SoVITS serving transaction fault injection.

A separate test-only HTTP server keeps inference in flight while other
CVS processes try to change model weights on the same backend endpoint.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from server.runtime_slot_lease import acquire_runtime_slot
from server.backends.gpt_sovits import _transaction_paths


_ROOT = Path(__file__).resolve().parents[1]
_WORKER = _ROOT / "tests" / "fixtures" / "gpt_sovits_transaction_worker.py"


@pytest.fixture
def backend_server(tmp_path):
    started = tmp_path / "inference-in-flight"
    release = tmp_path / "release-inference"
    calls = []
    current = {"gpt": None, "sovits": None}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _respond(self, body):
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass  # A killed test-only CVS controller dropped this socket.

        def do_GET(self):
            route = urlsplit(self.path)
            value = parse_qs(route.query).get("weights_path", [""])[0]
            with lock:
                if route.path == "/set_sovits_weights":
                    current["sovits"] = value
                elif route.path == "/set_gpt_weights":
                    current["gpt"] = value
                else:
                    self.send_error(404)
                    return
                calls.append(("weights", route.path, value))
            self._respond(b"ok")

        def do_POST(self):
            if self.path != "/tts":
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size))
            text = payload.get("text", "")
            if text == "A text":
                started.write_text("inference entered", encoding="utf-8")
                deadline = time.monotonic() + 14
                while not release.exists() and time.monotonic() < deadline:
                    time.sleep(0.03)
            with lock:
                # If another CVS client switched weights mid-inference,
                # the selected model would no longer match this request.
                model = str(current["gpt"] or "unset").removesuffix(".gpt")
                calls.append(("inference", text, model))
            self._respond(model.encode("ascii"))

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield {
            "endpoint": f"http://127.0.0.1:{server.server_port}",
            "started": started, "release": release, "calls": calls,
            "state": tmp_path / "shared-cvs-state", "server": server,
        }
    finally:
        release.touch()
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _spawn(fixture, model):
    env = os.environ.copy()
    env["CVS_DATA_DIR"] = str(fixture["state"])
    env["PYTHONPATH"] = str(_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, str(_WORKER), fixture["endpoint"], str(fixture["state"]), model],
        cwd=str(_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _result(proc, timeout=14):
    stdout, stderr = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, stderr
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        raise AssertionError(f"invalid worker result: {stdout!r}, stderr={stderr!r}") from exc


def _wait(path: Path, proc: subprocess.Popen, seconds=9):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise AssertionError(f"worker died before inference: {proc.communicate()}")
        time.sleep(0.025)
    raise AssertionError("first CVS worker never entered the HTTP inference route")


def test_second_cvs_process_cannot_switch_weights_during_inflight_inference(backend_server):
    f = backend_server
    a = _spawn(f, "A")
    try:
        _wait(f["started"], a)
        b = _spawn(f, "B")
        try:
            blocked = _result(b)
            assert blocked["ok"] is False
            assert blocked["status"] == 503
            assert "busy" in blocked["detail"]
            assert all("B." not in str(call) for call in f["calls"])
        finally:
            if b.poll() is None:
                b.kill()
                b.wait(timeout=5)
        f["release"].touch()
        assert _result(a)["audio"] == "A"
        assert _result(_spawn(f, "B")) == {"ok": True, "audio": "B"}
        assert [x[2] for x in f["calls"] if x[0] == "inference"] == ["A", "B"]
    finally:
        f["release"].touch()
        if a.poll() is None:
            a.kill()
            a.wait(timeout=5)


def test_killed_cvs_process_preserves_unknown_inference_even_after_os_lock_releases(backend_server):
    f = backend_server
    a = _spawn(f, "A")
    try:
        _wait(f["started"], a)
        # Kill controller WITHOUT any Python cleanup; fake backend is
        # still in the middle of A's /tts handler.
        a.kill()
        a.wait(timeout=5)
        key, root, receipt = _transaction_paths(f["endpoint"] + "/tts")
        # _transaction_paths uses the module default DATA_DIR, while the
        # worker explicitly uses our per-test data root.
        receipt = f["state"] / "inference-transactions" / receipt.name
        root = receipt.parent
        assert receipt.is_file()
        assert json.loads(receipt.read_text())["model_id"] == "A"
        lease = acquire_runtime_slot(key, root)
        assert lease is not None, "dead worker OS lease should be released"
        lease.release()

        rejected = _result(_spawn(f, "B"))
        assert rejected["ok"] is False
        assert rejected["status"] == 503
        assert "unresolved" in rejected["detail"]
        assert all("B." not in str(call) for call in f["calls"])

        # A real operator must verify backend state before reconciliation.
        # The controlled mock is known to be idle only after release.
        f["release"].touch()
        deadline = time.monotonic() + 5
        while not any(c[0] == "inference" and c[1] == "A text" for c in f["calls"]):
            assert time.monotonic() < deadline
            time.sleep(0.03)
        lease = acquire_runtime_slot(key, root)
        assert lease is not None
        try:
            receipt.unlink()
        finally:
            lease.release()
        assert _result(_spawn(f, "B"))["audio"] == "B"
    finally:
        f["release"].touch()
        if a.poll() is None:
            a.kill()
            a.wait(timeout=5)
