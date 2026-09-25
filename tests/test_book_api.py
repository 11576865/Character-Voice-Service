import io
import time
import wave

from fastapi.testclient import TestClient

import server.app as app_module
from server.book_library import BookLibrary
from tests.test_app import write_registry_profile


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 320)
    return output.getvalue()


def test_private_book_generation_and_exports(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "library", BookLibrary(tmp_path / "data"))
    monkeypatch.setattr(app_module, "VOICE_DIR", tmp_path / "voices")
    app_module.VOICE_DIR.mkdir()
    write_registry_profile(app_module.VOICE_DIR)
    calls = []
    monkeypatch.setattr(app_module, "synthesize", lambda text, speed, profile:
                        calls.append((text, profile["selected_reference"]["id"])) or wav_bytes())
    client = TestClient(app_module.app, base_url="https://testserver")
    document = {"title": "Demo", "chapters": [{"title": "One", "paragraphs": ["Surprised!"]}]}
    segments = [{"chapterIndex": 0, "paragraphIndex": 0, "start": 0,
                 "end": 10, "text": "Surprised!"}]
    payload = {"document": document, "segments": segments, "kind": "txt"}
    assert client.post("/v1/books", json=payload).status_code == 401
    assert client.post("/v1/session", json={"token": "wrong"}).status_code == 401
    assert client.post("/v1/session", json={"token": app_module.ADMIN_TOKEN}).status_code == 200
    assert client.get("/v1/books").status_code == 200
    headers = {"X-CVS-Token": app_module.ADMIN_TOKEN}
    created = client.post("/v1/books", json=payload, headers=headers)
    assert created.status_code == 200
    book_id = created.json()["id"]
    assert client.put(f"/v1/books/{book_id}/source?kind=txt", headers=headers,
                      content=b"Surprised!").status_code == 200
    assert client.put(f"/v1/books/{book_id}/annotations", headers=headers,
                      json={"0:0": {"voice": "march-7th", "reference_id": "surprised"}}).status_code == 200
    started = client.post(f"/v1/books/{book_id}/generate", headers=headers,
                          json={"voice": "march-7th"})
    assert started.status_code == 200
    for _ in range(100):
        job = client.get(f"/v1/books/{book_id}/job", headers=headers).json()
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert job["status"] == "completed", job
    assert calls == [("Surprised!", "surprised")]
    segment_id = client.get(f"/v1/books/{book_id}", headers=headers).json()["segments"][0]["id"]
    assert client.get(f"/v1/books/{book_id}/audio/{segment_id}", headers=headers).content.startswith(b"RIFF")
    assert client.get(f"/v1/books/{book_id}/offline-manifest", headers=headers).json()["clips"][0]["sha256"]
    assert client.get(f"/v1/books/{book_id}/read-aloud.epub", headers=headers).status_code == 200
    assert client.get(f"/v1/books/{book_id}/complete.wav", headers=headers).content.startswith(b"RIFF")
    assert client.put(f"/v1/books/{book_id}/progress", headers=headers,
                      json={"segmentIndex": 0, "audioTime": 0.4}).status_code == 200
