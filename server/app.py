import json
import hashlib
import hmac
import time
import threading
import wave
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.backends.gpt_sovits import synthesize
from server.book_library import BookLibrary
from server.emotion_router import choose_reference
from server.epub_export import export_read_aloud
from server.document_import import parse_document
from server.speaker_suggestions import suggest_speakers
from server.pronunciations import spoken_text
from server.config import (
    ADMIN_TOKEN,
    DATA_DIR,
    GPT_SOVITS_HEALTH_URL,
    HOST,
    PORT,
    PROJECT_ROOT,
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
FOLIATE_DIR = WEB_DIR.parent / "vendor" / "foliate-js"
if FOLIATE_DIR.is_dir():
    app.mount("/foliate-assets", StaticFiles(directory=FOLIATE_DIR), name="foliate-assets")
library = BookLibrary(DATA_DIR)
generation_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cvs-book")
active_jobs: dict[str, threading.Event] = {}
jobs_lock = threading.Lock()


class SpeechRequest(BaseModel):
    model: str = "gpt-sovits"
    voice: str
    model_id: str | None = None
    reference_id: str | None = None
    input: str
    response_format: str = "wav"
    speed: float = 1.0


class BookRequest(BaseModel):
    document: dict
    segments: list[dict]
    kind: str
    author: str = ""
    client_document_id: str | None = None


class GenerationRequest(BaseModel):
    voice: str
    model_id: str | None = None
    reference_id: str | None = None
    speed: float = 1.0
    continuous_emotion: bool = False


class SelectionRequest(BaseModel):
    version_id: str


class LoginRequest(BaseModel):
    token: str


def _session_value() -> str:
    issued = str(int(time.time()))
    signature = hmac.new(ADMIN_TOKEN.encode(), issued.encode(), "sha256").hexdigest()
    return f"{issued}.{signature}"


def _valid_session(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    issued, signature = value.split(".", 1)
    if not issued.isdigit() or abs(time.time() - int(issued)) > 7 * 24 * 3600:
        return False
    expected = hmac.new(ADMIN_TOKEN.encode(), issued.encode(), "sha256").hexdigest()
    return hmac.compare_digest(signature, expected)


def require_admin(request: Request, x_cvs_token: str | None = Header(default=None)):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Set CVS_ADMIN_TOKEN before using the private library")
    if not _valid_session(request.cookies.get("cvs_session")) and \
            (not x_cvs_token or not hmac.compare_digest(x_cvs_token, ADMIN_TOKEN)):
        raise HTTPException(status_code=401, detail="Invalid library token")


@app.post("/v1/session")
def login(request: LoginRequest, http_request: Request):
    if not hmac.compare_digest(request.token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid library token")
    secure = http_request.url.hostname not in {"127.0.0.1", "localhost"}
    response = Response(content=json.dumps({"authenticated": True}), media_type="application/json")
    response.set_cookie("cvs_session", _session_value(), httponly=True, secure=secure,
                        samesite="lax", max_age=7 * 24 * 3600, path="/")
    return response


@app.get("/v1/storage", dependencies=[Depends(require_admin)])
def storage_locations(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return {
        "books_root": str((DATA_DIR / "books").resolve()),
        "references_root": str((PROJECT_ROOT / "references").resolve()),
        "realtime_wav_root": str(SAVE_DIR.resolve()) if SAVE_GENERATED_WAV else None,
    }


def get_book_or_404(book_id: str) -> dict:
    try:
        return library.get_book(book_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="Book not found") from exc


def effective_selection(voice: str, model_id: str | None, reference_id: str | None,
                        text: str) -> tuple[dict, str]:
    profile = load_voice_profile(voice)
    if reference_id == "auto":
        reference_id, _ = choose_reference(profile, text)
    try:
        return resolve_profile_selection(profile, model_id=model_id,
                                         reference_id=reference_id), reference_id or profile["default_reference"]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc


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


@app.get("/v1")
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


@app.get("/v1/voices/{voice_id}/references/{reference_id}/audio",
         dependencies=[Depends(require_admin)])
def reference_audio(voice_id: str, reference_id: str):
    profile = load_voice_profile(voice_id)
    reference = profile["references"].get(reference_id)
    if not reference:
        raise HTTPException(status_code=404, detail="Reference not found")
    path = Path(reference["audio"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Reference WAV is missing")
    return FileResponse(path, media_type="audio/wav")


@app.get("/test")
def test_page():
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="test page not found")
    return FileResponse(page)


@app.get("/", include_in_schema=False)
def reader_page():
    return test_page()


@app.get("/service-worker.js", include_in_schema=False)
def service_worker():
    return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/"})


@app.get("/epub-prototype", include_in_schema=False)
def foliate_prototype():
    if not FOLIATE_DIR.is_dir():
        raise HTTPException(status_code=404, detail="foliate-js submodule is missing")
    return FileResponse(WEB_DIR / "foliate-prototype.html", headers={
        "Content-Security-Policy": "default-src 'self' blob:; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; "
        "frame-src blob:; img-src 'self' blob: data:; media-src 'self' blob:"
    })


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
    reference_id = request.reference_id
    selection_reason = "manual" if reference_id else "default"
    if reference_id == "auto":
        reference_id, selection_reason = choose_reference(profile, request.input)
    try:
        selection = resolve_profile_selection(
            profile,
            model_id=request.model_id,
            reference_id=reference_id,
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
    headers["X-Selected-Reference"] = selection["selected_reference"]["id"]
    headers["X-Reference-Reason"] = selection_reason
    if saved_path is not None:
        headers["X-Generated-Filename"] = saved_path.name

    return Response(
        content=audio,
        media_type="audio/wav",
        headers=headers,
    )


@app.get("/v1/books", dependencies=[Depends(require_admin)])
def list_books():
    return {"books": library.list_books()}


@app.post("/v1/books", dependencies=[Depends(require_admin)])
def create_book(request: BookRequest):
    try:
        book = library.put_book(request.document, request.segments,
                                kind=request.kind, author=request.author,
                                client_document_id=request.client_document_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": book["id"], "title": book["title"], "segments": len(book["segments"])}


@app.get("/v1/books/{book_id}", dependencies=[Depends(require_admin)])
def get_book(book_id: str):
    return get_book_or_404(book_id)


@app.put("/v1/books/{book_id}/source", dependencies=[Depends(require_admin)])
async def save_book_source(book_id: str, request: Request, kind: str):
    get_book_or_404(book_id)
    if request.headers.get("content-length") and int(request.headers["content-length"]) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Source file too large")
    source = await request.body()
    try:
        library.put_source(book_id, source, kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"stored": True, "bytes": len(source)}


@app.put("/v1/books/{book_id}/annotations", dependencies=[Depends(require_admin)])
def save_book_annotations(book_id: str, annotations: dict):
    get_book_or_404(book_id)
    try:
        library.save_annotations(book_id, annotations)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"saved": len(annotations)}


@app.get("/v1/books/{book_id}/speaker-suggestions", dependencies=[Depends(require_admin)])
def book_speaker_suggestions(book_id: str):
    return {"suggestions": suggest_speakers(get_book_or_404(book_id), voices()["voices"])}


@app.put("/v1/books/{book_id}/pronunciations", dependencies=[Depends(require_admin)])
def save_book_pronunciations(book_id: str, rules: dict):
    get_book_or_404(book_id)
    try:
        library.save_pronunciations(book_id, rules)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"saved": len(rules)}


@app.get("/v1/books/{book_id}/progress", dependencies=[Depends(require_admin)])
def get_book_progress(book_id: str):
    get_book_or_404(book_id)
    return library.progress(book_id)


@app.put("/v1/books/{book_id}/progress", dependencies=[Depends(require_admin)])
def save_book_progress(book_id: str, progress: dict):
    get_book_or_404(book_id)
    try:
        return library.save_progress(book_id, progress)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/books/{book_id}/versions", dependencies=[Depends(require_admin)])
def get_versions(book_id: str):
    get_book_or_404(book_id)
    return library.versions(book_id)


@app.get("/v1/books/{book_id}/audio/{segment_id}", dependencies=[Depends(require_admin)])
def get_selected_audio(book_id: str, segment_id: str):
    get_book_or_404(book_id)
    path = library.audio_path(book_id, segment_id)
    if not path:
        raise HTTPException(status_code=404, detail="Audio not generated")
    return FileResponse(path, media_type="audio/wav")


@app.get("/v1/books/{book_id}/offline-manifest", dependencies=[Depends(require_admin)])
def offline_manifest(book_id: str):
    get_book_or_404(book_id)
    try:
        manifest = library.offline_manifest(book_id)
        manifest["voices"] = voices()["voices"]
        return manifest
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/v1/books/{book_id}/offline-audio/{segment_id}", dependencies=[Depends(require_admin)])
def offline_audio(book_id: str, segment_id: str):
    get_book_or_404(book_id)
    if not any(item["id"] == segment_id for item in get_book_or_404(book_id)["segments"]):
        raise HTTPException(status_code=404, detail="Segment not found")
    try:
        path = library.compressed_audio(book_id, segment_id)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if path is None:
        raise HTTPException(status_code=404, detail="Audio not generated")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/v1/books/{book_id}/read-aloud.epub", dependencies=[Depends(require_admin)])
def read_aloud_epub(book_id: str):
    get_book_or_404(book_id)
    try:
        path = export_read_aloud(library, book_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return FileResponse(path, filename=f"{book_id}-read-aloud.epub",
                        media_type="application/epub+zip")


@app.post("/v1/documents/parse", dependencies=[Depends(require_admin)])
async def parse_uploaded_document(request: Request, kind: str, name: str):
    data = await request.body()
    try:
        return {"document": parse_document(data, kind, name)}
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/books/{book_id}/complete.wav", dependencies=[Depends(require_admin)])
def complete_wav(book_id: str):
    get_book_or_404(book_id)
    try:
        path = library.combined_wav(book_id)
    except (ValueError, OSError, wave.Error) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, filename=f"{book_id}-complete.wav", media_type="audio/wav")


@app.post("/v1/books/{book_id}/segments/{segment_id}/select", dependencies=[Depends(require_admin)])
def select_book_version(book_id: str, segment_id: str, request: SelectionRequest):
    get_book_or_404(book_id)
    try:
        library.select_version(book_id, segment_id, request.version_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"selected": request.version_id}


def _book_plan(book: dict, settings: dict) -> list[dict]:
    """Resolve the same paragraph voices and references used by generation."""
    continuity = {"chapter": None, "voice": None, "paragraph": None,
                  "reference": None, "reason": None, "held": False}
    profiles = {}
    plan = []
    for segment in book["segments"]:
        chapter_index = segment["chapterIndex"]
        paragraph_index = segment["paragraphIndex"]
        paragraph = (chapter_index, paragraph_index)
        key = f"{chapter_index}:{paragraph_index}"
        override = book.get("annotations", {}).get(key, {})
        voice = override.get("voice") or settings["voice"]
        model_id = override.get("model_id") or \
            (settings.get("model_id") if voice == settings["voice"] else None)
        requested = override.get("reference_id")
        if not requested:
            requested = ("auto" if settings.get("reference_id") == "auto" else None) \
                if voice != settings["voice"] else settings.get("reference_id")
        if voice not in profiles:
            profiles[voice] = load_voice_profile(voice)
        profile = profiles[voice]
        paragraph_text = book["document"]["chapters"][chapter_index]["paragraphs"][paragraph_index]
        if continuity["paragraph"] == paragraph and continuity["voice"] == voice:
            reference_id, reason = continuity["reference"], continuity["reason"]
        elif requested == "auto":
            reference_id, reason = choose_reference(profile, paragraph_text)
            same_context = continuity["chapter"] == chapter_index and continuity["voice"] == voice
            if settings.get("continuous_emotion") and \
                    reason.startswith("default: ambiguous or no emotion cue") and \
                    same_context and continuity["reference"] != profile["default_reference"] and \
                    not continuity["held"]:
                reference_id = continuity["reference"]
                reason = "continuity: held previous reference once"
                continuity["held"] = True
            else:
                continuity["held"] = False
        else:
            reference_id = requested or profile["default_reference"]
            reason = "manual override" if override.get("reference_id") else \
                ("fixed reference" if requested else "role default")
            continuity["held"] = True
        continuity.update(chapter=chapter_index, voice=voice, paragraph=paragraph,
                          reference=reference_id, reason=reason)
        text_to_speak = spoken_text(segment["text"], book.get("pronunciations", {}))
        selection, actual_reference = effective_selection(
            voice, model_id, reference_id, text_to_speak)
        plan.append({"segment": segment, "paragraph": key, "text": paragraph_text,
                     "voice": voice, "reference_id": actual_reference,
                     "model_id": selection["selected_model"]["id"], "reason": reason,
                     "spoken_text": text_to_speak, "selection": selection})
    return plan


@app.post("/v1/books/{book_id}/plan", dependencies=[Depends(require_admin)])
def preview_book_plan(book_id: str, request: GenerationRequest):
    book = get_book_or_404(book_id)
    if request.speed <= 0:
        raise HTTPException(status_code=400, detail="Speed must be positive")
    plan = _book_plan(book, request.model_dump())
    paragraphs = []
    seen = set()
    for item in plan:
        if item["paragraph"] in seen:
            continue
        seen.add(item["paragraph"])
        paragraphs.append({key: item[key] for key in
                           ("paragraph", "text", "voice", "reference_id", "model_id", "reason")})
    return {"paragraphs": paragraphs}


def _generate_book(book_id: str, settings: dict, cancel: threading.Event):
    book = library.get_book(book_id)
    total = len(book["segments"])
    job = {"status": "running", "completed": 0, "total": total,
           "error": None, "settings": settings, "updatedAt": _now_iso()}
    library.set_job(book_id, job)
    try:
        for item in _book_plan(book, settings):
            if cancel.is_set():
                job["status"] = "cancelled"
                break
            segment = item["segment"]
            voice = item["voice"]
            text_to_speak = item["spoken_text"]
            selection = item["selection"]
            actual_reference = item["reference_id"]
            fingerprint = hashlib.sha256(json.dumps({
                "text": text_to_speak, "voice": voice,
                "model": selection["selected_model"],
                "reference": selection["selected_reference"],
                "speed": settings["speed"],
            }, sort_keys=True).encode()).hexdigest()
            existing = library.versions(book_id).get(segment["id"], {})
            chosen = next((version for version in existing.get("versions", [])
                           if version["id"] == existing.get("selected")), None)
            if not chosen or chosen.get("metadata", {}).get("fingerprint") != fingerprint or \
                    not library.audio_path(book_id, segment["id"]):
                audio = synthesize(text_to_speak, settings["speed"], selection)
                library.add_version(book_id, segment["id"], audio, {
                    "voice": voice, "model_id": selection["selected_model"]["id"],
                    "reference_id": actual_reference, "speed": settings["speed"],
                    "fingerprint": fingerprint,
                    "pronunciationsUpdatedAt": book.get("pronunciationsUpdatedAt"),
                })
            job["completed"] += 1
            job["updatedAt"] = _now_iso()
            library.set_job(book_id, job)
        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
    finally:
        job["updatedAt"] = _now_iso()
        try:
            library.set_job(book_id, job)
        finally:
            with jobs_lock:
                active_jobs.pop(book_id, None)


def _now_iso():
    return datetime.now().astimezone().isoformat()


@app.post("/v1/books/{book_id}/generate", dependencies=[Depends(require_admin)])
def generate_book(book_id: str, request: GenerationRequest):
    get_book_or_404(book_id)
    if request.speed <= 0:
        raise HTTPException(status_code=400, detail="Speed must be positive")
    load_voice_profile(request.voice)
    with jobs_lock:
        if book_id in active_jobs:
            raise HTTPException(status_code=409, detail="Book generation already running")
        cancel = threading.Event()
        active_jobs[book_id] = cancel
    settings = request.model_dump()
    library.set_job(book_id, {"status": "queued", "completed": 0,
                              "total": len(get_book_or_404(book_id)["segments"]),
                              "settings": settings, "updatedAt": _now_iso(), "error": None})
    generation_pool.submit(_generate_book, book_id, settings, cancel)
    return {"status": "queued", "book_id": book_id}


@app.get("/v1/books/{book_id}/job", dependencies=[Depends(require_admin)])
def book_job(book_id: str):
    get_book_or_404(book_id)
    job = library.job(book_id)
    with jobs_lock:
        active = book_id in active_jobs
    if job["status"] in {"running", "queued"} and not active:
        job["status"] = "interrupted"
    return job


@app.post("/v1/books/{book_id}/cancel", dependencies=[Depends(require_admin)])
def cancel_book_job(book_id: str):
    get_book_or_404(book_id)
    with jobs_lock:
        cancel = active_jobs.get(book_id)
    if cancel:
        cancel.set()
    return {"cancelling": bool(cancel)}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
