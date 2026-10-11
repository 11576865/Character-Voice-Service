import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from server.config import DATA_DIR, GPT_SOVITS_BASE_URL
from server.runtime_registry import runtime_base_url
from server.runtime_slot_lease import acquire_runtime_slot

# Compatibility constants for callers/tests; synthesis resolves the machine-local
# Runtime Registry endpoint dynamically at call time.
GPT_SOVITS_TTS_URL = f"{GPT_SOVITS_BASE_URL}/tts"
GPT_SOVITS_SET_GPT_WEIGHTS_URL = f"{GPT_SOVITS_BASE_URL}/set_gpt_weights"
GPT_SOVITS_SET_SOVITS_WEIGHTS_URL = f"{GPT_SOVITS_BASE_URL}/set_sovits_weights"


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


def _urls() -> tuple[str, str, str]:
    base = runtime_base_url("gpt-sovits", GPT_SOVITS_BASE_URL)
    return (
        f"{base}/tts",
        f"{base}/set_gpt_weights",
        f"{base}/set_sovits_weights",
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


def _ensure_selected_model(profile: dict, urls: tuple[str, str, str]) -> None:
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
    # The old in-memory cache is NOT proof of backend state: another CVS
    # process can change weights, and a restarted GPT-SoVITS process can
    # forget them while this process retains its module globals. Until the
    # backend advertises an attested model/epoch, always reassert both weights
    # for every managed transaction, including the same model twice.
    _, set_gpt_url, set_sovits_url = urls

    _set_weight(
        set_sovits_url,
        signature[1],
        "SoVITS weight switch",
    )
    _set_weight(
        set_gpt_url,
        signature[0],
        "GPT weight switch",
    )
    _active_managed_model = signature


def _synthesize_locked(text: str, speed: float, profile: dict, urls: tuple[str, str, str]) -> bytes:
    _ensure_selected_model(profile, urls)
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
    tts_url, _, _ = urls
    request = urllib.request.Request(
        tts_url,
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
    # Local mutex prevents same-process request interleaving. The independent
    # OS lock must cover the entire weight-switch + inference transaction
    # across *all cooperating CVS processes* targeting this exact endpoint.
    # A launch/GPU-slot lock is not sufficient: it governs child lifetimes,
    # not concurrent /set_weights and /tts requests from different parents.
    with _backend_lock:
        urls = _urls()  # Freeze the backend endpoint for one transaction.
        tts_url = urls[0]
        lock_key = "gpt-txn-" + hashlib.sha256(tts_url.encode("utf-8")).hexdigest()[:32]
        try:
            lease = acquire_runtime_slot(lock_key, DATA_DIR / "inference-transactions")
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"GPT-SoVITS inference admission unavailable: {exc}",
                headers={"Retry-After": "2"},
            ) from exc
        if lease is None:
            raise HTTPException(
                status_code=503,
                detail="GPT-SoVITS backend is busy with another CVS synthesis transaction",
                headers={"Retry-After": "2"},
            )
        try:
            return _synthesize_locked(text, speed, profile, urls)
        finally:
            lease.release()
