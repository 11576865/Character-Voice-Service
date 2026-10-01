from __future__ import annotations

import json
import urllib.error
import urllib.request

from fastapi import HTTPException

from server.engine_registry import EngineDescriptor
from server.runtime_registry import runtime_base_url


class SidecarV1Adapter:
    """
    Generic HTTP adapter for engines that implement the Character Voice
    Sidecar Protocol v1. Engine-specific inference code stays outside CVS.
    """

    def __init__(self, descriptor: EngineDescriptor):
        self.descriptor = descriptor

    @property
    def engine_id(self) -> str:
        return self.descriptor.engine_id

    def capabilities(self) -> dict:
        return dict(self.descriptor.capabilities)

    def _path(self, key: str, default: str) -> str:
        value = str(self.descriptor.protocol.get(key) or default).strip()
        return value if value.startswith("/") else "/" + value

    def health(self) -> dict:
        base = runtime_base_url(self.engine_id, "")
        if not base:
            return {
                "status": "offline",
                "error": f"no Runtime Registry endpoint for {self.engine_id}",
            }
        url = base + self._path("health_path", "/health")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read()
            if "application/json" in content_type.lower():
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, dict):
                    return {
                        "status": str(payload.get("status") or "ready"),
                        **{
                            key: value
                            for key, value in payload.items()
                            if key != "status"
                        },
                    }
            return {"status": "ready"}
        except Exception as exc:
            return {"status": "offline", "error": str(exc)}

    def load_model(self, model: dict) -> None:
        return None

    def unload_model(self, model_id: str) -> None:
        return None

    def synthesize(self, text: str, speed: float, profile: dict) -> bytes:
        base = runtime_base_url(self.engine_id, "")
        if not base:
            raise HTTPException(
                status_code=503,
                detail=f"no Runtime Registry endpoint for {self.engine_id}",
            )

        selected_model = profile.get("selected_model") or {}
        selected_reference = profile.get("selected_reference") or {}
        emotion_reference = profile.get("selected_emotion_reference")
        binding = profile.get("selected_binding")

        payload = {
            "contract": "character-voice-sidecar-v1",
            "engine": self.engine_id,
            "input": text,
            "speed": float(speed),
            "target_language": profile.get("target_language"),
            "model": {
                "id": selected_model.get("model_id") or selected_model.get("id"),
                "alias": selected_model.get("id"),
                "revision": selected_model.get("revision"),
                "version": selected_model.get("version"),
            },
            "speaker_reference": {
                "id": selected_reference.get("id"),
                "audio": selected_reference.get("audio") or profile.get("reference_audio"),
                "text": selected_reference.get("text") or profile.get("reference_text"),
                "language": selected_reference.get("language")
                or profile.get("reference_language"),
                "aux_audio": list(
                    selected_reference.get("aux_audio")
                    or profile.get("aux_reference_audio")
                    or []
                ),
            },
            "emotion_reference": (
                {
                    "id": emotion_reference.get("id"),
                    "audio": emotion_reference.get("audio"),
                    "text": emotion_reference.get("text"),
                    "language": emotion_reference.get("language"),
                }
                if emotion_reference
                else None
            ),
            "binding": (
                {
                    "id": binding.get("binding_id"),
                    "revision": binding.get("revision"),
                    "emotion_policy": binding.get("emotion_policy"),
                }
                if binding
                else None
            ),
            "parameters": dict(profile.get("parameters") or {}),
        }

        url = base + self._path("synthesize_path", "/v1/synthesize")
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = float(self.descriptor.protocol.get("timeout_seconds") or 300)

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                audio = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HTTPException(
                status_code=502,
                detail=f"{self.engine_id} sidecar synthesis failed: {detail}",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"{self.engine_id} sidecar unavailable: {exc}",
            ) from exc

        if "audio/" not in content_type.lower():
            raise HTTPException(
                status_code=502,
                detail=f"{self.engine_id} returned non-audio response: {content_type}",
            )
        if not audio:
            raise HTTPException(
                status_code=502,
                detail=f"{self.engine_id} returned empty audio",
            )
        return audio
