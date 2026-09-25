import os
import secrets
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOICE_DIR = PROJECT_ROOT / "voices"
WEB_DIR = PROJECT_ROOT / "web"
DATA_DIR = Path(os.environ.get("CVS_DATA_DIR", str(PROJECT_ROOT / "data")))


def _admin_token() -> str:
    configured = os.environ.get("CVS_ADMIN_TOKEN", "").strip()
    if configured:
        return configured
    path = DATA_DIR / "admin-token.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(32)
    path.write_text(generated + "\n", encoding="utf-8")
    return generated


ADMIN_TOKEN = _admin_token()

GPT_SOVITS_BASE_URL = "http://127.0.0.1:9880"
GPT_SOVITS_TTS_URL = f"{GPT_SOVITS_BASE_URL}/tts"
GPT_SOVITS_HEALTH_URL = f"{GPT_SOVITS_BASE_URL}/docs"
GPT_SOVITS_SET_GPT_WEIGHTS_URL = f"{GPT_SOVITS_BASE_URL}/set_gpt_weights"
GPT_SOVITS_SET_SOVITS_WEIGHTS_URL = f"{GPT_SOVITS_BASE_URL}/set_sovits_weights"

HOST = "0.0.0.0"
PORT = 9881

SAVE_GENERATED_WAV = os.environ.get("CVS_SAVE_GENERATED_WAV", "0") == "1"
SAVE_DIR = Path.home() / "Music"
