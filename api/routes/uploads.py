"""Uploading documents for extraction, following and cancelling the batch, and the model call budget."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path
from typing import IO, Annotated

import pymupdf
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from pipeline import config, ingest, llm, runner, store, uploads

from .. import models
from ..db import DB
from ..requests import CancelBody

router = APIRouter()

MULTIPART_OVERHEAD = 1 << 20  # part headers and boundaries beside the file bytes, generous for 1000 parts


def _upload_queue(request: Request) -> uploads.UploadQueue:
    return request.app.state.upload_queue


Queue = Annotated[uploads.UploadQueue, Depends(_upload_queue)]


@router.post("/api/uploads")
async def upload_documents(queue: Queue, files: list[UploadFile] = File(...)) -> models.UploadJob:
    """One batch of files, or zips of them, becomes one document group assembled in a single run, so
    records of one hole spread over several files are combined. Other files in the batch
    (e.g. GIS files) are skipped and reported, not a reason to reject the documents beside them. So is a file
    sent before, in this batch or an earlier upload: a second copy would add a second group of the same holes."""
    batch, skipped, duplicates = [], [], []
    for file in files:
        name = _safe_part((file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]) or "upload"
        if Path(name).suffix.lower() in ingest.SUPPORTED | {".zip"}:
            batch.append((file, name))
        else:
            skipped.append(name)
    if not batch:
        raise HTTPException(415, "no PDF, JPG, PNG, TIFF, DOCX, TXT, XLSX or ZIP file in the upload")
    if msg := runner.missing_credentials():
        raise HTTPException(503, msg)
    job_id = uuid.uuid4().hex[:12]
    folder = f"upload-{job_id}"
    upload_dir = config.UPLOADS_DIR / folder
    upload_dir.mkdir(parents=True, exist_ok=True)
    docs: list[ingest.SourceDoc] = []
    try:
        budget = _Budget()
        for file, name in batch:
            if name.lower().endswith(".zip"):
                with tempfile.TemporaryFile() as archive:
                    while chunk := await file.read(1 << 20):
                        budget.received(len(chunk))
                        archive.write(chunk)
                    skipped += await run_in_threadpool(_extract_zip, archive, name, upload_dir, budget)
            else:
                budget.documents(1)
                with _unique_path(upload_dir, name).open("wb") as out:
                    while chunk := await file.read(1 << 20):
                        budget.received(len(chunk))
                        budget.stored(len(chunk))
                        out.write(chunk)
        found = ingest.discover(upload_dir)
        if not found:
            raise HTTPException(415, "the upload contains no PDF, JPG, PNG, TIFF, DOCX, TXT or XLSX file")
        digests = await run_in_threadpool(_new_files, found, upload_dir, duplicates)
        if not digests:
            first = duplicates[0]
            raise HTTPException(409, f"already uploaded: {first['file']}" + (
                f" (as {first['of']} on {first['at'][:16].replace('T', ' ')} UTC)" if first["at"] else ""))
        docs = [ingest.SourceDoc(f"{folder}__{d.doc_id}", d.path, d.kind) for d in found if d.path in digests]
        pages = await run_in_threadpool(_render_upload, docs, upload_dir)
        await run_in_threadpool(_check_daily_calls, pages)
        await run_in_threadpool(queue.submit, uploads.UploadTask(
            job_id=job_id, folder=folder,
            docs=[uploads.UploadDoc(doc_id=d.doc_id, path=d.path, kind=d.kind, sha256=digests[d.path]) for d in docs]))
    except Exception as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        shutil.rmtree(ingest.converted_dir(upload_dir), ignore_errors=True)
        for d in docs:
            shutil.rmtree(config.PAGES_DIR / d.doc_id, ignore_errors=True)
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, uploads.QueueFull):
            raise HTTPException(503, "the upload queue is full; try again after a document finishes processing") from exc
        raise
    return models.UploadJob(job_id=job_id, pages=pages, files=[Path(d.path).as_posix() for d in docs], skipped=skipped,
                            duplicates=duplicates)


@router.get("/api/uploads/{job_id}")
def upload_status(job_id: str, con: DB, queue: Queue) -> uploads.UploadResult:
    return _job_result(con, queue, job_id)


@router.post("/api/uploads/{job_id}/cancel")
def cancel_upload(job_id: str, body: CancelBody, con: DB, queue: Queue) -> uploads.UploadResult:
    """Stops a queued or running batch, or takes some of its documents out. Calls already running finish; no new
    ones start for them, and nothing is stored for them."""
    row = con.execute("SELECT task FROM upload_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "upload job not found")
    doc_ids = None
    if body.files is not None:
        by_path = {Path(d["path"]).as_posix(): d["doc_id"] for d in json.loads(row["task"]).get("docs", [])}
        if unknown := [f for f in body.files if f not in by_path]:
            raise HTTPException(422, f"not in this upload: {', '.join(unknown)}")
        doc_ids = [by_path[f] for f in body.files]
    try:
        queue.cancel(job_id, doc_ids)
    except uploads.NotCancellable as exc:
        raise HTTPException(409, str(exc)) from None
    return _job_result(con, queue, job_id)


@router.get("/api/llm/usage")
def llm_usage(con: DB):
    """Model calls today against the limits, and where they go."""
    return {"today": store.llm_calls_today(con), "daily_limit": config.MAX_DAILY_CALLS,
            "upload_limit": config.MAX_UPLOAD_CALLS, "endpoint": llm.endpoint()}


def _job_result(con: sqlite3.Connection, queue: uploads.UploadQueue, job_id: str) -> uploads.UploadResult:
    job = uploads.result(con, job_id)
    if job is None:
        raise HTTPException(404, "upload job not found")
    if job.status == "processing":
        job.progress = queue.progress.get(job_id)
    return job


def _new_files(found: list[ingest.SourceDoc], upload_dir: Path, duplicates: list[dict]) -> dict[str, str]:
    """path -> sha256 of the documents not sent before. A copy is removed and reported in `duplicates`: the file,
    the earlier one it repeats and that one's upload time (None within this batch)."""
    con = store.connect()
    try:
        known = uploads.uploaded_files(con)
    finally:
        con.close()
    digests: dict[str, str] = {}
    for d in found:
        src = upload_dir / d.path
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        earlier = next((p for p, x in digests.items() if x == digest), None)
        if earlier or digest in known:
            of, at = (earlier, None) if earlier else known[digest]
            duplicates.append({"file": Path(d.path).as_posix(), "of": Path(of).as_posix(), "at": at})
            src.unlink()
        else:
            digests[d.path] = digest
    return digests


def _check_daily_calls(pages: int) -> None:
    """Every page takes at least one model call."""
    con = store.connect()
    try:
        today = store.llm_calls_today(con)
    finally:
        con.close()
    if today + pages > config.MAX_DAILY_CALLS:
        raise HTTPException(429, f"the daily limit of {config.MAX_DAILY_CALLS} model calls would be passed "
                                 f"({today} made today, this upload needs at least {pages}); try again tomorrow")


class _Budget:
    """The limits apply to the whole batch. MAX_UPLOAD_BYTES to the bytes received and to the documents stored,
    so a small zip cannot expand past it; MAX_UPLOAD_FILES to the documents, counted before a zip is extracted, so
    a small zip of many empty entries is refused before they are written out."""

    def __init__(self) -> None:
        self.received_bytes = self.stored_bytes = self.document_count = 0

    def documents(self, n: int) -> None:
        self.document_count += n
        if self.document_count > config.MAX_UPLOAD_FILES:
            raise HTTPException(413, f"the upload holds more than the {config.MAX_UPLOAD_FILES} documents allowed")

    def received(self, n: int) -> None:
        self.received_bytes += n
        self._check(self.received_bytes)

    def stored(self, n: int) -> None:
        self.stored_bytes += n
        self._check(self.stored_bytes)

    @staticmethod
    def _check(total: int) -> None:
        if total > config.MAX_UPLOAD_BYTES:
            raise _over_byte_limit()


def _over_byte_limit() -> HTTPException:
    return HTTPException(413, f"the upload exceeds the {config.MAX_UPLOAD_BYTES // (1 << 20)} MB upload limit")


class UploadSizeLimit:
    """Refuses an upload whose declared length is past the byte limit before its body is read: FastAPI spools the
    whole multipart body to disk before the route runs. A chunked body declares no length; the batch budget
    catches it after parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/api/uploads":
            length = Headers(scope=scope).get("content-length", "")
            if length.isdigit() and int(length) > config.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD:
                exc = _over_byte_limit()
                await JSONResponse({"detail": exc.detail}, exc.status_code)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _safe_part(part: str) -> str:
    """One path component that is a valid file name on every OS."""
    return re.sub(r'[<>:"|?*\x00-\x1f]', "-", part).strip(" .")


def _unique_path(folder: Path, rel: str) -> Path:
    dest = folder / rel
    stem, suffix, n = dest.stem, dest.suffix, 1
    while dest.exists():  # two files of one name in a batch stay two documents
        n += 1
        dest = dest.with_name(f"{stem}-{n}{suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _extract_zip(archive: IO[bytes], name: str, upload_dir: Path, budget: _Budget) -> list[str]:
    """Extracts the documents; returns the other files it skipped."""
    skipped = []
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise HTTPException(422, f"could not read {name}: is it a valid ZIP file?") from None
    with zf:
        documents = []
        for info in zf.infolist():
            parts = info.filename.replace("\\", "/").split("/")
            if parts[0] == "" or ".." in parts or ":" in parts[0]:  # absolute, traversing or drive-qualified
                raise HTTPException(400, f"{name} contains an unsafe path: {info.filename}")
            parts = [p for p in parts if p not in ("", ".")]
            # Entries starting with '.' or '__MACOSX' are archive metadata, not documents.
            if info.is_dir() or not parts or any(p.startswith((".", "__MACOSX")) for p in parts):
                continue
            if Path(parts[-1]).suffix.lower() not in ingest.SUPPORTED:
                skipped.append("/".join(parts))
                continue
            documents.append((info, parts))
        budget.documents(len(documents))
        for info, parts in documents:
            dest = _unique_path(upload_dir, "/".join(_safe_part(p) or "_" for p in parts))
            if upload_dir.resolve() not in dest.resolve().parents:
                raise HTTPException(400, f"{name} contains an unsafe path: {info.filename}")
            try:
                with zf.open(info) as src, dest.open("wb") as out:
                    while chunk := src.read(1 << 20):  # counted as read: the sizes in the archive header can lie
                        budget.stored(len(chunk))
                        out.write(chunk)
            except (RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:  # encrypted, corrupt, unknown method
                raise HTTPException(422, f"could not extract {info.filename} from {name}: {exc}") from None
    return skipped


_MUPDF = threading.Lock()  # PyMuPDF is not thread-safe; concurrent uploads must not render at the same time


def _render_upload(docs: list[ingest.SourceDoc], upload_dir: Path) -> int:
    _check_page_count(len(docs))  # every document has at least one page
    with _MUPDF:
        declared = 0
        for doc in docs:
            if doc.kind in ("docx", "sheet"):  # laid out only when rendered; checked after that
                continue
            src = upload_dir / doc.path
            try:
                # Opened from memory first: on Windows a file PyMuPDF fails to open stays locked while the error
                # is alive, and the rejected upload could not be removed.
                with pymupdf.open(stream=src.read_bytes(), filetype=src.suffix.lstrip(".").lower()) as source:
                    declared += source.page_count if doc.kind in ("pdf", "text") else 1
            except Exception as exc:
                raise HTTPException(422, f"could not read {doc.path}: is it a valid "
                                         f"{src.suffix.lstrip('.').upper()} file?") from exc
        _check_page_count(declared)  # a long PDF would take minutes to render only to be rejected
        pages = 0
        for doc in docs:
            try:
                pages += len(ingest.render(doc, upload_dir))
            except Exception as exc:
                raise HTTPException(422, f"could not read {doc.path}") from exc
    _check_page_count(pages)
    return pages


def _check_page_count(pages: int) -> None:
    if pages > config.MAX_UPLOAD_PAGES:
        raise HTTPException(413, f"{pages} pages exceeds the {config.MAX_UPLOAD_PAGES}-page upload limit")
