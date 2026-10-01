import urllib.request

from server.backends import gpt_sovits
from server.config import GPT_SOVITS_BASE_URL
from server.runtime_registry import runtime_base_url


class GPTSoVITSAdapter:
    @property
    def engine_id(self) -> str:
        return "gpt-sovits"

    def capabilities(self) -> dict:
        return {
            "zero_shot": True,
            "fine_tuned_model": True,
            "audio_streaming": False,
            "text_streaming": False,
            "emotion": "reference",
            "speed_control": "backend-dependent",
            "pronunciation_control": "limited",
            "max_concurrency": 1,
            "supports_cancel": False,
            "model_switch_cost": "high",
        }

    def health(self) -> dict:
        try:
            health_url = runtime_base_url("gpt-sovits", GPT_SOVITS_BASE_URL) + "/docs"
            urllib.request.urlopen(health_url, timeout=2)
            return {"status": "ready"}
        except Exception as exc:
            return {"status": "offline", "error": str(exc)}

    def load_model(self, model: dict) -> None:
        # GPT-SoVITS performs the weight switch lazily inside synthesize().
        return None

    def unload_model(self, model_id: str) -> None:
        gpt_sovits.reset_model_state()

    def synthesize(self, text: str, speed: float, profile: dict) -> bytes:
        return gpt_sovits.synthesize(text, speed, profile)
