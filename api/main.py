"""HTTP API for the map UI. Run: .venv/bin/uvicorn api.main:app --reload --port 8000"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline import config, store
from schema.borehole import DIN4023_LABEL_DE, Lithology

app = FastAPI(title="Borehole Atlas API")
con = store.connect()

LIGHT_COLUMNS = (
    "id, name, project, location_name, country, lat, lon, depth, elevation, investigation_type, "
    "confidence, loc_confidence, loc_method, n_layers, n_issues, lithologies, strip, reviewed"
)


def _fts_query(q: str) -> str:
    tokens = re.findall(r"\w+", q, flags=re.UNICODE)
    return " AND ".join(f'"{t}"*' for t in tokens)


@app.get("/api/boreholes")
def list_boreholes(
    q: str | None = None,
    bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat"),
    lithology: list[str] = Query(default_factory=list),
    country: str | None = None,
    min_depth: float | None = None,
    min_confidence: float | None = None,
    has_issues: bool | None = None,
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
    if min_confidence is not None:
        where.append("confidence >= ?")
        params.append(min_confidence)
    if has_issues is not None:
        where.append("n_issues > 0" if has_issues else "n_issues = 0")
    rows = con.execute(f"SELECT {LIGHT_COLUMNS} FROM boreholes WHERE {' AND '.join(where)}", params).fetchall()
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {k: (json.loads(r[k]) if k == "strip" and r[k] else r[k]) for k in r.keys() if k not in ("lat", "lon")},
        }
        for r in rows
    ]
    return {"type": "FeatureCollection", "features": features}


@app.get("/api/boreholes/{borehole_id:path}/detail")
def get_borehole(borehole_id: str):
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
    return {"borehole": bh.model_dump(mode="json"), "source_pages": source_pages}


class Review(BaseModel):
    reviewed: bool


@app.post("/api/boreholes/{borehole_id:path}/review")
def review(borehole_id: str, body: Review):
    bh = store.load_borehole(con, borehole_id)
    if bh is None:
        raise HTTPException(404, "borehole not found")
    bh.reviewed = body.reviewed
    store.save_borehole(con, bh)
    return {"ok": True}


@app.get("/api/pages/{doc_id}/{page}")
def page_image(doc_id: str, page: int):
    row = con.execute("SELECT image FROM pages WHERE doc_id = ? AND page = ?", (doc_id, page)).fetchone()
    if row is None:
        raise HTTPException(404, "page not found")
    path = (config.PAGES_DIR / row["image"]).resolve()
    if config.PAGES_DIR.resolve() not in path.parents:
        raise HTTPException(400, "invalid path")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/stats")
def stats():
    total = con.execute("SELECT COUNT(*) n, SUM(lat IS NOT NULL) located, SUM(n_layers) layers FROM boreholes").fetchone()
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
    }


@app.get("/api/lithologies")
def lithologies():
    return [{"key": lith.value, "label_de": DIN4023_LABEL_DE.get(lith)} for lith in Lithology]


@app.get("/api/export.csv")
def export_csv():
    """Flat export in the spirit of BoreholeDataFormat.xlsx: one row per layer."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "BoreholeID", "BoreholeName", "ProjectName", "LocationName", "Latitude", "Longitude", "CoordinateSystem",
        "SurfaceElevation_mASL", "BoreholeDepth_m", "LayerTop_m", "LayerBottom_m", "Lithology", "Description",
        "StratigraphicName", "StratigraphicCode", "GeologicAge", "Colour", "Confidence",
    ])
    for row in con.execute("SELECT data FROM boreholes"):
        bh = store.Borehole.model_validate_json(row["data"])
        loc = bh.location
        for layer in bh.layers or [None]:
            w.writerow([
                bh.borehole_id, bh.borehole_name, bh.project_name, bh.location_name, loc.lat, loc.lon,
                f"EPSG:{loc.crs_epsg}" if loc.crs_epsg else loc.crs_raw, bh.surface_elevation_m, bh.depth_m,
                *((layer.top_m, layer.bottom_m, layer.primary.value, layer.description_en or layer.description_original,
                   layer.stratigraphic_name, layer.stratigraphic_code, layer.geologic_age, layer.color)
                  if layer else [None] * 8),
                bh.confidence,
            ])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=boreholes.csv"})


WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
