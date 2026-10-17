"""HTTP API for the map UI. Run: .venv/bin/uvicorn api.main:app --port 8000"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from pipeline import config, store
from pipeline.uploads import UploadQueue

from .routes import boreholes, export, review, stats, uploads

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.migrate()
    queue = UploadQueue(config.DB_PATH)
    app.state.upload_queue = queue
    try:
        yield
    finally:
        await run_in_threadpool(queue.close)


app = FastAPI(title="Borehole Atlas API", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2000)  # map points and lists compress about 6:1

# Review first: the catch-all GET /api/boreholes/{id:path} must not shadow its POST routes below that path, or a
# wrong method on them would be answered with the detail route's Allow header.
for module in (review, boreholes, uploads, stats, export):
    app.include_router(module.router)

# Last: the SPA catches every path the API does not.
if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
