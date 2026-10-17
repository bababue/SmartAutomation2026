"""Bounded upload processing with persisted outcomes and queued tasks."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import config, ingest, llm, store


class UploadDoc(BaseModel):
    doc_id: str
    path: str  # relative to the batch folder
    kind: Literal["pdf", "docx", "image", "text", "sheet"]
    sha256: str  # of the file as uploaded, to recognise the same file sent again


class UploadTask(BaseModel):
    """One upload batch: its documents form one document group and are assembled in one run."""
    job_id: str
    folder: str  # under UPLOADS_DIR
    docs: list[UploadDoc]
    cancelled: list[str] = Field(default_factory=list)  # doc_ids a user took out of the batch


class UploadedBorehole(BaseModel):
    id: str
    name: str
    review_status: str


class UploadProgress(BaseModel):
    stage: Literal["classify", "extract", "lab results", "coordinates", "store"]
    done: int
    total: int


class UploadResult(BaseModel):
    status: Literal["queued", "processing", "done", "error", "cancelled"]
    boreholes: list[UploadedBorehole] = Field(default_factory=list)
    error: str | None = None
    ahead: int | None = None  # jobs to finish before this one starts; only while queued
    progress: UploadProgress | None = None  # only while processing
    cancelled: list[str] = Field(default_factory=list)  # paths of the documents taken out of the batch
    calls: int = 0  # model calls made for this batch (cache hits make none)


class QueueFull(Exception):
    pass


class NotCancellable(Exception):
    pass


class UploadQueue:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.executor = ThreadPoolExecutor(max_workers=config.MAX_UPLOAD_WORKERS, thread_name_prefix="upload")
        # Stage and cancelled documents of each running job; in memory only, as a restart ends the job anyway.
        self.progress: dict[str, UploadProgress] = {}
        self.cancelled: dict[str, set[str]] = {}
        self.lock = threading.Lock()  # a job starts or is cancelled, never both at once
        con = store.connect(db_path)
        try:
            with con:
                con.execute("UPDATE upload_jobs SET status = 'error', error = ? WHERE status = 'processing'",
                            ("Processing was interrupted by a server restart; submit the file again to retry",))
            tasks = []
            for row in con.execute("SELECT id, task FROM upload_jobs WHERE status = 'queued' ORDER BY created_at").fetchall():
                try:
                    tasks.append(UploadTask.model_validate_json(row["task"]))
                except ValueError:
                    with con:
                        con.execute("UPDATE upload_jobs SET status = 'error', error = ? WHERE id = ?",
                                    ("The queued upload could not be resumed; submit the files again", row["id"]))
        finally:
            con.close()
        for task in tasks:
            self.executor.submit(self._work, task.job_id)

    def submit(self, task: UploadTask) -> None:
        con = store.connect(self.db_path)
        try:
            with con:
                con.execute("BEGIN IMMEDIATE")
                count = con.execute("SELECT COUNT(*) FROM upload_jobs WHERE status IN ('queued', 'processing')").fetchone()[0]
                if count >= config.MAX_UPLOAD_JOBS:
                    raise QueueFull()
                con.execute("INSERT INTO upload_jobs (id, task, status, created_at) VALUES (?, ?, 'queued', ?)",
                            (task.job_id, task.model_dump_json(), datetime.now(timezone.utc).isoformat()))
        finally:
            con.close()
        self.executor.submit(self._work, task.job_id)

    def cancel(self, job_id: str, doc_ids: list[str] | None = None) -> bool:
        """Takes documents (all when doc_ids is None) out of a queued or running batch. A running batch stops
        calling the model for them at once and stores nothing for them. False: no such job."""
        con = store.connect(self.db_path)
        try:
            with self.lock:
                row = con.execute("SELECT status, task FROM upload_jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    return False
                if row["status"] not in ("queued", "processing"):
                    raise NotCancellable(f"the upload is already {row['status']}")
                task = UploadTask.model_validate_json(row["task"])
                ids = {d.doc_id for d in task.docs}
                task.cancelled = sorted(set(task.cancelled) | (ids if doc_ids is None else ids & set(doc_ids)))
                if row["status"] == "queued" and set(task.cancelled) == ids:
                    result = UploadResult(status="cancelled", cancelled=_paths(task, task.cancelled))
                    with con:
                        con.execute("UPDATE upload_jobs SET status = 'cancelled', task = ?, result = ? WHERE id = ?",
                                    (task.model_dump_json(), result.model_dump_json(), job_id))
                    _remove_files(task, task.cancelled, whole=True)
                    return True
                with con:
                    con.execute("UPDATE upload_jobs SET task = ? WHERE id = ?", (task.model_dump_json(), job_id))
                if row["status"] == "processing":
                    self.cancelled[job_id] = set(task.cancelled)
                return True
        finally:
            con.close()

    def _work(self, job_id: str) -> None:
        from .runner import run

        con = store.connect(self.db_path)
        try:
            with self.lock:
                row = con.execute("SELECT status, task FROM upload_jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None or row["status"] != "queued":
                    return  # cancelled while it waited
                task = UploadTask.model_validate_json(row["task"])
                with con:
                    con.execute("UPDATE upload_jobs SET status = 'processing' WHERE id = ?", (job_id,))
                self.cancelled[job_id] = set(task.cancelled)
            docs = [d for d in task.docs if d.doc_id not in self.cancelled[job_id]]
            guard = llm.CallGuard(cancelled=lambda doc_id: doc_id in self.cancelled.get(job_id, ()),
                                  max_calls=max(0, min(config.MAX_UPLOAD_CALLS,
                                                       config.MAX_DAILY_CALLS - store.llm_calls_today(con))))
            try:
                run([ingest.SourceDoc(d.doc_id, d.path, d.kind) for d in docs],
                    dataset_dir=config.UPLOADS_DIR / task.folder, guard=guard, job_id=job_id,
                    progress=lambda stage, done, total: self.progress.__setitem__(
                        job_id, UploadProgress(stage=stage, done=done, total=total)))
                kept = [d.doc_id for d in docs if d.doc_id not in guard.dropped]
                rows = con.execute(f"SELECT id, name, review_status FROM boreholes WHERE doc_id IN "
                                   f"({', '.join('?' * len(kept))}) ORDER BY name, id", kept)
                result = UploadResult(status="done", boreholes=[UploadedBorehole.model_validate(dict(r)) for r in rows])
            except llm.Cancelled:
                result = UploadResult(status="cancelled")
            except Exception as exc:
                result = UploadResult(status="error", error=f"{type(exc).__name__}: {exc}")
            with self.lock:
                taken_out = self.cancelled.pop(job_id, set())
                # Taken out before the run stored anything; a later cancellation came too late.
                cancelled = sorted(taken_out if result.status != "done" else
                                   {d.doc_id for d in task.docs if d not in docs} | guard.dropped)
                task.cancelled = cancelled
                result.cancelled = _paths(task, cancelled)
                result.calls = _calls(con, job_id)
                _remove_files(task, cancelled, whole=result.status == "cancelled")  # gone before the status says so
                with con:
                    con.execute("UPDATE upload_jobs SET status = ?, task = ?, result = ?, error = ? WHERE id = ?",
                                (result.status, task.model_dump_json(), result.model_dump_json(), result.error, job_id))
            self.progress.pop(job_id, None)
        finally:
            con.close()

    def close(self) -> None:
        self.executor.shutdown(wait=True)


def _paths(task: UploadTask, doc_ids: list[str]) -> list[str]:
    return [d.path for d in task.docs if d.doc_id in doc_ids]


def _calls(con: sqlite3.Connection, job_id: str) -> int:
    return con.execute("SELECT COUNT(*) FROM llm_calls WHERE job_id = ?", (job_id,)).fetchone()[0]


def _remove_files(task: UploadTask, doc_ids: list[str], whole: bool) -> None:
    for doc_id in doc_ids:
        shutil.rmtree(config.PAGES_DIR / doc_id, ignore_errors=True)
    if whole:
        shutil.rmtree(config.UPLOADS_DIR / task.folder, ignore_errors=True)
        shutil.rmtree(ingest.converted_dir(config.UPLOADS_DIR / task.folder), ignore_errors=True)


def result(con: sqlite3.Connection, job_id: str) -> UploadResult | None:
    row = con.execute("SELECT status, task, result, error, created_at FROM upload_jobs WHERE id = ?",
                      (job_id,)).fetchone()
    if row is None:
        return None
    if row["result"]:
        return UploadResult.model_validate_json(row["result"])
    task = json.loads(row["task"])
    cancelled = [d["path"] for d in task["docs"] if d["doc_id"] in task.get("cancelled", [])]
    if row["status"] == "queued":
        ahead = con.execute("SELECT COUNT(*) FROM upload_jobs WHERE status IN ('queued', 'processing') "
                            "AND (created_at, id) < (?, ?)", (row["created_at"], job_id)).fetchone()[0]
        return UploadResult(status="queued", ahead=ahead, cancelled=cancelled)
    return UploadResult(status="error" if row["status"] == "error" else "processing", error=row["error"],
                        cancelled=cancelled, calls=_calls(con, job_id))


def uploaded_files(con: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """sha256 -> (path, upload time) of every document in a queued, running or finished upload that was not
    taken out of it."""
    out: dict[str, tuple[str, str]] = {}
    for row in con.execute("SELECT task, created_at FROM upload_jobs WHERE status IN ('queued', 'processing', 'done') "
                           "ORDER BY created_at"):
        task = json.loads(row["task"])
        for d in task["docs"]:
            if d.get("sha256") and d["doc_id"] not in task.get("cancelled", []):
                out.setdefault(d["sha256"], (d["path"], row["created_at"]))
    return out
