"""Finding boreholes: the list, the map, depth strips, one record, similar holes and source page images."""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import Field

from pipeline import config, store

from .. import models, similar
from ..db import DB, SQLITE_MAX_INT
from ..filters import ListFilters, bbox_clause
from ..queries import document_crs_groups, group_facets, load_record, severity

router = APIRouter()

LIGHT_COLUMNS = ("id, name, project, grp, location_name, country, lat, lon, depth, elevation, "
                 "loc_method, score, review_status, strip, ground")
MAP_COLUMNS = "id, name, lat, lon, depth, review_status, loc_method"

STRIPS_LIMIT = 5000  # the map asks from street zoom on; a wider box gets a part of its holes, not the database


def _feature(r: sqlite3.Row) -> dict:
    props = {k: (json.loads(r[k]) if r[k] else None) if k in ("strip", "ground") else r[k]
             for k in r.keys() if k not in ("lat", "lon")}
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]}, "properties": props}


@router.get("/api/boreholes/strips")
def layer_strips(con: DB, bbox: str = Query(description="minLon,minLat,maxLon,maxLat (min > max crosses the antimeridian)")
                 ) -> models.LayerStrips:
    """Depth strips of the holes in a map box, for the 3D columns."""
    clause, params = bbox_clause(bbox)
    rows = con.execute(f"SELECT id, strip FROM boreholes WHERE {clause} LIMIT ?", [*params, STRIPS_LIMIT])
    return models.LayerStrips.model_validate(
        {"items": [{"id": r["id"], "strip": json.loads(r["strip"]) if r["strip"] else None} for r in rows]})


@router.get("/api/boreholes")
def list_boreholes(
    con: DB,
    filters: ListFilters,
    bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat (min > max crosses the antimeridian)"),
    id: list[str] = Query(default_factory=list, max_length=500, description="only these borehole ids"),
    fields: Literal["list", "map"] = Query("list", description="'map': slim points of every match"),
    limit: int = Query(1000, ge=1, le=10000),
    offset: int = Query(0, ge=0, le=SQLITE_MAX_INT),
) -> models.ListPage | models.MapPoints:
    """Filtered boreholes with a position. 'map' returns slim GeoJSON points of every match; 'list' returns a
    GeoJSON page with the total, lithology counts and groups (document groups with their most frequent project
    name, count, released count and bounding box) of all matches, ordered by group."""
    where = filters.also("lat IS NOT NULL")
    if bbox:
        where = where.also(*bbox_clause(bbox))
    if id:
        where = where.also(f"id IN ({','.join('?' * len(id))})", id)
    sql_where, params = where.sql(), where.params
    if fields == "map":
        rows = con.execute(f"SELECT {MAP_COLUMNS} FROM boreholes WHERE {sql_where}", params).fetchall()
        return models.MapPoints.model_validate({"total": len(rows), "features": [_feature(r) for r in rows]})
    total = con.execute(f"SELECT COUNT(*) FROM boreholes WHERE {sql_where}", params).fetchone()[0]
    rows = con.execute(f"SELECT {LIGHT_COLUMNS} FROM boreholes WHERE {sql_where} ORDER BY grp, name, id "
                       "LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    lithologies = con.execute(
        f"SELECT primary_code, COUNT(DISTINCT rid) n FROM layers WHERE rid IN (SELECT rid FROM boreholes WHERE {sql_where}) "
        "GROUP BY primary_code ORDER BY n DESC", params).fetchall()
    return models.ListPage.model_validate({
        "total": total, "offset": offset, "limit": limit,
        "facets": {"lithologies": [[r["primary_code"], r["n"]] for r in lithologies],
                   "groups": group_facets(con, where)},
        "features": [_feature(r) for r in rows]})


@router.get("/api/boreholes/{borehole_id:path}/similar")
def similar_boreholes(borehole_id: str, con: DB, limit: int = Query(5, ge=1, le=20)) -> models.SimilarGround:
    """Holes of other documents with the most similar ground (lithology per depth band, water level)."""
    found = similar.similar(con, borehole_id, limit)
    if found is None:
        raise HTTPException(404, "borehole not found")
    return found


# After the sub-routes: the path converter would also match '<id>/similar'.
@router.get("/api/boreholes/{borehole_id:path}")
def get_borehole(borehole_id: str, con: DB) -> models.Detail:
    record = load_record(con, borehole_id)
    bh = record["borehole"]
    pages = {(s.file, s.page) for s in bh.sources}
    files = sorted({f for f, _ in pages})
    marks = ",".join("?" * len(files))
    docs = {r["path"]: r["doc_id"] for r in con.execute(f"SELECT doc_id, path FROM documents WHERE path IN ({marks})", files)}
    stored = {(r["doc_id"], r["page"]) for r in con.execute(
        f"SELECT doc_id, page FROM pages WHERE doc_id IN ({','.join('?' * len(docs))})", list(docs.values()))}
    source_pages = [
        {"file": f, "page": p, "url": f"/api/pages/{docs[f]}/{p}"}
        for f, p in sorted(pages, key=lambda x: (x[0], x[1] or 0))
        if f in docs and (docs[f], p) in stored
    ]
    inferred = con.execute(
        "SELECT f.epsg FROM findings f JOIN boreholes b ON b.rid = f.rid WHERE b.id = ? AND f.code = 'crs_inferred' "
        "AND f.epsg IS NOT NULL AND b.review_status = 'pending_review'", (borehole_id,)).fetchone()
    # The document-wide confirmation this waiting hole belongs to, if its document has one.
    crs_group = inferred and next((g for g in document_crs_groups(con) if g["group"] == store.document_group(borehole_id)
                                   and g["epsg"] == inferred["epsg"]), None)
    return models.Detail.model_validate({
        **record, "source_pages": source_pages, "corrections": store.corrections_for(con, borehole_id, record["uid"]),
        "severity": severity(bh), "threshold": config.AUTO_APPROVE_THRESHOLD, "crs_group": crs_group or None})


@router.get("/api/pages/{doc_id}/{page}")
def page_image(doc_id: str, page: Annotated[int, Field(ge=1, le=SQLITE_MAX_INT)], con: DB):
    row = con.execute("SELECT image FROM pages WHERE doc_id = ? AND page = ?", (doc_id, page)).fetchone()
    if row is None:
        raise HTTPException(404, "page not found")
    path = (config.PAGES_DIR / row["image"]).resolve()
    if config.PAGES_DIR.resolve() not in path.parents:
        raise HTTPException(400, "invalid path")
    if not path.is_file():
        raise HTTPException(404, "page image not found")
    return FileResponse(path, media_type="image/jpeg")
