"""Upload outcomes persist; concurrency and admitted work are bounded without LLM calls."""

import threading
import time

import pytest

from pipeline import config, store, uploads


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "uploads.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "MAX_UPLOAD_WORKERS", 1)
    monkeypatch.setattr(config, "MAX_UPLOAD_JOBS", 2)
    store.migrate(path)
    return path


def task(name):
    return uploads.UploadTask(job_id=name, folder=f"upload-{name}",
                              docs=[uploads.UploadDoc(doc_id=f"upload-{name}__invented", path="invented.pdf", kind="pdf",
                                                  sha256=name)])


def wait_result(path, name):
    con = store.connect(path)
    try:
        for _ in range(100):
            result = uploads.result(con, name)
            if result and result.status not in ("queued", "processing"):
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

    monkeypatch.setattr("pipeline.runner.run", run)
    queue = uploads.UploadQueue(db_path)
    try:
        queue.submit(task("one"))
        assert started.wait(2)
        queue.submit(task("two"))
        con = store.connect(db_path)
        try:
            assert uploads.result(con, "two").model_dump(include={"status", "ahead"}) == {"status": "queued", "ahead": 1}
        finally:
            con.close()
        with pytest.raises(uploads.QueueFull):
            queue.submit(task("three"))
        assert len(calls) == 1
    finally:
        release.set()
        queue.close()
    assert len(calls) == 2
    assert wait_result(db_path, "one").status == wait_result(db_path, "two").status == "done"


def test_results_and_interrupted_state_survive_restart(db_path, monkeypatch):
    monkeypatch.setattr("pipeline.runner.run", lambda *args, **kwargs: None)
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

    monkeypatch.setattr("pipeline.runner.run", fail)
    queue = uploads.UploadQueue(db_path)
    queue.submit(task("failed"))
    queue.close()
    result = wait_result(db_path, "failed")
    assert result.status == "error" and "synthetic extraction failed" in result.error


def test_failure_outside_the_run_still_ends_the_job_as_error(db_path, monkeypatch):
    def fail(con):
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr("pipeline.runner.run", lambda *args, **kwargs: None)
    monkeypatch.setattr(store, "llm_calls_today", fail)
    queue = uploads.UploadQueue(db_path)
    queue.submit(task("broken-count"))
    queue.close()
    result = wait_result(db_path, "broken-count")
    assert result.status == "error" and "synthetic database failure" in result.error
    assert not queue.cancelled and not queue.progress


def test_unreadable_queued_job_is_marked_as_error(db_path):
    con = store.connect(db_path)
    with con:
        con.execute("INSERT INTO upload_jobs (id, task, status, created_at) VALUES ('broken', '{\"job_id\": 1}', "
                    "'queued', '2026-01-01')")
    con.close()
    uploads.UploadQueue(db_path).close()
    con = store.connect(db_path)
    try:
        row = con.execute("SELECT status, error FROM upload_jobs WHERE id = 'broken'").fetchone()
    finally:
        con.close()
    assert row["status"] == "error" and "could not be resumed" in row["error"]


def test_job_whose_documents_were_all_cancelled_during_the_run_is_cancelled(db_path, monkeypatch):
    def run(docs, **kwargs):
        kwargs["guard"].dropped = {d.doc_id for d in docs}

    monkeypatch.setattr("pipeline.runner.run", run)
    queue = uploads.UploadQueue(db_path)
    queue.submit(task("dropped"))
    queue.close()
    result = wait_result(db_path, "dropped")
    assert result.status == "cancelled" and result.error is None
