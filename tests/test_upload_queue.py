"""Upload outcomes persist; concurrency and admitted work are bounded without LLM calls."""

import threading
import time

import pytest

from pipeline import config, store, uploads


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "uploads.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store.connect, "__defaults__", (path,))
    monkeypatch.setattr(config, "MAX_UPLOAD_WORKERS", 1)
    monkeypatch.setattr(config, "MAX_UPLOAD_JOBS", 2)
    return path


def task(name):
    return uploads.UploadTask(job_id=name, doc_id=f"upload-{name}", filename="invented.pdf", kind="pdf")


def wait_result(path, name):
    con = store.connect(path)
    try:
        for _ in range(100):
            result = uploads.result(con, name)
            if result and result.status != "processing":
                return result
            time.sleep(0.01)
    finally:
        con.close()
    pytest.fail("upload did not finish")


def test_queue_limits_workers_and_admitted_jobs(db_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        started.set()
        assert release.wait(3)

    monkeypatch.setattr("pipeline.__main__.run", run)
    queue = uploads.UploadQueue(db_path)
    try:
        queue.submit(task("one"))
        assert started.wait(2)
        queue.submit(task("two"))
        with pytest.raises(uploads.QueueFull):
            queue.submit(task("three"))
        assert len(calls) == 1
    finally:
        release.set()
        queue.close()
    assert len(calls) == 2
    assert wait_result(db_path, "one").status == wait_result(db_path, "two").status == "done"


def test_results_and_interrupted_state_survive_restart(db_path, monkeypatch):
    monkeypatch.setattr("pipeline.__main__.run", lambda *args, **kwargs: None)
    con = store.connect(db_path)
    with con:
        for name, state in (("interrupted", "processing"), ("waiting", "queued")):
            con.execute("INSERT INTO upload_jobs (id, task, status, created_at) VALUES (?, ?, ?, '2026-01-01')",
                        (name, task(name).model_dump_json(), state))
    con.close()
    queue = uploads.UploadQueue(db_path)
    queue.close()
    assert "interrupted" in wait_result(db_path, "interrupted").error
    assert wait_result(db_path, "waiting").status == "done"
    queue = uploads.UploadQueue(db_path)
    queue.close()
    assert wait_result(db_path, "waiting").status == "done"


def test_processing_failure_is_persisted(db_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic extraction failed")

    monkeypatch.setattr("pipeline.__main__.run", fail)
    queue = uploads.UploadQueue(db_path)
    queue.submit(task("failed"))
    queue.close()
    result = wait_result(db_path, "failed")
    assert result.status == "error" and "synthetic extraction failed" in result.error
