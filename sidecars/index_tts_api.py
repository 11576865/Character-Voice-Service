"""Minimal IndexTTS-2.5 HTTP sidecar for Character Voice Service.

Run this with the IndexTTS environment, not the CVS environment. The sidecar
owns exactly one IndexTTS2 object and serializes inference with a lock.

Environment variables:
  INDEX_TTS_MODEL_DIR       default: <cwd>/checkpoints
  INDEX_TTS_CONFIG          default: <model-dir>/config.yaml
  INDEX_TTS_HOST            default: 127.0.0.1
  INDEX_TTS_PORT            default: 9882
  INDEX_TTS_USE_BF16        default: 1
  INDEX_TTS_USE_QWEN_EMO    default: 0
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOST = os.environ.get("INDEX_TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("INDEX_TTS_PORT", "9882"))
MODEL_DIR = Path(
    os.environ.get("INDEX_TTS_MODEL_DIR", str(Path.cwd() / "checkpoints"))
).expanduser().resolve()
CONFIG_PATH = Path(
    os.environ.get("INDEX_TTS_CONFIG", str(MODEL_DIR / "config.yaml"))
).expanduser().resolve()
USE_BF16 = os.environ.get("INDEX_TTS_USE_BF16", "1") == "1"
USE_QWEN_EMO = os.environ.get("INDEX_TTS_USE_QWEN_EMO", "0") == "1"

SUPPORTED_LANGUAGES = {"zh", "en", "ja", "es", "ar"}
_INFER_LOCK = threading.Lock()
_SHUTDOWN_REQUESTED = threading.Event()


def normalize_language(value: object) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "zh-cn": "zh",
        "zh-hans": "zh",
        "cn": "zh",
        "en-us": "en",
        "en-gb": "en",
        "ja-jp": "ja",
        "jp": "ja",
        "es-es": "es",
        "ar-sa": "ar",
    }
    language = aliases.get(raw, raw)
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"unsupported IndexTTS-2.5 language: {value!r}; "
            f"supported: {', '.join(sorted(SUPPORTED_LANGUAGES))}"
        )
    return language


def load_tts():
    from indextts.infer_v2_5 import IndexTTS2

    return IndexTTS2(
        cfg_path=str(CONFIG_PATH),
        model_dir=str(MODEL_DIR),
        use_bf16=USE_BF16,
        use_cuda_kernel=False,
        use_deepspeed=False,
        use_accel=False,
        use_torch_compile=False,
        use_qwen_emo=USE_QWEN_EMO,
    )


class IndexTTSServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_class, tts):
        super().__init__(address, handler_class)
        self.tts = tts

    def request_shutdown(self, reason: str) -> None:
        if _SHUTDOWN_REQUESTED.is_set():
            return
        _SHUTDOWN_REQUESTED.set()
        print(f"IndexTTS sidecar shutdown requested: {reason}", flush=True)
        threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "CVS-IndexTTS/0.2"

    def json_response(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            tts = self.server.tts
            self.json_response(
                200,
                {
                    "status": "ready",
                    "engine": "index-tts",
                    "model": "IndexTTS-2.5",
                    "precision": "bf16" if bool(getattr(tts, "use_bf16", False)) else "fp32",
                    "qwen_emotion": bool(getattr(tts, "qwen_emo", None) is not None),
                    "max_concurrency": 1,
                    "output_sample_rate": 22050,
                },
            )
            return
        if self.path == "/capabilities":
            self.json_response(
                200,
                {
                    "engine": "index-tts",
                    "model": "IndexTTS-2.5",
                    "languages": sorted(SUPPORTED_LANGUAGES),
                    "speaker_reference": True,
                    "emotion_reference": True,
                    "emotion_vector": True,
                    "emotion_text": USE_QWEN_EMO,
                    "streaming": False,
                    "supports_cancel": False,
                    "max_concurrency": 1,
                    "output_sample_rate": 22050,
                },
            )
            return
        self.json_response(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/shutdown":
            try:
                client_ip = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                client_ip = None
            if client_ip is None or not client_ip.is_loopback:
                self.json_response(403, {"error": "shutdown is loopback-only"})
                return
            self.json_response(202, {"status": "stopping", "engine": "index-tts"})
            self.server.request_shutdown("HTTP /shutdown")
            return

        if self.path != "/synthesize":
            self.json_response(404, {"error": "not found"})
            return

        if _SHUTDOWN_REQUESTED.is_set():
            self.json_response(503, {"error": "server is stopping"})
            return

        output_path = None
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
            if not Path(speaker_audio).is_file():
                raise ValueError(f"speaker_audio does not exist: {speaker_audio}")

            duration_factor = float(request.get("duration_factor", 1.0))
            if not 0.5 <= duration_factor <= 2.0:
                raise ValueError("duration_factor must be between 0.5 and 2.0")

            kwargs = {
                "spk_audio_prompt": speaker_audio,
                "text": text,
                "lang": normalize_language(request.get("lang")),
                "emo_alpha": float(request.get("emo_alpha", 1.0)),
                "use_emo_text": bool(request.get("use_emo_text", False)),
                "emo_text": request.get("emotion_text"),
                "use_random": bool(request.get("use_random", False)),
                "interval_silence": int(request.get("interval_silence", 200)),
                "max_text_tokens_per_segment": int(
                    request.get("max_text_tokens_per_segment", 120)
                ),
                "duration_factor": duration_factor,
                "text_normalization": bool(request.get("text_normalization", True)),
                "verbose": False,
            }

            emotion_audio = request.get("emotion_audio")
            emotion_vector = request.get("emotion_vector")
            if emotion_audio:
                emotion_audio = str(emotion_audio)
                if not Path(emotion_audio).is_file():
                    raise ValueError(f"emotion_audio does not exist: {emotion_audio}")
                kwargs["emo_audio_prompt"] = emotion_audio
            if emotion_vector is not None:
                if not isinstance(emotion_vector, list) or len(emotion_vector) != 8:
                    raise ValueError("emotion_vector must contain exactly 8 numbers")
                kwargs["emo_vector"] = [float(item) for item in emotion_vector]

            if kwargs["use_emo_text"] and not USE_QWEN_EMO:
                raise ValueError(
                    "use_emo_text requires INDEX_TTS_USE_QWEN_EMO=1 at sidecar startup"
                )

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                output_path = Path(handle.name)
            kwargs["output_path"] = str(output_path)

            with _INFER_LOCK:
                self.server.tts.infer(**kwargs)

            audio = output_path.read_bytes()
            if not audio:
                raise RuntimeError("IndexTTS produced an empty WAV")

            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("X-IndexTTS-Model", "IndexTTS-2.5")
            self.send_header(
                "X-IndexTTS-Precision",
                "bf16" if bool(getattr(self.server.tts, "use_bf16", False)) else "fp32",
            )
            self.end_headers()
            self.wfile.write(audio)
        except ValueError as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[IndexTTS] {self.address_string()} - {fmt % args}")


def main() -> int:
    if not MODEL_DIR.is_dir():
        raise SystemExit(f"IndexTTS model directory not found: {MODEL_DIR}")
    if not CONFIG_PATH.is_file():
        raise SystemExit(f"IndexTTS config not found: {CONFIG_PATH}")

    print(f"Loading IndexTTS-2.5 from {MODEL_DIR}")
    print(
        "Runtime policy: "
        f"bf16={USE_BF16}, qwen_emotion={USE_QWEN_EMO}, "
        "cuda_kernel=False, deepspeed=False, max_concurrency=1"
    )
    tts = load_tts()
    server = IndexTTSServer((HOST, PORT), Handler, tts)
    print(f"IndexTTS sidecar ready: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
