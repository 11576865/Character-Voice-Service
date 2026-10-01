import hashlib
import hmac
import json
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from server.config import ADMIN_TOKEN, HOST, PORT, SAVE_DIR, SAVE_GENERATED_WAV, VOICE_DIR
from server.emotion_router import choose_reference
from server.engines import get_adapter, list_engines, synthesize
from server.runtime_supervisor import RuntimeSupervisorError, runtime_supervisor
from server.model_registry import (
    list_models,
    promote_model,
    retire_model,
    scan_model_root,
    sha256_file,
)
from server.runtime_supervisor import (
    RuntimeConflictError,
    RuntimeSupervisorError,
    runtime_supervisor,
)
from server.voice_profiles import (
    iter_real_profile_paths,
    public_profile_summary,
    read_valid_profile,
    resolve_profile_selection,
)


app = FastAPI(title="Character Voice Service", version="0.2.0")


class SpeechRequest(BaseModel):
    model: str | None = None
    voice: str
    model_id: str | None = None
    reference_id: str | None = None
    input: str
    response_format: str = "wav"
    speed: float = 1.0


def require_admin(x_cvs_token: str | None = Header(default=None)):
    if not x_cvs_token or not hmac.compare_digest(x_cvs_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid CVS admin token")


def load_voice_profile(name: str) -> dict:
    if not name or any(part in name for part in ("/", "\\", "..")):
        raise HTTPException(status_code=400, detail="invalid voice name")
    if name.casefold() == "example":
        raise HTTPException(status_code=404, detail="example is a template, not a voice")
    profile_path = VOICE_DIR / f"{name}.json"
    if not profile_path.exists():
        raise HTTPException(status_code=404, detail=f"Voice profile not found: {name}")
    try:
        return read_valid_profile(profile_path)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid voice profile JSON: {name}") from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=500, detail=f"Invalid voice profile {name}: {exc}") from exc


def _reference_revision(reference: dict | None) -> str | None:
    if not reference:
        return None
    path = Path(str(reference.get("audio") or ""))
    if path.is_file():
        return sha256_file(path)
    fallback = {
        "id": reference.get("id"),
        "text": reference.get("text"),
        "language": reference.get("language"),
    }
    return hashlib.sha256(
        json.dumps(
            fallback,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _revision_parameters(parameters: dict) -> dict:
    path_keys = {
        "emotion_audio",
        "emo_audio_prompt",
        "speaker_audio",
        "reference_audio",
    }
    return {
        key: value
        for key, value in parameters.items()
        if key not in path_keys
    }


def save_wav(audio: bytes) -> Path | None:
    if not SAVE_GENERATED_WAV:
        return None
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = uuid.uuid4().hex[:6]
    output_path = SAVE_DIR / f"character_voice_{timestamp}_{short_id}.wav"
    output_path.write_bytes(audio)
    print(f"Saved WAV: {output_path}")
    return output_path


@app.get("/")
@app.get("/v1")
def root():
    return {
        "service": "character-voice-service",
        "version": app.version,
        "contract": "Character Voice Contract v1",
        "health": "/health",
        "voices": "/v1/voices",
        "models": "/v1/models",
        "engines": "/v1/engines",
        "runtime": "/v1/runtime",
        "speech": "/v1/audio/speech",
        "runtime": "/v1/runtime",
        "docs": "/docs",
    }


@app.get("/health/live")
def health_live():
    return {"status": "ok", "service": "character-voice-service"}

@app.get("/health")
def health():
    engines = []
    for item in list_engines():
        engine_id = item["engine"]
        status = get_adapter(engine_id).health()
        engines.append({"engine": engine_id, **status})
    return {"status": "ok", "service": "character-voice-service", "engines": engines}


@app.get("/v1/engines")
def engines():
    return {"engines": list_engines()}


@app.get("/v1/runtime")
def runtime_info():
    try:
        return runtime_supervisor.diagnostics()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Runtime Registry error: {exc}") from exc


@app.post("/v1/admin/runtime/{engine_id}/start", dependencies=[Depends(require_admin)])
def runtime_start(engine_id: str):
    try:
        return runtime_supervisor.start(engine_id)
    except (RuntimeSupervisorError, OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/v1/admin/runtime/{engine_id}/stop", dependencies=[Depends(require_admin)])
def runtime_stop(engine_id: str):
    try:
        return runtime_supervisor.stop(engine_id)
    except (RuntimeSupervisorError, OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/v1/admin/runtime/{engine_id}/restart", dependencies=[Depends(require_admin)])
def runtime_restart(engine_id: str):
    try:
        return runtime_supervisor.restart(engine_id)
    except (RuntimeSupervisorError, OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/v1/runtime")
def runtime_info():
    try:
        return runtime_supervisor.diagnostics()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Runtime Registry error: {exc}") from exc


@app.post("/v1/admin/runtime/{engine_id}/start", dependencies=[Depends(require_admin)])
def start_runtime(engine_id: str):
    try:
        return runtime_supervisor.start(engine_id)
    except RuntimeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeSupervisorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Runtime start failed: {exc}") from exc


@app.post("/v1/admin/runtime/{engine_id}/stop", dependencies=[Depends(require_admin)])
def stop_runtime(engine_id: str):
    try:
        return runtime_supervisor.stop(engine_id)
    except RuntimeSupervisorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Runtime stop failed: {exc}") from exc


@app.post("/v1/admin/runtime/{engine_id}/restart", dependencies=[Depends(require_admin)])
def restart_runtime(engine_id: str):
    try:
        return runtime_supervisor.restart(engine_id)
    except RuntimeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeSupervisorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Runtime restart failed: {exc}") from exc


@app.get("/v1/models")
def models():
    try:
        return {"models": list_models()}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Model Registry error: {exc}") from exc


@app.post("/v1/admin/models/sync", dependencies=[Depends(require_admin)])
def sync_models():
    try:
        return scan_model_root()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Model sync failed: {exc}") from exc


@app.post("/v1/admin/models/{model_id}/promote", dependencies=[Depends(require_admin)])
def promote_registered_model(model_id: str):
    try:
        promote_model(model_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"model_id": model_id, "status": "default"}


@app.post("/v1/admin/models/{model_id}/retire", dependencies=[Depends(require_admin)])
def retire_registered_model(model_id: str):
    try:
        retire_model(model_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"model_id": model_id, "status": "retired"}


@app.get("/v1/voices")
def voices():
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in iter_real_profile_paths(VOICE_DIR):
        try:
            profile = read_valid_profile(path)
            items.append(public_profile_summary(path.stem, profile))
        except Exception:
            items.append({"id": path.stem, "name": path.stem, "error": "invalid profile"})
    return {"voices": items}


@app.get(
    "/v1/voices/{voice_id}/references/{reference_id}/audio",
    dependencies=[Depends(require_admin)],
)
def reference_audio(voice_id: str, reference_id: str):
    profile = load_voice_profile(voice_id)
    reference = profile["references"].get(reference_id)
    if not reference:
        raise HTTPException(status_code=404, detail="Reference not found")
    path = Path(reference["audio"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Reference WAV is missing")
    return FileResponse(path, media_type="audio/wav")


@app.post("/v1/audio/speech")
def speech(request: SpeechRequest):
    if not request.input.strip():
        raise HTTPException(status_code=400, detail="input cannot be empty")
    if request.response_format != "wav":
        raise HTTPException(status_code=400, detail="current version supports WAV only")
    if request.speed <= 0:
        raise HTTPException(status_code=400, detail="speed must be greater than 0")

    profile = load_voice_profile(request.voice)
    reference_id = request.reference_id
    selection_reason = "manual" if reference_id else "default"
    if reference_id == "auto":
        reference_id, selection_reason = choose_reference(profile, request.input)

    try:
        selection = resolve_profile_selection(
            profile, model_id=request.model_id, reference_id=reference_id
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc

    selected_model = selection["selected_model"]
    selected_engine = str(selected_model.get("engine") or "gpt-sovits")
    if request.model and request.model not in {
        selected_engine,
        selected_model.get("id"),
        selected_model.get("model_id"),
    }:
        raise HTTPException(
            status_code=400,
            detail="model field no longer selects the inference engine; use model_id",
        )

    try:
        runtime_supervisor.ensure_ready(selected_engine)
    except RuntimeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeSupervisorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    audio = synthesize(text=request.input, speed=request.speed, profile=selection)
    saved_path = save_wav(audio)

    request_id = uuid.uuid4().hex
    model_identity = selected_model.get("model_id") or selected_model["id"]
    revision = selected_model.get("revision")
    binding = selection.get("selected_binding")
    emotion_reference = selection.get("selected_emotion_reference")
    generation_revision = hashlib.sha256(
        json.dumps(
            {
                "voice": request.voice,
                "engine": selected_engine,
                "adapter_api_version": selected_model.get("adapter_api_version", "1"),
                "model_id": model_identity,
                "model_revision": revision,
                "binding_revision": binding.get("revision") if binding else None,
                "speaker_reference_id": selection["selected_reference"]["id"],
                "speaker_reference_revision": _reference_revision(
                    selection["selected_reference"]
                ),
                "emotion_reference_id": (
                    emotion_reference.get("id") if emotion_reference else None
                ),
                "emotion_reference_revision": _reference_revision(emotion_reference),
                "emotion_policy": binding.get("emotion_policy") if binding else None,
                "parameters": _revision_parameters(selection.get("parameters") or {}),
                "speed": request.speed,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    headers = {
        "X-CVS-Request-ID": request_id,
        "X-CVS-Voice": request.voice,
        "X-CVS-Model": str(model_identity),
        "X-CVS-Engine": selected_engine,
        "X-CVS-Generation-Revision": generation_revision,
        "X-Selected-Reference": selection["selected_reference"]["id"],
        "X-Reference-Reason": selection_reason,
    }
    if revision:
        headers["X-CVS-Model-Revision"] = str(revision)
    if binding:
        headers["X-CVS-Binding"] = str(binding["binding_id"])
        headers["X-CVS-Binding-Revision"] = str(binding["revision"])
    if saved_path is not None:
        headers["X-Generated-Filename"] = saved_path.name

    return Response(content=audio, media_type="audio/wav", headers=headers)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
