"""Bounded upload processing with persisted outcomes and queued tasks."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import config, ingest, store


class UploadDoc(BaseModel):
    doc_id: str
    path: str  # relative to the batch folder
    kind: Literal["pdf", "docx", "image", "text", "sheet"]


class UploadTask(BaseModel):
    """One upload batch: its documents form one document group and are assembled in one run."""
    job_id: str
    folder: str  # under UPLOADS_DIR
    docs: list[UploadDoc]


class UploadedBorehole(BaseModel):
    id: str
    name: str
    review_status: str


class UploadProgress(BaseModel):
    stage: Literal["classify", "extract", "lab results", "coordinates", "store"]
    done: int
    total: int


class UploadResult(BaseModel):
    status: Literal["queued", "processing", "done", "error"]
    boreholes: list[UploadedBorehole] = Field(default_factory=list)
    error: str | None = None
    ahead: int | None = None  # jobs to finish before this one starts; only while queued
    progress: UploadProgress | None = None  # only while processing


class QueueFull(Exception):
    pass


class UploadQueue:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.executor = ThreadPoolExecutor(max_workers=config.MAX_UPLOAD_WORKERS, thread_name_prefix="upload")
        # Stage of each running job; in memory only, as a restart ends the job anyway.
        self.progress: dict[str, UploadProgress] = {}
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
                    con.execute("UPDATE upload_jobs SET status = 'error', error = ? WHERE id = ?",
                                ("The queued upload could not be resumed; submit the files again", row["id"]))
        finally:
            con.close()
        for task in tasks:
            self.executor.submit(self._work, task)

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
        self.executor.submit(self._work, task)

    def _work(self, task: UploadTask) -> None:
        from .__main__ import run

        con = store.connect(self.db_path)
        try:
            with con:
                con.execute("UPDATE upload_jobs SET status = 'processing' WHERE id = ?", (task.job_id,))
            try:
                run([ingest.SourceDoc(d.doc_id, d.path, d.kind) for d in task.docs],
                    dataset_dir=config.UPLOADS_DIR / task.folder,
                    progress=lambda stage, done, total: self.progress.__setitem__(
                        task.job_id, UploadProgress(stage=stage, done=done, total=total)))
                doc_ids = [d.doc_id for d in task.docs]
                rows = con.execute(f"SELECT id, name, review_status FROM boreholes WHERE doc_id IN "
                                   f"({', '.join('?' * len(doc_ids))}) ORDER BY name, id", doc_ids)
                result = UploadResult(status="done", boreholes=[UploadedBorehole.model_validate(dict(r)) for r in rows])
            except Exception as exc:
                result = UploadResult(status="error", error=f"{type(exc).__name__}: {exc}")
            with con:
                con.execute("UPDATE upload_jobs SET status = ?, result = ?, error = ? WHERE id = ?",
                            (result.status, result.model_dump_json(), result.error, task.job_id))
            self.progress.pop(task.job_id, None)
        finally:
            con.close()

    def close(self) -> None:
        self.executor.shutdown(wait=True)


def result(con: sqlite3.Connection, job_id: str) -> UploadResult | None:
    row = con.execute("SELECT status, result, error, created_at FROM upload_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    if row["result"]:
        return UploadResult.model_validate_json(row["result"])
    if row["status"] == "queued":
        ahead = con.execute("SELECT COUNT(*) FROM upload_jobs WHERE status IN ('queued', 'processing') "
                            "AND (created_at, id) < (?, ?)", (row["created_at"], job_id)).fetchone()[0]
        return UploadResult(status="queued", ahead=ahead)
    return UploadResult(status="error" if row["status"] == "error" else "processing", error=row["error"])
