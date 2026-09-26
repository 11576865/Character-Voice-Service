import io
import threading
import wave

from fastapi.testclient import TestClient

import server.app as app_module
from server.book_library import BookLibrary


def _book(library):
    paragraphs = ["I am happy.", "The door opened.", "Another voice spoke.",
                  "The room was quiet."]
    document = {"title": "Two voices", "chapters": [
        {"title": "One", "paragraphs": paragraphs}]}
    segments = [{"chapterIndex": 0, "paragraphIndex": index, "start": 0,
                 "end": len(text), "text": text}
                for index, text in enumerate(paragraphs)]
    book = library.put_book(document, segments, kind="manual")
    library.save_annotations(book["id"], {"0:2": {"voice": "b", "reference_id": None}})
    return library.get_book(book["id"])


def _profiles(monkeypatch):
    profiles = {
        "a": {"default_reference": "a-neutral", "references": {
            "a-neutral": {"emotion": "neutral", "quality": "A"},
            "a-happy": {"emotion": "happy", "quality": "A"}}},
        "b": {"default_reference": "b-neutral", "references": {
            "b-neutral": {"emotion": "neutral", "quality": "A"}}},
    }
    monkeypatch.setattr(app_module, "load_voice_profile", lambda name: profiles[name])
    def select(voice, model, reference, text):
        if voice == "b":
            assert model is None  # narrator's model ID must not leak to another role
        return ({"selected_model": {"id": "v4"},
                 "selected_reference": {"id": reference}}, reference)

    monkeypatch.setattr(app_module, "effective_selection", select)


def test_chapter_plan_holds_once_and_resets_at_role_change(tmp_path, monkeypatch):
    _profiles(monkeypatch)
    book = _book(BookLibrary(tmp_path))
    settings = {"voice": "a", "reference_id": "auto", "model_id": None,
                "speed": 1.0, "continuous_emotion": True}
    plan = list(app_module._book_plan(book, settings))
    assert [(item["voice"], item["reference_id"]) for item in plan] == [
        ("a", "a-happy"), ("a", "a-happy"), ("b", "b-neutral"), ("a", "a-neutral")]
    assert plan[1]["reason"].startswith("continuity:")
    assert "default:" in plan[3]["reason"]


def test_preview_is_private_and_other_role_uses_its_own_default(tmp_path, monkeypatch):
    _profiles(monkeypatch)
    library = BookLibrary(tmp_path)
    book = _book(library)
    monkeypatch.setattr(app_module, "library", library)
    client = TestClient(app_module.app)
    route = f"/v1/books/{book['id']}/plan"
    settings = {"voice": "a", "model_id": "a-only", "reference_id": "a-neutral", "speed": 1.0}
    assert client.post(route, json=settings).status_code == 401
    response = client.post(route, json=settings,
                           headers={"X-CVS-Token": app_module.ADMIN_TOKEN})
    assert response.status_code == 200
    paragraphs = response.json()["paragraphs"]
    assert paragraphs[2]["voice"] == "b"
    assert paragraphs[2]["reference_id"] == "b-neutral"
    assert "selected_model" not in response.text
    assert client.post(route + "?chapter_index=99", json=settings,
                       headers={"X-CVS-Token": app_module.ADMIN_TOKEN}).status_code == 400


def test_generation_uses_previewed_roles_and_references(tmp_path, monkeypatch):
    _profiles(monkeypatch)
    library = BookLibrary(tmp_path)
    book = _book(library)
    monkeypatch.setattr(app_module, "library", library)
    used = []

    def synthesize(text, speed, selection):
        used.append((text, selection["selected_reference"]["id"]))
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 160)
        return output.getvalue()

    monkeypatch.setattr(app_module, "synthesize", synthesize)
    settings = {"voice": "a", "reference_id": "auto", "model_id": None,
                "speed": 1.0, "continuous_emotion": True}
    app_module._generate_book(book["id"], settings, threading.Event())
    assert library.job(book["id"])["status"] == "completed"
    assert [reference for _, reference in used] == [
        "a-happy", "a-happy", "b-neutral", "a-neutral"]
