from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOICE_DIR = PROJECT_ROOT / "voices"
WEB_DIR = PROJECT_ROOT / "web"

GPT_SOVITS_TTS_URL = "http://127.0.0.1:9880/tts"
GPT_SOVITS_HEALTH_URL = "http://127.0.0.1:9880/docs"

HOST = "0.0.0.0"
PORT = 9881

SAVE_GENERATED_WAV = True
SAVE_DIR = Path.home() / "Music"
