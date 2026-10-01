import importlib.util
import json
import socket
import threading
import urllib.request
from pathlib import Path


def load_sidecar_module():
    path = Path(__file__).resolve().parents[1] / "sidecars" / "index_tts_api.py"
    spec = importlib.util.spec_from_file_location("index_tts_sidecar_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DummyTTS:
    use_bf16 = True
    qwen_emo = None


def test_index_tts_sidecar_loopback_shutdown_endpoint():
    module = load_sidecar_module()
    module._SHUTDOWN_REQUESTED.clear()

    server = module.IndexTTSServer(("127.0.0.1", 0), module.Handler, DummyTTS())
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/shutdown",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 202
        assert payload["status"] == "stopping"
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert module._SHUTDOWN_REQUESTED.is_set()
    finally:
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=3)
        server.server_close()
        module._SHUTDOWN_REQUESTED.clear()
