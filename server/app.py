import json
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.backends.gpt_sovits import synthesize
from server.config import (
    GPT_SOVITS_HEALTH_URL,
    HOST,
    PORT,
    SAVE_DIR,
    SAVE_GENERATED_WAV,
    VOICE_DIR,
    WEB_DIR,
)
from server.voice_profiles import (
    iter_real_profile_paths,
    public_profile_summary,
    read_valid_profile,
    resolve_profile_selection,
)


app = FastAPI(
    title="Character Voice Service",
    version="0.1.0",
)
app.mount("/reader-assets", StaticFiles(directory=WEB_DIR), name="reader-assets")


class SpeechRequest(BaseModel):
    model: str = "gpt-sovits"
    voice: str
    model_id: str | None = None
    reference_id: str | None = None
    input: str
    response_format: str = "wav"
    speed: float = 1.0


def load_voice_profile(name: str) -> dict:
    if not name or any(part in name for part in ("/", "\\", "..")):
        raise HTTPException(status_code=400, detail="invalid voice name")
    if name.casefold() == "example":
        raise HTTPException(status_code=404, detail="example is a template, not a voice")

    profile_path = VOICE_DIR / f"{name}.json"

    if not profile_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Voice profile not found: {name}",
        )

    try:
        profile = read_valid_profile(profile_path)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid voice profile JSON: {name}",
        ) from exc

    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid voice profile {name}: {exc}",
        )

    return profile


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
def root():
    return {
        "service": "character-voice-service",
        "version": app.version,
        "health": "/health",
        "voices": "/v1/voices",
        "test_page": "/test",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    try:
        urllib.request.urlopen(GPT_SOVITS_HEALTH_URL, timeout=2)
        backend_status = "ready"
    except Exception:
        backend_status = "offline"

    return {
        "status": "ok",
        "service": "character-voice-service",
        "backend": "gpt-sovits",
        "backend_status": backend_status,
    }


@app.get("/v1/voices")
def voices():
    VOICE_DIR.mkdir(parents=True, exist_ok=True)

    items = []
    for path in iter_real_profile_paths(VOICE_DIR):
        try:
            profile = read_valid_profile(path)
            items.append(public_profile_summary(path.stem, profile))
        except Exception:
            items.append(
                {
                    "id": path.stem,
                    "name": path.stem,
                    "error": "invalid profile",
                }
            )

    return {"voices": items}


@app.get("/test")
def test_page():
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="test page not found")
    return FileResponse(page)


@app.post("/v1/audio/speech")
def speech(request: SpeechRequest):
    if not request.input.strip():
        raise HTTPException(status_code=400, detail="input cannot be empty")

    if request.response_format != "wav":
        raise HTTPException(
            status_code=400,
            detail="current version supports WAV only",
        )

    if request.speed <= 0:
        raise HTTPException(status_code=400, detail="speed must be greater than 0")

    if request.model != "gpt-sovits":
        raise HTTPException(status_code=400, detail="current version supports gpt-sovits only")

    profile = load_voice_profile(request.voice)
    try:
        selection = resolve_profile_selection(
            profile,
            model_id=request.model_id,
            reference_id=request.reference_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc

    audio = synthesize(
        text=request.input,
        speed=request.speed,
        profile=selection,
    )

    saved_path = save_wav(audio)
    headers = {}
    if saved_path is not None:
        headers["X-Generated-Filename"] = saved_path.name

    return Response(
        content=audio,
        media_type="audio/wav",
        headers=headers,
    )


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
