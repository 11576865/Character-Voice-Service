"""Minimal IndexTTS 2.5 HTTP sidecar for Character Voice Service.

Run this file with the Python environment that owns IndexTTS, not the CVS
environment. Example (from the IndexTTS checkout):

    uv run D:/path/to/Character-Voice-Service/sidecars/index_tts_api.py

Environment variables:
    INDEX_TTS_MODEL_DIR   default: ./checkpoints
    INDEX_TTS_CONFIG      default: <model-dir>/config.yaml
    INDEX_TTS_HOST        default: 127.0.0.1
    INDEX_TTS_PORT        default: 9882
    INDEX_TTS_USE_BF16    default: 1
    INDEX_TTS_USE_QWEN_EMO default: 0
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MODEL_DIR = Path(os.environ.get("INDEX_TTS_MODEL_DIR", "checkpoints")).resolve()
CONFIG_PATH = Path(
    os.environ.get("INDEX_TTS_CONFIG", str(MODEL_DIR / "config.yaml"))
).resolve()
HOST = os.environ.get("INDEX_TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("INDEX_TTS_PORT", "9882"))
USE_BF16 = os.environ.get("INDEX_TTS_USE_BF16", "1") == "1"
USE_QWEN_EMO = os.environ.get("INDEX_TTS_USE_QWEN_EMO", "0") == "1"

_infer_lock = threading.Lock()


def _language(value: object) -> str:
    raw = str(value or "ZH").strip().upper().replace("_", "-")
    aliases = {
        "ZH-CN": "ZH",
        "ZH-HANS": "ZH",
        "CN": "ZH",
        "EN-US": "EN",
        "EN-GB": "EN",
        "JA-JP": "JA",
        "JP": "JA",
        "KO-KR": "KO",
        "YUE-HK": "YUE",
    }
    return aliases.get(raw, raw)


def _load_model():
    from indextts.infer_v2_5 import IndexTTS2

    return IndexTTS2(
        cfg_path=str(CONFIG_PATH),
        model_dir=str(MODEL_DIR),
        use_bf16=USE_BF16,
        use_qwen_emo=USE_QWEN_EMO,
    )


print(f"Loading IndexTTS 2.5 from {MODEL_DIR} ...")
tts = _load_model()
print("IndexTTS 2.5 ready.")


class Handler(BaseHTTPRequestHandler):
    server_version = "CVS-IndexTTS/0.1"

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {
                "status": "ready",
                "engine": "index-tts",
                "model": "2.5",
                "qwen_emotion": USE_QWEN_EMO,
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/synthesize":
            self._json(404, {"error": "not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 2 * 1024 * 1024:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(size).decode("utf-8"))
            text = str(request.get("text") or "").strip()
            speaker_audio = str(request.get("speaker_audio") or "").strip()
            if not text:
                raise ValueError("text is required")
            if not speaker_audio:
                raise ValueError("speaker_audio is required")

            kwargs = {
                "spk_audio_prompt": speaker_audio,
                "text": text,
                "lang": _language(request.get("lang")),
                "emo_alpha": float(request.get("emo_alpha", 0.6)),
                "use_emo_text": bool(request.get("use_emo_text", False)),
                "emo_text": request.get("emotion_text"),
                "use_random": bool(request.get("use_random", False)),
                "interval_silence": int(request.get("interval_silence", 200)),
                "max_text_tokens_per_segment": int(
                    request.get("max_text_tokens_per_segment", 120)
                ),
                "duration_factor": float(request.get("duration_factor", 1.0)),
                "text_normalization": bool(request.get("text_normalization", True)),
                "verbose": False,
            }
            emotion_audio = request.get("emotion_audio")
            emotion_vector = request.get("emotion_vector")
            if emotion_audio:
                kwargs["emo_audio_prompt"] = str(emotion_audio)
            if emotion_vector is not None:
                kwargs["emo_vector"] = emotion_vector

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                output_path = Path(handle.name)
            kwargs["output_path"] = str(output_path)
            try:
                with _infer_lock:
                    tts.infer(**kwargs)
                audio = output_path.read_bytes()
            finally:
                output_path.unlink(missing_ok=True)

            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[IndexTTS] {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"IndexTTS sidecar listening on http://{HOST}:{PORT}")
    server.serve_forever()
