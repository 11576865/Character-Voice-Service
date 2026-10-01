import json
import urllib.error
import urllib.request

from fastapi import HTTPException

from server.config import INDEX_TTS_BASE_URL
from server.runtime_registry import runtime_base_url


class IndexTTSAdapter:
    @property
    def engine_id(self) -> str:
        return "index-tts"

    def capabilities(self) -> dict:
        return {
            "zero_shot": True,
            "fine_tuned_model": False,
            "shared_engine_model": True,
            "audio_streaming": False,
            "text_streaming": False,
            "emotion": "reference-or-vector",
            "emotion_text": False,
            "speed_control": "duration-factor",
            "pronunciation_control": "upstream-annotations",
            "output_sample_rate": 22050,
            "max_concurrency": 1,
            "supports_cancel": False,
            "model_switch_cost": "none",
            "speaker_switch_cost": "backend-dependent",
        }

    def health(self) -> dict:
        try:
            health_url = runtime_base_url("index-tts", INDEX_TTS_BASE_URL) + "/health"
            with urllib.request.urlopen(health_url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return {
                "status": payload.get("status", "ready"),
                "model": payload.get("model", "IndexTTS-2.5"),
                "precision": payload.get("precision"),
            }
        except Exception as exc:
            return {"status": "offline", "error": str(exc)}

    def load_model(self, model: dict) -> None:
        # IndexTTS 2.5 uses one shared engine model owned by the sidecar.
        return None

    def unload_model(self, model_id: str) -> None:
        # Residency is controlled by the IndexTTS sidecar process.
        return None

    def synthesize(self, text: str, speed: float, profile: dict) -> bytes:
        if speed <= 0:
            raise HTTPException(status_code=400, detail="speed must be greater than 0")

        params = dict(profile.get("parameters") or {})
        duration_factor = params.get("duration_factor")
        if duration_factor is None:
            duration_factor = 1.0 / float(speed)
        try:
            duration_factor = float(duration_factor)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="IndexTTS duration_factor must be numeric"
            ) from exc
        if not 0.5 <= duration_factor <= 2.0:
            raise HTTPException(
                status_code=400,
                detail="IndexTTS duration_factor must be between 0.5 and 2.0",
            )

        payload = {
            "text": text,
            "lang": profile["target_language"],
            "speaker_audio": profile["reference_audio"],
            "duration_factor": duration_factor,
            "emo_alpha": float(params.get("emo_alpha", 1.0)),
            "emotion_audio": (
                params.get("emotion_audio")
                or params.get("emo_audio_prompt")
                or None
            ),
            "emotion_vector": params.get("emotion_vector") or params.get("emo_vector"),
            "use_emo_text": bool(params.get("use_emo_text", False)),
            "emotion_text": params.get("emotion_text") or params.get("emo_text"),
            "use_random": bool(params.get("use_random", False)),
            "interval_silence": int(params.get("interval_silence", 200)),
            "max_text_tokens_per_segment": int(
                params.get("max_text_tokens_per_segment", 120)
            ),
            "text_normalization": bool(params.get("text_normalization", True)),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        tts_url = runtime_base_url("index-tts", INDEX_TTS_BASE_URL) + "/synthesize"
        request = urllib.request.Request(
            tts_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                content_type = response.headers.get("Content-Type", "")
                audio = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HTTPException(
                status_code=502, detail=f"IndexTTS synthesis failed: {detail}"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"IndexTTS sidecar unavailable: {exc}"
            ) from exc

        if "audio/" not in content_type.lower():
            raise HTTPException(
                status_code=502,
                detail=f"IndexTTS returned non-audio response: {content_type}",
            )
        if not audio:
            raise HTTPException(status_code=502, detail="IndexTTS returned empty audio")
        return audio
