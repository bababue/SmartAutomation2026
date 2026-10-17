"""Bounded upload processing with persisted outcomes and queued tasks."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import config, ingest, store


class UploadTask(BaseModel):
    job_id: str
    doc_id: str
    filename: str
    kind: Literal["pdf", "docx", "image"]


class UploadedBorehole(BaseModel):
    id: str
    name: str
    review_status: str


class UploadResult(BaseModel):
    status: Literal["processing", "done", "error"]
    boreholes: list[UploadedBorehole] = Field(default_factory=list)
    error: str | None = None


class QueueFull(Exception):
    pass


class UploadQueue:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.executor = ThreadPoolExecutor(max_workers=config.MAX_UPLOAD_WORKERS, thread_name_prefix="upload")
        con = store.connect(db_path)
        try:
            with con:
                con.execute("UPDATE upload_jobs SET status = 'error', error = ? WHERE status = 'processing'",
                            ("Processing was interrupted by a server restart; submit the file again to retry",))
            tasks = [UploadTask.model_validate_json(r["task"]) for r in con.execute(
                "SELECT task FROM upload_jobs WHERE status = 'queued' ORDER BY created_at")]
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
                run([ingest.SourceDoc(task.doc_id, task.filename, task.kind)],
                    dataset_dir=config.UPLOADS_DIR / task.doc_id)
                rows = con.execute("SELECT id, name, review_status FROM boreholes WHERE doc_id = ?", (task.doc_id,))
                result = UploadResult(status="done", boreholes=[UploadedBorehole.model_validate(dict(r)) for r in rows])
            except Exception as exc:
                result = UploadResult(status="error", error=f"{type(exc).__name__}: {exc}")
            with con:
                con.execute("UPDATE upload_jobs SET status = ?, result = ?, error = ? WHERE id = ?",
                            (result.status, result.model_dump_json(), result.error, task.job_id))
        finally:
            con.close()

    def close(self) -> None:
        self.executor.shutdown(wait=True)


def result(con: sqlite3.Connection, job_id: str) -> UploadResult | None:
    row = con.execute("SELECT status, result, error FROM upload_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    if row["result"]:
        return UploadResult.model_validate_json(row["result"])
    return UploadResult(status="error" if row["status"] == "error" else "processing", error=row["error"])
