import threading

from fastapi.testclient import TestClient

import server.app as api
from tests.test_reading_settings import setup


def test_task_overview_requires_login_and_projects_interrupted_jobs(tmp_path, monkeypatch):
    library, books, client = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "active_jobs", {})
    assert TestClient(api.app).get("/v1/tasks").status_code == 401
    assert client.get("/v1/tasks").json() == {"tasks": []}
    first, second = (book["id"] for book in books)
    library.set_job(first, {"status": "running", "completed": 1, "total": 3,
                           "settings": {"paragraphs": ["0:0"], "private": "secret"},
                           "updatedAt": "2026-09-28T01:00:00Z"})
    library.set_job(second, {"status": "queued", "completed": 0, "total": 1})
    api.active_jobs[second] = threading.Event()
    tasks = client.get("/v1/tasks").json()["tasks"]
    assert [task["bookId"] for task in tasks] == [second, first]
    assert tasks[1]["status"] == "interrupted"
    assert tasks[1]["scope"] == "选定段落"
    assert "secret" not in str(tasks)
    assert library.job(first)["status"] == "running"
    assert client.get(f"/v1/books/{first}/job").json()["status"] == "interrupted"


def test_unreadable_task_does_not_hide_other_books(tmp_path, monkeypatch):
    library, books, client = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "active_jobs", {})
    library.job_path(books[0]["id"]).write_text("invalid", encoding="utf-8")
    library.set_job(books[1]["id"], {"status": "completed_with_errors", "completed": 2,
                                   "total": 3, "errors": [{"error": "private path"}],
                                   "settings": {"retry_failed": True}})
    tasks = client.get("/v1/tasks").json()["tasks"]
    assert len(tasks) == 2
    assert {task["status"] for task in tasks} == {"unreadable", "completed_with_errors"}
    failed = next(task for task in tasks if task["failures"])
    assert failed["scope"] == "失败项重试"
    assert "private path" not in str(tasks)
