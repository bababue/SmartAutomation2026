"""Upload endpoint: file type and page cap checks run before any LLM call."""

import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

from api.main import app
from pipeline import config, store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store.connect, "__defaults__", (path,))
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "MAX_UPLOAD_PAGES", 2)
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10_000)
    store.connect(path).close()
    return TestClient(app)


def _pdf_bytes(n_pages: int) -> bytes:
    pdf = pymupdf.open()
    for i in range(n_pages):
        pdf.new_page().insert_text((72, 72), f"page {i + 1}")
    return pdf.tobytes()


def test_rejects_unsupported_file_type(client):
    res = client.post("/api/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert res.status_code == 415


def test_rejects_file_over_the_page_cap(client):
    res = client.post("/api/upload", files={"file": ("log.pdf", _pdf_bytes(3), "application/pdf")})
    assert res.status_code == 413
    assert "3 pages" in res.json()["detail"]


def test_rejects_file_over_the_byte_cap(client):
    res = client.post("/api/upload", files={"file": ("log.pdf", _pdf_bytes(1) + b"\0" * 20_000, "application/pdf")})
    assert res.status_code == 413
    assert "MB upload limit" in res.json()["detail"]


def test_strips_directory_components_from_the_filename(client, monkeypatch):
    monkeypatch.setattr("pipeline.__main__.run", lambda *a, **k: None)
    res = client.post("/api/upload", files={"file": ("../../evil.pdf", _pdf_bytes(1), "application/pdf")})
    assert res.status_code == 200
    saved = list((config.UPLOADS_DIR / f"upload-{res.json()['job_id']}").iterdir())
    assert [p.name for p in saved] == ["evil.pdf"]  # no path components escaped the upload's own folder


def test_accepts_file_within_the_page_cap(client, monkeypatch):
    calls = []

    def fake_run(docs, max_pages=None, yes=True, dataset_dir=config.DATASET_DIR):
        calls.append(docs)

    monkeypatch.setattr("pipeline.__main__.run", fake_run)
    res = client.post("/api/upload", files={"file": ("log.pdf", _pdf_bytes(2), "application/pdf")})
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    assert res.json()["pages"] == 2

    status = {}
    for _ in range(50):
        status = client.get(f"/api/upload/{job_id}").json()
        if status["status"] != "processing":
            break
        time.sleep(0.05)
    assert status["status"] == "done"
    assert calls  # run() was invoked from the background thread, without touching the LLM
