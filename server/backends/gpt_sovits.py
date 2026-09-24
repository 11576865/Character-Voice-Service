import json
import urllib.error
import urllib.request

from fastapi import HTTPException

from server.config import GPT_SOVITS_TTS_URL


def synthesize(text: str, speed: float, profile: dict) -> bytes:
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
        error_body = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=502,
            detail=f"GPT-SoVITS error: {error_body}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"GPT-SoVITS unavailable: {exc}",
        ) from exc
