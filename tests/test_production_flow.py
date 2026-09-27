import threading

from fastapi.testclient import TestClient

import server.app as api
from server.book_library import BookLibrary
from tests.test_book_plan import _profiles
from tests.test_book_api import wav_bytes


def setup_book(tmp_path, monkeypatch):
    _profiles(monkeypatch)
    library = BookLibrary(tmp_path)
    monkeypatch.setattr(api, "library", library)
    chapters = [{"title": "One", "paragraphs": ["First.", "Second."]},
                {"title": "Two", "paragraphs": ["Third."]}]
    segments = [{"chapterIndex": c, "paragraphIndex": p, "start": 0, "end": len(text), "text": text}
                for c, chapter in enumerate(chapters) for p, text in enumerate(chapter["paragraphs"])]
    book = library.put_book({"title": "Production", "chapters": chapters}, segments, kind="manual")
    settings = {"voice": "a", "model_id": None, "reference_id": "a-neutral",
                "speed": 1.0, "continuous_emotion": False}
    calls = []
    monkeypatch.setattr(api, "synthesize", lambda text, speed, selection:
                        calls.append((text, selection["selected_reference"]["id"])) or wav_bytes())
    client = TestClient(api.app)
    client.headers["X-CVS-Token"] = api.ADMIN_TOKEN
    return library, book, settings, calls, client


def test_all_chapters_and_stale_audio_cannot_be_exported(tmp_path, monkeypatch):
    library, book, settings, calls, client = setup_book(tmp_path, monkeypatch)
    book_id = book["id"]
    api._generate_book(book_id, settings, threading.Event())
    assert len(calls) == 3
    assert library.job(book_id)["completed"] == 3
    before = library.versions(book_id)
    assert client.put(f"/v1/books/{book_id}/annotations",
                      json={"0:1": {"voice": "b"}}).status_code == 200
    plan = client.post(f"/v1/books/{book_id}/plan", json=settings).json()["paragraphs"]
    assert [p["audio"] for p in plan] == [
        {"ready": 1, "stale": 0, "missing": 0},
        {"ready": 0, "stale": 1, "missing": 0},
        {"ready": 1, "stale": 0, "missing": 0}]
    for suffix in ("complete.wav", "read-aloud.epub", "offline-manifest"):
        assert client.get(f"/v1/books/{book_id}/{suffix}").status_code == 409
    changed_id = book["segments"][1]["id"]
    assert client.get(f"/v1/books/{book_id}/audio/{changed_id}").status_code == 409
    api._generate_book(book_id, settings, threading.Event())
    assert calls[3:] == [("Second.", "b-neutral")]
    assert library.job(book_id)["reused"] == 2
    summary = client.get(f"/v1/books/{book_id}/audio-status").json()
    assert summary["ready"] == summary["total"] == 3
    assert len(summary["chapters"]) == 2
    after = library.versions(book_id)
    assert after[book["segments"][0]["id"]] == before[book["segments"][0]["id"]]
    assert after[changed_id]["selected"] != before[changed_id]["selected"]
    assert client.get(f"/v1/books/{book_id}/complete.wav").status_code == 200


def test_failure_continues_and_retry_only_targets_failed_clip(tmp_path, monkeypatch):
    library, book, settings, calls, client = setup_book(tmp_path, monkeypatch)
    def fail_middle(text, speed, selection):
        calls.append(text)
        if text == "Second.":
            raise RuntimeError("temporary inference failure")
        return wav_bytes()
    monkeypatch.setattr(api, "synthesize", fail_middle)
    api._generate_book(book["id"], settings, threading.Event())
    job = library.job(book["id"])
    assert job["status"] == "completed_with_errors"
    assert job["completed"] == 2
    assert calls == ["First.", "Second.", "Third."]
    submitted = []
    monkeypatch.setattr(api.generation_pool, "submit", lambda *args: submitted.append(args))
    response = client.post(f"/v1/books/{book['id']}/generate",
                           json={**settings, "retry_failed": True})
    assert response.status_code == 200
    _, book_id, retry_settings, cancel = submitted[0]
    assert retry_settings["target_segments"] == [book["segments"][1]["id"]]
    monkeypatch.setattr(api, "synthesize", lambda *args: wav_bytes())
    api._generate_book(book_id, retry_settings, cancel)
    assert library.job(book_id)["generated"] == 1
    assert library.job(book_id)["status"] == "completed"


def test_selected_paragraph_generation_and_edit_guard(tmp_path, monkeypatch):
    library, book, settings, calls, client = setup_book(tmp_path, monkeypatch)
    submitted = []
    monkeypatch.setattr(api.generation_pool, "submit", lambda *args: submitted.append(args))
    route = f"/v1/books/{book['id']}"
    assert client.post(route + "/generate", json={**settings, "paragraphs": ["99:0"]}).status_code == 400
    assert client.post(route + "/generate", json={**settings, "paragraphs": ["1:0"]}).status_code == 200
    assert client.put(route + "/annotations", json={}).status_code == 409
    assert client.put(route + "/pronunciations", json={}).status_code == 409
    _, book_id, selected_settings, cancel = submitted[0]
    api._generate_book(book_id, selected_settings, cancel)
    assert calls == [("Third.", "a-neutral")]
    assert library.job(book_id)["total"] == 1
    assert client.get(route + "/complete.wav").status_code == 409
