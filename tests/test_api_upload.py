"""Upload endpoint: batches, zip extraction, and the type, size and page checks before any LLM call."""

import io
import time
import zipfile

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
    monkeypatch.setattr("pipeline.__main__._missing_credentials", lambda: None)
    store.connect(path).close()
    with TestClient(app) as client:
        yield client


def _pdf_bytes(n_pages: int) -> bytes:
    pdf = pymupdf.open()
    for i in range(n_pages):
        pdf.new_page().insert_text((72, 72), f"page {i + 1}")
    return pdf.tobytes()


def test_rejects_unsupported_file_type(client):
    res = client.post("/api/upload", files={"files": ("notes.csv", b"hello", "text/csv")})
    assert res.status_code == 415


def test_rejects_file_over_the_page_cap(client):
    res = client.post("/api/upload", files={"files": ("log.pdf", _pdf_bytes(3), "application/pdf")})
    assert res.status_code == 413
    assert "3 pages" in res.json()["detail"]
    assert not any(config.UPLOADS_DIR.iterdir())


def test_rejects_unreadable_file_and_leaves_nothing_behind(client):
    res = client.post("/api/upload", files={"files": ("log.pdf", b"not a pdf", "application/pdf")})
    assert res.status_code == 422
    assert not any(config.UPLOADS_DIR.iterdir())
    assert not config.PAGES_DIR.exists() or not any(config.PAGES_DIR.iterdir())


def test_rejects_upload_without_credentials_before_saving(client, monkeypatch):
    monkeypatch.setattr("pipeline.__main__._missing_credentials", lambda: "No key")
    res = client.post("/api/upload", files={"files": ("log.pdf", _pdf_bytes(1), "application/pdf")})
    assert res.status_code == 503
    assert not config.UPLOADS_DIR.exists()


def test_rejects_file_over_the_byte_cap(client):
    res = client.post("/api/upload", files={"files": ("log.pdf", _pdf_bytes(1) + b"\0" * 20_000, "application/pdf")})
    assert res.status_code == 413
    assert "MB upload limit" in res.json()["detail"]


def test_strips_directory_components_from_the_filename(client, monkeypatch):
    monkeypatch.setattr("pipeline.__main__.run", lambda *a, **k: None)
    res = client.post("/api/upload", files={"files": ("../../evil.pdf", _pdf_bytes(1), "application/pdf")})
    assert res.status_code == 200
    saved = list((config.UPLOADS_DIR / f"upload-{res.json()['job_id']}").iterdir())
    assert [p.name for p in saved] == ["evil.pdf"]  # no path components escaped the upload's own folder


def test_accepts_file_within_the_page_cap(client, monkeypatch):
    calls = []

    def fake_run(docs, max_pages=None, yes=True, dataset_dir=config.DATASET_DIR, progress=None):
        calls.append(docs)

    monkeypatch.setattr("pipeline.__main__.run", fake_run)
    res = client.post("/api/upload", files={"files": ("log.pdf", _pdf_bytes(2), "application/pdf")})
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    assert res.json()["pages"] == 2

    status = {}
    for _ in range(50):
        status = client.get(f"/api/upload/{job_id}").json()
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
    response = client.post("/api/upload", files={"files": ("invented.pdf", _pdf_bytes(1), "application/pdf")})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    for _ in range(100):
        result = client.get(f"/api/upload/{job_id}").json()
        if result["status"] not in ("queued", "processing"):
            break
        time.sleep(0.01)
    assert result["status"] == "done" and len(result["boreholes"]) == 1
    bid = result["boreholes"][0]["id"]
    detail = client.get(f"/api/boreholes/{bid}/detail").json()
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
        result = client.get(f"/api/upload/{job_id}").json()
        if result["status"] not in ("queued", "processing"):
            return result
        time.sleep(0.01)
    pytest.fail("upload did not finish")


def test_files_of_one_batch_form_one_document_group_in_one_run(client, monkeypatch):
    from pipeline.extraction_models import LogRef, PageClassification, PageExtraction
    from tests.test_assemble import _log

    runs = []
    real_run = __import__("pipeline.__main__", fromlist=["run"]).run

    def recording_run(docs, **kwargs):
        runs.append([d.doc_id for d in docs])
        real_run(docs, **kwargs)

    classification = PageClassification(kind="borehole_log", logs=[LogRef(name="BH1", sheet=None)],
                                        language="en", has_coordinates=True, crs_text=None)
    log = _log("BH1", "E 412000 N 5320000", 300, [(0, 9)]).model_copy(update={"final_depth_m": 9})
    monkeypatch.setattr("pipeline.__main__.run", recording_run)
    monkeypatch.setattr("pipeline.llm.classify_page", lambda *args: classification)
    monkeypatch.setattr("pipeline.llm.extract_page", lambda *args: PageExtraction(logs=[log], coordinate_rows=[]))
    res = client.post("/api/upload", files=[("files", ("log.pdf", _pdf_bytes(1), "application/pdf")),
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
    monkeypatch.setattr("pipeline.__main__.run", lambda *a, **k: None)
    archive = _zip_bytes({"logs/a.pdf": _pdf_bytes(1), "notes.csv": b"x", "__MACOSX/logs/._a.pdf": b"meta"})
    res = client.post("/api/upload", files={"files": ("batch.zip", archive, "application/zip")})
    assert res.status_code == 200
    assert res.json()["files"] == ["logs/a.pdf"]
    assert res.json()["skipped"] == ["notes.csv"]


def test_batch_skips_other_files_beside_its_documents(client, monkeypatch):
    monkeypatch.setattr("pipeline.__main__.run", lambda *a, **k: None)
    res = client.post("/api/upload", files=[("files", ("log.pdf", _pdf_bytes(1), "application/pdf")),
                                            ("files", ("site.shp", b"x", "application/octet-stream")),
                                            ("files", ("site.gpkg", b"x", "application/octet-stream"))])
    assert res.status_code == 200
    assert res.json()["files"] == ["log.pdf"]
    assert res.json()["skipped"] == ["site.shp", "site.gpkg"]
    assert [p.name for p in (config.UPLOADS_DIR / f"upload-{res.json()['job_id']}").iterdir()] == ["log.pdf"]


def test_rejects_zip_with_a_traversing_entry_and_leaves_nothing_behind(client):
    archive = _zip_bytes({"a.pdf": _pdf_bytes(1), "../escaped.pdf": _pdf_bytes(1)})
    res = client.post("/api/upload", files={"files": ("batch.zip", archive, "application/zip")})
    assert res.status_code == 400
    assert "unsafe path" in res.json()["detail"]
    assert not any(config.UPLOADS_DIR.iterdir())
    assert not (config.UPLOADS_DIR.parent / "escaped.pdf").exists()


def test_page_cap_applies_to_the_whole_batch(client):
    res = client.post("/api/upload", files=[("files", ("a.pdf", _pdf_bytes(2), "application/pdf")),
                                            ("files", ("b.pdf", _pdf_bytes(1), "application/pdf"))])
    assert res.status_code == 413
    assert "3 pages" in res.json()["detail"]
    assert not any(config.UPLOADS_DIR.iterdir())


def test_byte_cap_applies_to_the_extracted_zip(client):
    archive = _zip_bytes({"a.pdf": _pdf_bytes(1) + b"\0" * 50_000})  # compresses far below the cap
    assert len(archive) < config.MAX_UPLOAD_BYTES
    res = client.post("/api/upload", files={"files": ("batch.zip", archive, "application/zip")})
    assert res.status_code == 413
    assert not any(config.UPLOADS_DIR.iterdir())


def test_status_reports_the_running_stage(client, monkeypatch):
    import threading

    reported, release = threading.Event(), threading.Event()

    def fake_run(docs, progress=None, **kwargs):
        progress("extract", 3, 7)
        reported.set()
        assert release.wait(3)

    monkeypatch.setattr("pipeline.__main__.run", fake_run)
    job_id = client.post("/api/upload", files={"files": ("log.pdf", _pdf_bytes(1), "application/pdf")}).json()["job_id"]
    assert reported.wait(3)
    try:
        status = client.get(f"/api/upload/{job_id}").json()
        assert status["status"] == "processing"
        assert status["progress"] == {"stage": "extract", "done": 3, "total": 7}
    finally:
        release.set()
    assert _wait(client, job_id)["progress"] is None
