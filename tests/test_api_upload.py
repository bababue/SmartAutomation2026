"""Upload endpoint: batches, zip extraction, and the type, size and page checks before any LLM call."""

import io
import json
import time
import zipfile
from datetime import datetime, timezone

import pymupdf
import pytest
from fastapi.testclient import TestClient

from api.main import app
from pipeline import config, store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "MAX_UPLOAD_PAGES", 2)
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10_000)
    monkeypatch.setattr("pipeline.runner.missing_credentials", lambda: None)
    store.migrate(path)
    with TestClient(app) as client:
        yield client


def _pdf_bytes(n_pages: int) -> bytes:
    pdf = pymupdf.open()
    for i in range(n_pages):
        pdf.new_page().insert_text((72, 72), f"page {i + 1}")
    return pdf.tobytes()


def test_rejects_unsupported_file_type(client):
    res = client.post("/api/uploads", files={"files": ("notes.csv", b"hello", "text/csv")})
    assert res.status_code == 415


def test_rejects_file_over_the_page_cap(client):
    res = client.post("/api/uploads", files={"files": ("log.pdf", _pdf_bytes(3), "application/pdf")})
    assert res.status_code == 413
    assert "3 pages" in res.json()["detail"]
    assert not any(config.UPLOADS_DIR.iterdir())


def test_rejects_unreadable_file_and_leaves_nothing_behind(client):
    res = client.post("/api/uploads", files={"files": ("log.pdf", b"not a pdf", "application/pdf")})
    assert res.status_code == 422
    assert not any(config.UPLOADS_DIR.iterdir())
    assert not config.PAGES_DIR.exists() or not any(config.PAGES_DIR.iterdir())


def test_rejects_upload_without_credentials_before_saving(client, monkeypatch):
    monkeypatch.setattr("pipeline.runner.missing_credentials", lambda: "No key")
    res = client.post("/api/uploads", files={"files": ("log.pdf", _pdf_bytes(1), "application/pdf")})
    assert res.status_code == 503
    assert not config.UPLOADS_DIR.exists()


def test_rejects_file_over_the_byte_cap(client):
    res = client.post("/api/uploads", files={"files": ("log.pdf", _pdf_bytes(1) + b"\0" * 20_000, "application/pdf")})
    assert res.status_code == 413
    assert "MB upload limit" in res.json()["detail"]


def test_strips_directory_components_from_the_filename(client, monkeypatch):
    monkeypatch.setattr("pipeline.runner.run", lambda *a, **k: None)
    res = client.post("/api/uploads", files={"files": ("../../evil.pdf", _pdf_bytes(1), "application/pdf")})
    assert res.status_code == 200
    saved = list((config.UPLOADS_DIR / f"upload-{res.json()['job_id']}").iterdir())
    assert [p.name for p in saved] == ["evil.pdf"]  # no path components escaped the upload's own folder


def test_accepts_file_within_the_page_cap(client, monkeypatch):
    calls = []

    def fake_run(docs, **kwargs):
        calls.append(docs)

    monkeypatch.setattr("pipeline.runner.run", fake_run)
    res = client.post("/api/uploads", files={"files": ("log.pdf", _pdf_bytes(2), "application/pdf")})
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    assert res.json()["pages"] == 2

    status = {}
    for _ in range(50):
        status = client.get(f"/api/uploads/{job_id}").json()
        if status["status"] not in ("queued", "processing"):
            break
        time.sleep(0.05)
    assert status["status"] == "done"
    assert calls  # run() was invoked from the background thread, without touching the LLM


def test_uploaded_document_flows_through_review_and_export(client, monkeypatch):
    from pipeline.extraction_models import LogRef, PageClassification, PageExtraction
    from tests.test_assemble import _log

    classification = PageClassification(kind="borehole_log", logs=[LogRef(name="BH1", sheet=None)],
                                        language="en", has_coordinates=True, crs_text=None)
    log = _log("BH1", "E 412000 N 5320000", 300, [(0, 9)]).model_copy(update={"final_depth_m": 9})
    monkeypatch.setattr("pipeline.llm.classify_page", lambda *args: classification)
    monkeypatch.setattr("pipeline.llm.extract_page", lambda *args: PageExtraction(logs=[log], coordinate_rows=[]))
    response = client.post("/api/uploads", files={"files": ("invented.pdf", _pdf_bytes(1), "application/pdf")})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    for _ in range(100):
        result = client.get(f"/api/uploads/{job_id}").json()
        if result["status"] not in ("queued", "processing"):
            break
        time.sleep(0.01)
    assert result["status"] == "done" and len(result["boreholes"]) == 1
    bid = result["boreholes"][0]["id"]
    detail = client.get(f"/api/boreholes/{bid}").json()
    assert len(detail["source_pages"]) == 1
    assert client.get(detail["source_pages"][0]["url"]).status_code == 200
    assert bid not in client.get("/api/export.csv").text
    reviewed = client.post(f"/api/boreholes/{bid}/review", json={"decision": "approved", "reviewer": "Ana",
                           "content_hash": detail["content_hash"], "review_revision": detail["review_revision"]})
    assert reviewed.status_code == 200
    assert bid in client.get("/api/export.csv").text


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _wait(client, job_id):
    for _ in range(200):
        result = client.get(f"/api/uploads/{job_id}").json()
        if result["status"] not in ("queued", "processing"):
            return result
        time.sleep(0.01)
    pytest.fail("upload did not finish")


def test_files_of_one_batch_form_one_document_group_in_one_run(client, monkeypatch):
    from pipeline.extraction_models import LogRef, PageClassification, PageExtraction
    from tests.test_assemble import _log

    runs = []
    real_run = __import__("pipeline.runner", fromlist=["run"]).run

    def recording_run(docs, **kwargs):
        runs.append([d.doc_id for d in docs])
        real_run(docs, **kwargs)

    classification = PageClassification(kind="borehole_log", logs=[LogRef(name="BH1", sheet=None)],
                                        language="en", has_coordinates=True, crs_text=None)
    log = _log("BH1", "E 412000 N 5320000", 300, [(0, 9)]).model_copy(update={"final_depth_m": 9})
    monkeypatch.setattr("pipeline.runner.run", recording_run)
    monkeypatch.setattr("pipeline.llm.classify_page", lambda *args: classification)
    monkeypatch.setattr("pipeline.llm.extract_page", lambda *args: PageExtraction(logs=[log], coordinate_rows=[]))
    res = client.post("/api/uploads", files=[("files", ("log.pdf", _pdf_bytes(1), "application/pdf")),
                                            ("files", ("report.pdf", _pdf_bytes(1), "application/pdf"))])
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    assert res.json()["files"] == ["log.pdf", "report.pdf"]

    result = _wait(client, job_id)
    assert result["status"] == "done"
    assert len(runs) == 1 and len(runs[0]) == 2
    assert {store.document_group(d) for d in runs[0]} == {f"upload-{job_id}"}
    assert len(result["boreholes"]) == 1  # the same hole in both files is combined into one record


def test_zip_keeps_only_documents_and_skips_archive_metadata(client, monkeypatch):
    monkeypatch.setattr("pipeline.runner.run", lambda *a, **k: None)
    archive = _zip_bytes({"logs/a.pdf": _pdf_bytes(1), "notes.csv": b"x", "__MACOSX/logs/._a.pdf": b"meta"})
    res = client.post("/api/uploads", files={"files": ("batch.zip", archive, "application/zip")})
    assert res.status_code == 200
    assert res.json()["files"] == ["logs/a.pdf"]
    assert res.json()["skipped"] == ["notes.csv"]


def test_batch_skips_other_files_beside_its_documents(client, monkeypatch):
    monkeypatch.setattr("pipeline.runner.run", lambda *a, **k: None)
    res = client.post("/api/uploads", files=[("files", ("log.pdf", _pdf_bytes(1), "application/pdf")),
                                            ("files", ("site.shp", b"x", "application/octet-stream")),
                                            ("files", ("site.gpkg", b"x", "application/octet-stream"))])
    assert res.status_code == 200
    assert res.json()["files"] == ["log.pdf"]
    assert res.json()["skipped"] == ["site.shp", "site.gpkg"]
    assert [p.name for p in (config.UPLOADS_DIR / f"upload-{res.json()['job_id']}").iterdir()] == ["log.pdf"]


def test_rejects_zip_with_a_traversing_entry_and_leaves_nothing_behind(client):
    archive = _zip_bytes({"a.pdf": _pdf_bytes(1), "../escaped.pdf": _pdf_bytes(1)})
    res = client.post("/api/uploads", files={"files": ("batch.zip", archive, "application/zip")})
    assert res.status_code == 400
    assert "unsafe path" in res.json()["detail"]
    assert not any(config.UPLOADS_DIR.iterdir())
    assert not (config.UPLOADS_DIR.parent / "escaped.pdf").exists()


def test_page_cap_applies_to_the_whole_batch(client):
    res = client.post("/api/uploads", files=[("files", ("a.pdf", _pdf_bytes(2), "application/pdf")),
                                            ("files", ("b.pdf", _pdf_bytes(1), "application/pdf"))])
    assert res.status_code == 413
    assert "3 pages" in res.json()["detail"]
    assert not any(config.UPLOADS_DIR.iterdir())


def test_byte_cap_applies_to_the_extracted_zip(client):
    archive = _zip_bytes({"a.pdf": _pdf_bytes(1) + b"\0" * 50_000})  # compresses far below the cap
    assert len(archive) < config.MAX_UPLOAD_BYTES
    res = client.post("/api/uploads", files={"files": ("batch.zip", archive, "application/zip")})
    assert res.status_code == 413
    assert not any(config.UPLOADS_DIR.iterdir())


def test_status_reports_the_running_stage(client, monkeypatch):
    import threading

    reported, release = threading.Event(), threading.Event()

    def fake_run(docs, progress=None, **kwargs):
        progress("extract", 3, 7)
        reported.set()
        assert release.wait(3)

    monkeypatch.setattr("pipeline.runner.run", fake_run)
    job_id = client.post("/api/uploads", files={"files": ("log.pdf", _pdf_bytes(1), "application/pdf")}).json()["job_id"]
    assert reported.wait(3)
    try:
        status = client.get(f"/api/uploads/{job_id}").json()
        assert status["status"] == "processing"
        assert status["progress"] == {"stage": "extract", "done": 3, "total": 7}
    finally:
        release.set()
    assert _wait(client, job_id)["progress"] is None


def test_a_file_sent_again_is_reported_not_processed_twice(client, monkeypatch):
    monkeypatch.setattr("pipeline.runner.run", lambda *a, **k: None)
    log, other = _pdf_bytes(1), _pdf_bytes(1)  # each call makes a PDF with its own id
    first = client.post("/api/uploads", files={"files": ("log.pdf", log, "application/pdf")})
    assert first.status_code == 200
    again = client.post("/api/uploads", files={"files": ("renamed.pdf", log, "application/pdf")})
    assert again.status_code == 409 and "already uploaded: renamed.pdf (as log.pdf" in again.json()["detail"]
    mixed = client.post("/api/uploads", files=[("files", ("a-new.pdf", other, "application/pdf")),
                                              ("files", ("b-copy.pdf", other, "application/pdf")),
                                              ("files", ("log.pdf", log, "application/pdf"))])
    assert mixed.status_code == 200
    assert mixed.json()["files"] == ["a-new.pdf"]
    dups = {d["file"]: (d["of"], d["at"] is None) for d in mixed.json()["duplicates"]}
    assert dups == {"b-copy.pdf": ("a-new.pdf", True), "log.pdf": ("log.pdf", False)}
    folder = config.UPLOADS_DIR / f"upload-{mixed.json()['job_id']}"
    assert [p.name for p in folder.iterdir()] == ["a-new.pdf"]


def _blocking_run(monkeypatch):
    """A fake run that holds the queue's single worker until released, and keeps the guard it was given."""
    import threading

    started, release, seen = threading.Event(), threading.Event(), {}

    def fake_run(docs, guard=None, **kwargs):
        seen["docs"], seen["guard"] = [d.doc_id for d in docs], guard
        started.set()
        assert release.wait(3)
        if all(guard.cancelled(d.doc_id) for d in docs):
            from pipeline import llm
            raise llm.Cancelled("cancelled")
        guard.dropped = {d.doc_id for d in docs if guard.cancelled(d.doc_id)}  # as run() does before storing

    monkeypatch.setattr("pipeline.runner.run", fake_run)
    return started, release, seen


def test_cancel_a_queued_batch_and_one_document_of_a_running_one(client, monkeypatch):
    started, release, seen = _blocking_run(monkeypatch)
    running = client.post("/api/uploads", files=[("files", ("a.pdf", _pdf_bytes(1), "application/pdf")),
                                                ("files", ("b.pdf", _pdf_bytes(1), "application/pdf"))]).json()
    assert started.wait(3)
    waiting = client.post("/api/uploads", files={"files": ("c.pdf", _pdf_bytes(1), "application/pdf")}).json()
    try:
        cancelled = client.post(f"/api/uploads/{waiting['job_id']}/cancel", json={})
        assert cancelled.json()["status"] == "cancelled"
        assert not (config.UPLOADS_DIR / f"upload-{waiting['job_id']}").exists()
        partial = client.post(f"/api/uploads/{running['job_id']}/cancel", json={"files": ["b.pdf"]})
        assert partial.json()["status"] == "processing" and partial.json()["cancelled"] == ["b.pdf"]
        a, b = seen["docs"]
        assert not seen["guard"].cancelled(a) and seen["guard"].cancelled(b)  # no more model calls for b
        assert client.post(f"/api/uploads/{running['job_id']}/cancel", json={"files": ["x.pdf"]}).status_code == 422
    finally:
        release.set()
    result = _wait(client, running["job_id"])
    assert result["status"] == "done" and result["cancelled"] == ["b.pdf"]
    assert client.post(f"/api/uploads/{running['job_id']}/cancel", json={}).status_code == 409


def test_cancelling_a_whole_running_batch_stores_nothing(client, monkeypatch):
    started, release, _ = _blocking_run(monkeypatch)
    job = client.post("/api/uploads", files={"files": ("a.pdf", _pdf_bytes(1), "application/pdf")}).json()
    assert started.wait(3)
    client.post(f"/api/uploads/{job['job_id']}/cancel", json={})
    release.set()
    assert _wait(client, job["job_id"])["status"] == "cancelled"
    assert not (config.UPLOADS_DIR / f"upload-{job['job_id']}").exists()


def test_daily_call_limit_refuses_an_upload_and_usage_counts_the_calls(client, monkeypatch):
    monkeypatch.setattr("pipeline.runner.run", lambda *a, **k: None)
    monkeypatch.setattr(config, "MAX_DAILY_CALLS", 2)
    con = store.connect()
    store.record_llm_call(con, {"at": datetime.now(timezone.utc).isoformat(), "doc_id": "d", "page": 1,
                                "stage": "classify", "model": "m", "endpoint": "api.example", "image_bytes": 10,
                                "text_chars": 5, "outcome": "ok"})
    con.close()
    res = client.post("/api/uploads", files={"files": ("log.pdf", _pdf_bytes(2), "application/pdf")})
    assert res.status_code == 429 and "daily limit of 2 model calls" in res.json()["detail"]
    assert not any(config.UPLOADS_DIR.iterdir())
    usage = client.get("/api/llm/usage").json()
    assert usage["today"] == 1 and usage["daily_limit"] == 2


def test_an_upload_job_stored_in_another_shape_does_not_break_uploads(client, monkeypatch):
    monkeypatch.setattr("pipeline.runner.run", lambda *a, **k: None)
    con = store.connect()
    with con:
        con.execute("INSERT INTO upload_jobs (id, task, status, created_at) VALUES ('old', ?, 'done', '2026-01-01')",
                    (json.dumps({"job_id": "old", "doc_id": "upload-old", "filename": "a.pdf", "kind": "pdf"}),))
    con.close()
    res = client.post("/api/uploads", files={"files": ("log.pdf", _pdf_bytes(1), "application/pdf")})
    assert res.status_code == 200
    assert client.post("/api/uploads/old/cancel", json={"files": ["a.pdf"]}).status_code == 422
