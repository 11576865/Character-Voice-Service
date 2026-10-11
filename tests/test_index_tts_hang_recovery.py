"""IndexTTS hang admission, diagnostics and process-deadline regressions.

All tests use a fake TTS; no model weights or GPU are required.
"""
import io
import json
import os
import subprocess
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi import HTTPException

from sidecars import index_tts_api as sidecar
from server.engines import index_tts


class BlockingTTS:
    use_bf16 = True
    qwen_emo = None

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def infer(self, **kwargs):
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test did not release inference")
        Path(kwargs["output_path"]).write_bytes(b"RIFF-fake-WAVE")


def test_busy_inference_returns_503_without_queuing_and_health_is_observable(tmp_path):
    speaker = tmp_path / "speaker.wav"
    speaker.write_bytes(b"input")
    fake_tts = BlockingTTS()
    sidecar._SHUTDOWN_REQUESTED.clear()
    server = sidecar.IndexTTSServer(("127.0.0.1", 0), sidecar.Handler, fake_tts)
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.025})
    serving.start()
    finished = {}
    base = f"http://127.0.0.1:{server.server_address[1]}"
    req_body = json.dumps({"text": "hello", "lang": "en", "speaker_audio": str(speaker)}).encode()

    def post_first():
        try:
            request = urllib.request.Request(
                base + "/synthesize", data=req_body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                finished["data"] = response.read()
                finished["status"] = response.status
        except Exception as exc:
            finished["error"] = repr(exc)

    first = threading.Thread(target=post_first)
    first.start()
    try:
        assert fake_tts.entered.wait(timeout=3)
        with urllib.request.urlopen(base + "/health", timeout=2) as response:
            health = json.loads(response.read())
        assert health["status"] == "busy"
        assert health["busy"] is True
        assert health["active_seconds"] is not None
        assert len(health["active_request_id"]) == 32

        second_request = urllib.request.Request(
            base + "/synthesize", data=req_body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        start = time.monotonic()
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(second_request, timeout=2)
        assert time.monotonic() - start < 1.5
        assert caught.value.code == 503
        assert caught.value.headers["Retry-After"] == "2"
        payload = json.loads(caught.value.read())
        assert payload["retryable"] is True
        assert payload["busy"] is True
        assert fake_tts.entered.is_set()

        fake_tts.release.set()
        first.join(timeout=3)
        assert not first.is_alive()
        assert finished == {"data": b"RIFF-fake-WAVE", "status": 200}
        with urllib.request.urlopen(base + "/health", timeout=2) as response:
            ready = json.loads(response.read())
        assert ready["status"] == "ready"
        assert ready["busy"] is False
        assert ready["active_request_id"] is None
        assert ready["active_seconds"] is None
    finally:
        fake_tts.release.set()
        first.join(timeout=3)
        server.shutdown()
        serving.join(timeout=3)
        server.server_close()
        sidecar._SHUTDOWN_REQUESTED.clear()
    assert not serving.is_alive()


def test_inference_watchdog_terminates_stalled_child_process():
    source = """
import time
from sidecars import index_tts_api as api
class Stalled:
    def infer(self, **kwargs):
        time.sleep(10)
api.infer_with_watchdog(Stalled(), {}, request_id="timeout-fixture", deadline=0.05)
"""
    # Windows can keep a captured child pipe readable after process exit
    # if another inherited handle remains open. The contract here is the
    # CHILD EXIT status, so record its logs to a regular file and wait on
    # the process handle rather than waiting for PIPE EOF.
    with tempfile.TemporaryFile(mode="w+b") as log:
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=log, stderr=subprocess.STDOUT, timeout=8,
        )
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
    assert result.returncode == 124, output
    assert "aborting sidecar process" in output
    assert "timeout-fixture" in output


def test_watchdog_early_timer_callback_must_wait_until_deadline_in_child():
    # The original watchdog returned permanently if Timer woke before the
    # monotonic deadline. Force that scheduler ordering in an isolated
    # process so the test itself cannot be terminated by os._exit(124).
    source = """
import threading
import time
from sidecars import index_tts_api as api

class EagerTimer:
    def __init__(self, seconds, callback, args=()):
        self.callback, self.args = callback, args
    def start(self):
        threading.Thread(target=self.callback, args=self.args, daemon=True).start()
    def cancel(self):
        pass

api.threading.Timer = EagerTimer
class Stalled:
    def infer(self, **kwargs):
        time.sleep(10)

print("eager-timer", flush=True)
api.infer_with_watchdog(Stalled(), {}, request_id="early-callback", deadline=0.2)
"""
    with tempfile.TemporaryFile(mode="w+b") as log:
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=log, stderr=subprocess.STDOUT, timeout=8,
        )
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
    assert result.returncode == 124, output
    assert "eager-timer" in output
    assert "early-callback" in output


def test_inference_watchdog_does_not_kill_completed_child_process():
    source = """
import time
from sidecars import index_tts_api as api
class Completed:
    def infer(self, **kwargs):
        time.sleep(0.01)
api.infer_with_watchdog(Completed(), {}, request_id="completed", deadline=0.1)
time.sleep(0.16)
print("still-alive", flush=True)
"""
    with tempfile.TemporaryFile(mode="w+b") as log:
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=log, stderr=subprocess.STDOUT, timeout=8,
        )
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
    assert result.returncode == 0, output
    assert "still-alive" in output


@pytest.mark.parametrize("value", ["abc", "-1", "nan", "inf", "281", "0.5"])
def test_invalid_deadline_configuration_fails_closed(monkeypatch, value):
    monkeypatch.setenv("INDEX_TTS_INFER_DEADLINE_SECONDS", value)
    with pytest.raises(ValueError, match="INDEX_TTS_INFER_DEADLINE_SECONDS"):
        sidecar.inference_deadline_seconds()


def test_deadline_defaults_to_240s_and_accepts_explicit_zero(monkeypatch):
    monkeypatch.delenv("INDEX_TTS_INFER_DEADLINE_SECONDS", raising=False)
    assert sidecar.inference_deadline_seconds() == 240
    monkeypatch.setenv("INDEX_TTS_INFER_DEADLINE_SECONDS", "0")
    assert sidecar.inference_deadline_seconds() == 0


@pytest.mark.parametrize("backend_code,expected", [(503, 503), (429, 503), (400, 502)])
def test_index_tts_adapter_preserves_retryable_busy_error(monkeypatch, backend_code, expected):
    def unavailable(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, backend_code, "busy" if backend_code != 400 else "bad",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"error":"backend refused"}'),
        )
    monkeypatch.setattr(index_tts.urllib.request, "urlopen", unavailable)
    with pytest.raises(HTTPException) as failure:
        index_tts.IndexTTSAdapter().synthesize(
            text="hello", speed=1.0,
            profile={"target_language": "en", "reference_audio": "speaker.wav", "parameters": {}},
        )
    assert failure.value.status_code == expected
    if backend_code in (429, 503):
        assert failure.value.headers == {"Retry-After": "2"}
    else:
        assert failure.value.headers is None


def test_index_tts_adapter_health_exposes_busy_duration(monkeypatch):
    class JSONResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({
                "status": "busy", "model": "IndexTTS-2.5", "precision": "bf16",
                "busy": True, "active_seconds": 3.6, "active_request_id": "abcd",
            }).encode()
    monkeypatch.setattr(index_tts.urllib.request, "urlopen", lambda url, timeout: JSONResponse())
    status = index_tts.IndexTTSAdapter().health()
    assert status["status"] == "busy"
    assert status["active_seconds"] == 3.6
    assert status["active_request_id"] == "abcd"
