import threading

from fastapi.testclient import TestClient

import server.app as api
from server.book_library import BookLibrary
from tests.test_app import write_registry_profile
from tests.test_book_api import wav_bytes


def setup(tmp_path, monkeypatch):
    library = BookLibrary(tmp_path / "data")
    monkeypatch.setattr(api, "library", library)
    directory = tmp_path / "voices"
    directory.mkdir()
    write_registry_profile(directory)
    monkeypatch.setattr(api, "VOICE_DIR", directory)
    books = []
    for title in ("First", "Second"):
        books.append(library.put_book({"title": title, "chapters": [
            {"title": "One", "paragraphs": ["Hello."]}]}, [
            {"chapterIndex": 0, "paragraphIndex": 0, "start": 0, "end": 6,
             "text": "Hello."}], kind="manual"))
    client = TestClient(api.app)
    client.headers["X-CVS-Token"] = api.ADMIN_TOKEN
    return library, books, client


SETTINGS = {"voice": "march-7th", "model_id": "self-400", "reference_id": "neutral",
            "fixed_reference_id": "neutral", "speed": 1.0, "continuous_emotion": False,
            "continuity_span": 1, "speaker_analysis": False, "use_annotations": False}


def test_settings_persist_independently_and_invalid_changes_are_rejected(tmp_path, monkeypatch):
    library, books, client = setup(tmp_path, monkeypatch)
    route = f"/v1/books/{books[0]['id']}"
    assert client.put(route + "/reading-settings", json=SETTINGS).status_code == 200
    second = {**SETTINGS, "speed": 1.2, "reference_id": "auto", "fixed_reference_id": "surprised",
              "continuous_emotion": True, "continuity_span": 2, "speaker_analysis": True}
    other = f"/v1/books/{books[1]['id']}"
    assert client.put(other + "/reading-settings", json=second).status_code == 200
    monkeypatch.setattr(api, "library", BookLibrary(library.root))
    assert client.get(route).json()["readingSettings"] == SETTINGS
    assert client.get(other).json()["readingSettings"] == second
    assert library.job(books[0]["id"])["status"] == "none"
    assert client.put(route + "/reading-settings", json={**SETTINGS, "speed": 0}).status_code == 400
    assert client.put(route + "/reading-settings", json={**SETTINGS, "reference_id": "missing"}).status_code == 404
    assert client.get(route).json()["readingSettings"] == SETTINGS
    assert TestClient(api.app).put(route + "/reading-settings", json=SETTINGS).status_code == 401


def test_legacy_job_restore_and_saving_settings_marks_audio_stale(tmp_path, monkeypatch):
    library, books, client = setup(tmp_path, monkeypatch)
    book_id = books[0]["id"]
    route = f"/v1/books/{book_id}"
    monkeypatch.setattr(api, "synthesize", lambda *args: wav_bytes())
    api._generate_book(book_id, {**SETTINGS, "target_segments": [books[0]["segments"][0]["id"]]},
                       threading.Event())
    assert "readingSettings" not in library.get_book(book_id)
    assert client.get(route).json()["readingSettings"] == SETTINGS
    assert client.get(route + "/offline-manifest").json()["book"]["readingSettings"] == SETTINGS
    assert client.put(route + "/reading-settings", json={**SETTINGS, "speed": 1.2}).status_code == 200
    assert client.get(route + "/audio-status").json()["stale"] == 1
    assert client.get(route + "/complete.wav").status_code == 409
    assert library.job(book_id)["settings"]["speed"] == 1.0
    assert client.put(route + "/reading-settings", json=SETTINGS).status_code == 200
    assert client.get(route + "/audio-status").json()["ready"] == 1


def test_generation_saves_preferences_without_target_scope_and_blocks_edits(tmp_path, monkeypatch):
    library, books, client = setup(tmp_path, monkeypatch)
    book_id = books[0]["id"]
    route = f"/v1/books/{book_id}"
    monkeypatch.setattr(api.generation_pool, "submit", lambda *args: None)
    try:
        assert client.post(route + "/generate", json={**SETTINGS, "paragraphs": ["0:0"]}).status_code == 200
        assert library.get_book(book_id)["readingSettings"] == SETTINGS
        assert client.put(route + "/reading-settings", json={**SETTINGS, "speed": 1.2}).status_code == 409
        assert library.get_book(book_id)["readingSettings"] == SETTINGS
    finally:
        api.active_jobs.pop(book_id, None)
