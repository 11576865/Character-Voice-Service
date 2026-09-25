import json
import threading
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from server.config import (
    GPT_SOVITS_SET_GPT_WEIGHTS_URL,
    GPT_SOVITS_SET_SOVITS_WEIGHTS_URL,
    GPT_SOVITS_TTS_URL,
)


_backend_lock = threading.Lock()
_active_managed_model: tuple[str, str] | None = None


def reset_model_state() -> None:
    """Forget the service-side model cache without changing GPT-SoVITS itself."""
    global _active_managed_model
    _active_managed_model = None


def _backend_error(exc: urllib.error.HTTPError, operation: str) -> HTTPException:
    body = exc.read().decode("utf-8", errors="replace")
    return HTTPException(
        status_code=502,
        detail=f"GPT-SoVITS {operation} error: {body}",
    )


def _set_weight(url: str, path: str, label: str) -> None:
    query = urllib.parse.urlencode({"weights_path": path})
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=120) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise _backend_error(exc, label) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"GPT-SoVITS unavailable while {label}: {exc}",
        ) from exc


def _ensure_selected_model(profile: dict) -> None:
    global _active_managed_model

    model = profile.get("selected_model") or {}
    gpt_weights = model.get("gpt_weights")
    sovits_weights = model.get("sovits_weights")
    managed = bool(gpt_weights and sovits_weights)

    if not managed:
        if _active_managed_model is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The selected model is externally loaded, but this service has already "
                    "switched GPT-SoVITS to a managed model. Register weight paths for this "
                    "model or restart GPT-SoVITS and Character Voice Service before using it."
                ),
            )
        return

    signature = (str(gpt_weights), str(sovits_weights))
    if signature == _active_managed_model:
        return

    # GPT-SoVITS exposes these as separate GET control endpoints. Keep the
    # whole switch + synthesis operation under one lock so concurrent requests
    # cannot cross model boundaries.
    _set_weight(
        GPT_SOVITS_SET_SOVITS_WEIGHTS_URL,
        signature[1],
        "SoVITS weight switch",
    )
    _set_weight(
        GPT_SOVITS_SET_GPT_WEIGHTS_URL,
        signature[0],
        "GPT weight switch",
    )
    _active_managed_model = signature


def _synthesize_locked(text: str, speed: float, profile: dict) -> bytes:
    _ensure_selected_model(profile)
    params = profile.get("parameters", {})

    payload = {
        "text": text,
        "text_lang": profile["target_language"],
        "ref_audio_path": profile["reference_audio"],
        "aux_ref_audio_paths": profile.get("aux_reference_audio", []),
        "prompt_lang": profile["reference_language"],
        "prompt_text": profile["reference_text"],

        "top_k": params.get("top_k", 15),
        "top_p": params.get("top_p", 1.0),
        "temperature": params.get("temperature", 1.0),

        "text_split_method": params.get("text_split_method", "cut5"),
        "batch_size": params.get("batch_size", 1),
        "batch_threshold": params.get("batch_threshold", 0.75),
        "split_bucket": params.get("split_bucket", True),

        "speed_factor": speed,
        "fragment_interval": params.get("fragment_interval", 0.3),
        "seed": params.get("seed", -1),

        "media_type": "wav",
        "streaming_mode": False,
        "parallel_infer": params.get("parallel_infer", True),
        "repetition_penalty": params.get("repetition_penalty", 1.35),
        "sample_steps": params.get("sample_steps", 32),
        "super_sampling": params.get("super_sampling", False),
        "overlap_length": params.get("overlap_length", 2),
        "min_chunk_length": params.get("min_chunk_length", 16),
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        GPT_SOVITS_TTS_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise _backend_error(exc, "TTS") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"GPT-SoVITS unavailable: {exc}",
        ) from exc


def synthesize(text: str, speed: float, profile: dict) -> bytes:
    with _backend_lock:
        return _synthesize_locked(text, speed, profile)
