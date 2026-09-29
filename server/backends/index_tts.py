from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from fastapi import HTTPException

from server.backends.base import TTSEngine
from server.config import INDEX_TTS_HEALTH_URL, INDEX_TTS_TTS_URL


def _http_error(exc: urllib.error.HTTPError, operation: str) -> HTTPException:
    body = exc.read().decode("utf-8", errors="replace")
    return HTTPException(
        status_code=502,
        detail=f"IndexTTS {operation} error: {body}",
    )


class IndexTTSEngine(TTSEngine):
    engine_id = "index-tts"
    display_name = "IndexTTS 2.5"

    def health(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(INDEX_TTS_HEALTH_URL, timeout=2) as response:
                response.read()
            return {"engine": self.engine_id, "status": "ready"}
        except Exception as exc:
            return {
                "engine": self.engine_id,
                "status": "offline",
                "detail": str(exc),
            }

    def capabilities(self) -> dict[str, Any]:
        return {
            "speaker_reference": True,
            "separate_emotion_reference": True,
            "emotion_text": True,
            "emotion_vector": True,
            "duration_control": True,
            "managed_character_weights": False,
            "languages": "multilingual",
        }

    def synthesize(self, text: str, speed: float, profile: dict) -> bytes:
        params = dict(profile.get("parameters") or {})
        duration_factor = params.get("duration_factor")
        if duration_factor is None:
            duration_factor = 1.0 / speed
        try:
            duration_factor = float(duration_factor)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="IndexTTS duration_factor must be numeric") from exc
        if not 0.5 <= duration_factor <= 2.0:
            raise HTTPException(
                status_code=400,
                detail="IndexTTS duration_factor must be between 0.5 and 2.0",
            )

        emotion_audio = (
            profile.get("emotion_reference_audio")
            or params.get("emotion_audio")
            or params.get("emo_audio_prompt")
        )

        payload = {
            "text": text,
            "lang": profile["target_language"],
            "speaker_audio": profile["reference_audio"],
            "speed": speed,
            "duration_factor": duration_factor,
            "emotion_audio": emotion_audio,
            "emo_alpha": float(params.get("emo_alpha", 0.6)),
            "emotion_vector": params.get("emotion_vector") or params.get("emo_vector"),
            "use_emo_text": bool(params.get("use_emo_text", False)),
            "emotion_text": params.get("emotion_text") or params.get("emo_text"),
            "use_random": bool(params.get("use_random", False)),
            "interval_silence": int(params.get("interval_silence", 200)),
            "max_text_tokens_per_segment": int(params.get("max_text_tokens_per_segment", 120)),
            "text_normalization": bool(params.get("text_normalization", True)),
        }

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            INDEX_TTS_TTS_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                content_type = response.headers.get("Content-Type", "")
                audio = response.read()
                if "audio/" not in content_type.lower():
                    raise HTTPException(
                        status_code=502,
                        detail=f"IndexTTS returned non-audio response: {content_type}",
                    )
                return audio
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, "TTS") from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"IndexTTS unavailable: {exc}",
            ) from exc
