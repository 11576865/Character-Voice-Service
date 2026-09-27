import threading

from fastapi.testclient import TestClient

import server.app as api
from server.book_library import BookLibrary
from server.emotion_router import choose_reference
from tests.test_book_api import wav_bytes
from tests.test_book_plan import _profiles


def setup(tmp_path, monkeypatch, chapters, annotations=None, split_first=False):
    _profiles(monkeypatch)
    library = BookLibrary(tmp_path)
    monkeypatch.setattr(api, "library", library)
    document = {"title": "Continuity", "chapters": [
        {"title": str(i), "paragraphs": texts} for i, texts in enumerate(chapters)]}
    segments = []
    for ci, texts in enumerate(chapters):
        for pi, text in enumerate(texts):
            offsets = [0, len(text)]
            if split_first and ci == pi == 0:
                offsets.insert(1, 5)
            segments.extend({"chapterIndex": ci, "paragraphIndex": pi, "start": start,
                             "end": end, "text": text[start:end]}
                            for start, end in zip(offsets, offsets[1:]))
    book = library.put_book(document, segments, kind="manual")
    if annotations:
        library.save_annotations(book["id"], annotations)
    return library, library.get_book(book["id"])


SETTINGS = {"voice": "a", "model_id": None, "reference_id": "auto",
            "speed": 1.0, "continuous_emotion": True, "continuity_span": 2}


def test_conflicting_cues_and_missing_reviewed_reference_end_continuity(tmp_path, monkeypatch):
    _, book = setup(tmp_path, monkeypatch, [[
        "I am happy.", "I am happy and sad.", "A quiet room.",
        "I am happy.", "I am angry.", "A quiet room."]])
    plan = list(api._book_plan(book, SETTINGS))
    assert [p["reference_id"] for p in plan] == [
        "a-happy", "a-neutral", "a-neutral", "a-happy", "a-neutral", "a-neutral"]
    assert plan[1]["emotion_plan"]["cues"] == {"happy": ["happy"], "sad": ["sad"]}
    assert plan[1]["reason"] == "default: conflicting emotion cues"
    assert plan[4]["reason"] == "default: no reviewed angry reference"


def test_span_counts_paragraphs_and_resets_on_new_chapter(tmp_path, monkeypatch):
    _, book = setup(tmp_path, monkeypatch, [
        ["I am happy.", "One room.", "Two doors.", "Three lights."],
        ["A new chapter."]], split_first=True)
    plan = list(api._book_plan(book, SETTINGS))
    assert [p["reference_id"] for p in plan] == [
        "a-happy", "a-happy", "a-happy", "a-happy", "a-neutral", "a-neutral"]
    assert plan[2]["emotion_plan"]["carried_from"] == "0:0"
    assert plan[3]["emotion_plan"]["carried_paragraphs"] == 2
    assert plan[4]["emotion_plan"]["transition"] == "limit"
    chapter = list(api._book_plan(book, SETTINGS, 1))
    assert chapter[0]["reference_id"] == plan[-1]["reference_id"]
    assert chapter[0]["emotion_plan"]["transition"] == "boundary"


def test_manual_reference_and_model_changes_break_chain(tmp_path, monkeypatch):
    _, book = setup(tmp_path, monkeypatch, [[
        "I am happy.", "Model changed.", "Still quiet.",
        "Manual paragraph.", "After manual."]], {
        "0:1": {"voice": "a", "model_id": "other"},
        "0:3": {"voice": "a", "reference_id": "a-happy"}})
    plan = list(api._book_plan(book, SETTINGS))
    assert [p["reference_id"] for p in plan] == [
        "a-happy", "a-neutral", "a-neutral", "a-happy", "a-neutral"]
    assert plan[3]["emotion_plan"] is None
    fixed = list(api._book_plan(book, {**SETTINGS, "reference_id": "a-neutral"}))
    assert [p["reference_id"] for p in fixed] == [
        "a-neutral", "a-neutral", "a-neutral", "a-happy", "a-neutral"]
    assert all(p["emotion_plan"] is None for p in fixed)


def test_preview_generation_and_reuse_share_continuity_result(tmp_path, monkeypatch):
    library, book = setup(tmp_path, monkeypatch, [["I am happy.", "One.", "Two."]])
    client = TestClient(api.app)
    headers = {"X-CVS-Token": api.ADMIN_TOKEN}
    route = f"/v1/books/{book['id']}/plan"
    preview = client.post(route, json=SETTINGS, headers=headers).json()["paragraphs"]
    assert preview[1]["emotion_plan"]["independent_reference_id"] == "a-neutral"
    used = []
    monkeypatch.setattr(api, "synthesize", lambda text, speed, selection:
                        used.append(selection["selected_reference"]["id"]) or wav_bytes())
    api._generate_book(book["id"], SETTINGS, threading.Event())
    assert used == [p["reference_id"] for p in preview]
    used.clear()
    api._generate_book(book["id"], {**SETTINGS, "continuity_span": 1}, threading.Event())
    assert used == ["a-neutral"]
    assert library.job(book["id"])["reused"] == 2
    assert client.post(route, json={**SETTINGS, "continuity_span": 10},
                       headers=headers).status_code == 422


def test_unreviewed_emotion_reference_uses_default():
    profile = {"default_reference": "neutral", "references": {
        "neutral": {"emotion": "neutral", "quality": "A"},
        "happy": {"emotion": "happy", "quality": "unrated"}}}
    assert choose_reference(profile, "I am happy.") == (
        "neutral", "default: no reviewed happy reference")
