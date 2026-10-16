"""HTTP API for the map UI. Run: .venv/bin/uvicorn api.main:app --port 8000"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline import config, store
from schema.borehole import DIN4023_LABEL_DE, Borehole, Lithology, ReviewStatus

app = FastAPI(title="Borehole Atlas API")


def db() -> Iterator[sqlite3.Connection]:
    """One connection per request: no shared transactions between concurrent requests."""
    con = store.connect()
    try:
        yield con
    finally:
        con.close()


DB = Annotated[sqlite3.Connection, Depends(db)]

LIGHT_COLUMNS = (
    "id, name, project, location_name, country, lat, lon, depth, elevation, investigation_type, "
    "confidence, loc_confidence, loc_method, score, review_status, n_layers, n_issues, lithologies, strip"
)

STATUS_GROUPS = {
    "released": ("auto_approved", "approved"),
    "pending": ("pending_review",),
    "rejected": ("rejected",),
}


def _fts_query(q: str) -> str:
    tokens = re.findall(r"\w+", q, flags=re.UNICODE)
    return " AND ".join(f'"{t}"*' for t in tokens)


def _feature(r: sqlite3.Row) -> dict:
    props = {k: r[k] for k in r.keys() if k not in ("lat", "lon")}
    props["strip"] = json.loads(r["strip"]) if r["strip"] else None
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]}, "properties": props}


@app.get("/api/boreholes")
def list_boreholes(
    con: DB,
    q: str | None = None,
    bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat"),
    lithology: list[str] = Query(default_factory=list),
    country: str | None = None,
    min_depth: float | None = None,
    min_position: float | None = Query(None, description="minimum position (georeference) confidence"),
    status: Literal["all", "released", "pending", "rejected"] = "all",
):
    where, params = ["lat IS NOT NULL"], []
    if q and (fq := _fts_query(q)):
        where.append("id IN (SELECT id FROM boreholes_fts WHERE boreholes_fts MATCH ?)")
        params.append(fq)
    if bbox:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
        where.append("rid IN (SELECT rid FROM boreholes_rtree WHERE min_lat >= ? AND max_lat <= ? AND min_lon >= ? AND max_lon <= ?)")
        params += [min_lat, max_lat, min_lon, max_lon]
    for lith in lithology:
        where.append("(',' || lithologies || ',') LIKE ?")
        params.append(f"%,{lith},%")
    if country:
        where.append("country = ?")
        params.append(country.upper())
    if min_depth is not None:
        where.append("depth >= ?")
        params.append(min_depth)
    if min_position is not None:
        where.append("loc_confidence >= ?")
        params.append(min_position)
    if status != "all":
        values = STATUS_GROUPS[status]
        where.append(f"review_status IN ({','.join('?' * len(values))})")
        params += list(values)
    rows = con.execute(f"SELECT {LIGHT_COLUMNS} FROM boreholes WHERE {' AND '.join(where)}", params).fetchall()
    return {"type": "FeatureCollection", "features": [_feature(r) for r in rows]}


@app.get("/api/review-queue")
def review_queue(con: DB):
    """Everything waiting for a human, weakest first (also records without a position)."""
    rows = con.execute(
        "SELECT id, name, project, location_name, country, score, confidence, loc_confidence, loc_method, "
        "n_layers, data FROM boreholes WHERE review_status = 'pending_review' ORDER BY score ASC, name"
    ).fetchall()
    return [
        {**{k: r[k] for k in r.keys() if k != "data"}, "reasons": json.loads(r["data"]).get("score_reasons", [])}
        for r in rows
    ]


@app.get("/api/boreholes/{borehole_id:path}/detail")
def get_borehole(borehole_id: str, con: DB):
    bh = store.load_borehole(con, borehole_id)
    if bh is None:
        raise HTTPException(404, "borehole not found")
    pages = {(s.file, s.page) for s in bh.sources}
    docs = {r["path"]: r["doc_id"] for r in con.execute("SELECT doc_id, path FROM documents")}
    stored = {(r["doc_id"], r["page"]) for r in con.execute("SELECT doc_id, page FROM pages")}
    source_pages = [
        {"file": f, "page": p, "url": f"/api/pages/{docs[f]}/{p}"}
        for f, p in sorted(pages, key=lambda x: (x[0], x[1] or 0))
        if f in docs and (docs[f], p) in stored
    ]
    return {
        "borehole": bh.model_dump(mode="json"),
        "source_pages": source_pages,
        "review_log": store.review_log(con, borehole_id),
        "threshold": config.AUTO_APPROVE_THRESHOLD,
    }


class Review(BaseModel):
    decision: Literal["approved", "rejected", "pending_review"]
    reviewer: str = Field(min_length=1, max_length=80)
    comment: str | None = Field(None, max_length=2000)


@app.post("/api/boreholes/{borehole_id:path}/review")
def review(borehole_id: str, body: Review, con: DB):
    bh = store.record_review(con, borehole_id, ReviewStatus(body.decision), body.reviewer.strip(), body.comment)
    if bh is None:
        raise HTTPException(404, "borehole not found")
    return {"borehole": bh.model_dump(mode="json"), "review_log": store.review_log(con, borehole_id)}


@app.get("/api/pages/{doc_id}/{page}")
def page_image(doc_id: str, page: int, con: DB):
    row = con.execute("SELECT image FROM pages WHERE doc_id = ? AND page = ?", (doc_id, page)).fetchone()
    if row is None:
        raise HTTPException(404, "page not found")
    path = (config.PAGES_DIR / row["image"]).resolve()
    if config.PAGES_DIR.resolve() not in path.parents:
        raise HTTPException(400, "invalid path")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/stats")
def stats(con: DB):
    total = con.execute("SELECT COUNT(*) n, SUM(lat IS NOT NULL) located, SUM(n_layers) layers FROM boreholes").fetchone()
    by_status = {r["review_status"]: r["n"] for r in con.execute(
        "SELECT review_status, COUNT(*) n FROM boreholes GROUP BY review_status")}
    countries = con.execute(
        "SELECT country, COUNT(*) n FROM boreholes WHERE country IS NOT NULL GROUP BY country ORDER BY n DESC"
    ).fetchall()
    docs = con.execute("SELECT COUNT(*) n, SUM(pages) pages FROM documents").fetchone()
    return {
        "boreholes": total["n"],
        "located": total["located"] or 0,
        "layers": total["layers"] or 0,
        "documents": docs["n"],
        "pages": docs["pages"] or 0,
        "countries": [dict(r) for r in countries],
        "status": {s.value: by_status.get(s.value, 0) for s in ReviewStatus},
        "threshold": config.AUTO_APPROVE_THRESHOLD,
    }


@app.get("/api/lithologies")
def lithologies():
    return [{"key": lith.value, "label_de": DIN4023_LABEL_DE.get(lith)} for lith in Lithology]


@app.get("/api/export.csv")
def export_csv(con: DB, status: Literal["all", "released"] = "released"):
    """Flat export in the spirit of BoreholeDataFormat.xlsx: one row per layer. Released records only
    by default, so unreviewed extractions do not leak into downstream design work."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "BoreholeID", "BoreholeName", "ProjectName", "LocationName", "Latitude_WGS84", "Longitude_WGS84",
        "OriginalCoordinates", "OriginalCRS", "SurfaceElevation_m", "ElevationDatum", "BoreholeDepth_m",
        "LayerTop_m", "LayerBottom_m", "Lithology", "Description", "StratigraphicName", "StratigraphicCode",
        "GeologicAge", "Colour", "ReviewStatus", "ReviewedBy", "Score",
    ])
    where = "" if status == "all" else "WHERE review_status IN ('auto_approved', 'approved')"
    for row in con.execute(f"SELECT data FROM boreholes {where}"):
        bh = Borehole.model_validate_json(row["data"])
        loc = bh.location
        original_crs = f"EPSG:{loc.crs_epsg}" if loc.crs_epsg else loc.crs_raw
        for layer in bh.layers or [None]:
            w.writerow([
                bh.borehole_id, bh.borehole_name, bh.project_name, bh.location_name, loc.lat, loc.lon,
                loc.coords_raw, original_crs, bh.surface_elevation_m, bh.elevation_datum, bh.depth_m,
                *((layer.top_m, layer.bottom_m, layer.primary.value, layer.description_en or layer.description_original,
                   layer.stratigraphic_name, layer.stratigraphic_code, layer.geologic_age, layer.color)
                  if layer else [None] * 8),
                bh.review_status.value, bh.reviewed_by, bh.score,
            ])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=boreholes.csv"})


WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
