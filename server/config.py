from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOICE_DIR = PROJECT_ROOT / "voices"
WEB_DIR = PROJECT_ROOT / "web"

GPT_SOVITS_BASE_URL = "http://127.0.0.1:9880"
GPT_SOVITS_TTS_URL = f"{GPT_SOVITS_BASE_URL}/tts"
GPT_SOVITS_HEALTH_URL = f"{GPT_SOVITS_BASE_URL}/docs"
GPT_SOVITS_SET_GPT_WEIGHTS_URL = f"{GPT_SOVITS_BASE_URL}/set_gpt_weights"
GPT_SOVITS_SET_SOVITS_WEIGHTS_URL = f"{GPT_SOVITS_BASE_URL}/set_sovits_weights"

HOST = "0.0.0.0"
PORT = 9881

SAVE_GENERATED_WAV = True
SAVE_DIR = Path.home() / "Music"
