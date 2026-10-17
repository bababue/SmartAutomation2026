"""HTTP API for the map UI. Run: .venv/bin/uvicorn api.main:app --port 8000"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO, Annotated, Literal, NamedTuple

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from datetime import date as date_type

from pydantic import BaseModel, Field, ValidationError, model_validator
import pymupdf
from pyproj import CRS
from pyproj.exceptions import CRSError

from pipeline import config, corrections, evaluate, export, ingest, scoring, store, uploads
from schema.borehole import Borehole, LabParameter, Lithology, ReviewStatus

from . import similar


@asynccontextmanager
async def lifespan(app: FastAPI):
    queue = uploads.UploadQueue(config.DB_PATH)
    app.state.upload_queue = queue
    try:
        yield
    finally:
        await run_in_threadpool(queue.close)


app = FastAPI(title="Borehole Atlas API", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2000)  # map points and lists compress about 6:1


def db() -> Iterator[sqlite3.Connection]:
    """One connection per request: no shared transactions between concurrent requests."""
    con = store.connect()
    try:
        yield con
    finally:
        con.close()


DB = Annotated[sqlite3.Connection, Depends(db)]

# A hole's group in the result list: its document group (the document, or the folder of single images), as
# store.document_group derives it. The extracted project name varies from sheet to sheet (cut off, spelt
# otherwise), so it labels a group but does not form one.
GROUP = "CASE WHEN instr(doc_id, '__') > 0 THEN substr(doc_id, 1, instr(doc_id, '__') - 1) ELSE doc_id END"

SOURCE_FILE = "(SELECT path FROM documents d WHERE d.doc_id = boreholes.doc_id) AS source_file"

LIGHT_COLUMNS = (
    f"id, name, project, {GROUP} AS grp, location_name, country, lat, lon, depth, elevation, investigation_type, "
    f"confidence, loc_confidence, loc_method, score, review_status, n_layers, n_issues, lithologies, strip, ground, "
    f"{SOURCE_FILE}"
)

STATUS_GROUPS = {
    "released": ("auto_approved", "approved"),
    "pending": ("pending_review",),
    "rejected": ("rejected",),
}
RELEASED = f"review_status IN ({', '.join(repr(s) for s in STATUS_GROUPS['released'])})"


def _fts_query(q: str) -> str:
    tokens = re.findall(r"\w+", q, flags=re.UNICODE)
    return " AND ".join(f'"{t}"*' for t in tokens)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _group_facet(r: sqlite3.Row, projects: Counter) -> dict:
    """A document group among the matches, labelled with the project name its holes give most often."""
    return {"group": r["grp"], "project": projects.most_common(1)[0][0] if projects else None,
            "project_names": len(projects), "count": r["total"], "released": r["released"],
            "bbox": [r["west"], r["south"], r["east"], r["north"]]}


def _feature(r: sqlite3.Row) -> dict:
    props = {k: (json.loads(r[k]) if r[k] else None) if k in ("strip", "ground") else r[k]
             for k in r.keys() if k not in ("lat", "lon")}
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]}, "properties": props}


MAP_COLUMNS = "id, name, lat, lon, depth, review_status, loc_method"


def _bbox_clause(bbox: str) -> tuple[str, list]:
    """R-tree candidates by overlap (its coordinates are rounded outwards), then the exact position. A box
    crossing the antimeridian (min lon > max lon) is split in two."""
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(422, "bbox must contain four numbers: minLon,minLat,maxLon,maxLat")
    if not (all(math.isfinite(v) for v in (min_lon, min_lat, max_lon, max_lat))
            and -180 <= min_lon <= 180 and -180 <= max_lon <= 180 and -90 <= min_lat <= max_lat <= 90):
        raise HTTPException(422, "bbox must have valid longitude and latitude bounds, with minLat <= maxLat")
    spans = [(min_lon, max_lon)] if min_lon <= max_lon else [(min_lon, 180.0), (-180.0, max_lon)]
    parts, params = [], []
    for lo, hi in spans:
        parts.append("(rid IN (SELECT rid FROM boreholes_rtree WHERE max_lat >= ? AND min_lat <= ? AND max_lon >= ? "
                     "AND min_lon <= ?) AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?)")
        params += [min_lat, max_lat, lo, hi, min_lat, max_lat, lo, hi]
    return "(" + " OR ".join(parts) + ")", params


def _conditions(values: list[str], name: str) -> list[tuple[str, float | None, float | None]]:
    """'key', 'key:min:max', 'key:min:' or 'key::max' -> (key, min, max)."""
    out = []
    for value in values:
        key, *bounds = value.split(":")
        if not key or len(bounds) > 2:
            raise HTTPException(422, f"{name}: expected key or key:min:max, got {value!r}")
        try:
            lo, hi = [float(b) if b else None for b in [*bounds, "", ""][:2]]
        except ValueError:
            raise HTTPException(422, f"{name}: bounds must be numbers, got {value!r}") from None
        out.append((key, lo, hi))
    return out


StatusFilter = Literal["all", "released", "pending", "rejected"]


class Where(NamedTuple):
    clauses: list[str]
    params: list

    def also(self, clause: str, params: Sequence = ()) -> Where:
        return Where([*self.clauses, clause], [*self.params, *params])

    def sql(self) -> str:
        return " AND ".join(self.clauses) or "1"


def list_filters(
    q: str | None = None,
    lithology: list[str] = Query(default_factory=list,
                                 description="code[:from_m:to_m], e.g. clay:10:20 (a clay layer between 10 and 20 m)"),
    lab: list[str] = Query(default_factory=list, description="parameter[:min:max], e.g. ucs:80: (UCS of at least 80)"),
    min_position: float | None = Query(None, description="minimum position (georeference) confidence"),
    status: StatusFilter = "all",
    group: str | None = Query(None, description="only this document group (see the 'groups' facet)"),
    rock_head_max: float | None = Query(None, description="rock head at most this deep (m)"),
    groundwater_max: float | None = Query(None, description="groundwater at most this deep (m)"),
    material: list[str] = Query(default_factory=list, description="group:from_m:to_m:metres, e.g. sand:0:20:5"),
) -> Where:
    """WHERE clauses of the filters the list, the map and the statistics share."""
    where, params = [], []
    if group:
        where.append(f"{GROUP} = ?")
        params.append(group)
    if q and (fq := _fts_query(q)):
        # Text match, or every search word appears in the source document's file path (e.g. 'report').
        tokens = re.findall(r"\w+", q, flags=re.UNICODE)
        path_match = " AND ".join("path LIKE ? ESCAPE '\\'" for _ in tokens)
        where.append("(rid IN (SELECT rowid FROM boreholes_fts WHERE boreholes_fts MATCH ?) "
                     f"OR doc_id IN (SELECT doc_id FROM documents WHERE {path_match}))")
        params += [fq, *(f"%{_like_escape(t)}%" for t in tokens)]
    # Set lookups through the (code, depth) and (parameter, value) indexes. A correlated EXISTS per hole
    # would make SQLite scan every matching layer for every hole (171 s at 50,000 holes).
    for code, depth_from, depth_to in _conditions(lithology, "lithology"):  # each within its depth range
        clause = "rid IN (SELECT rid FROM layers WHERE primary_code = ?"
        params.append(code)
        if depth_to is not None:
            clause += " AND top_m < ?"
            params.append(depth_to)
        if depth_from is not None:
            clause += " AND bottom_m > ?"
            params.append(depth_from)
        where.append(clause + ")")
    for parameter, lab_min, lab_max in _conditions(lab, "lab"):  # each measured at least once within its range
        clause = "rid IN (SELECT rid FROM lab_values WHERE parameter = ?"
        params.append(parameter)
        if lab_min is not None:
            clause += " AND (qualifier IS NULL OR qualifier IN ('>', '≥', '>=', 'range')) AND value >= ?"
            params.append(lab_min)
        if lab_max is not None:
            clause += (" AND ((qualifier IS NULL OR qualifier IN ('<', '≤', '<=')) AND value <= ? "
                       "OR qualifier = 'range' AND value_upper >= value AND value_upper <= ?)")
            params += [lab_max, lab_max]
        where.append(clause + ")")
    if min_position is not None:
        where.append("loc_confidence >= ?")
        params.append(min_position)
    if status != "all":
        values = STATUS_GROUPS[status]
        where.append(f"review_status IN ({','.join('?' * len(values))})")
        params += list(values)
    try:
        materials = similar.material_conditions(material)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    ground_where, ground_params = similar.ground_filters(rock_head_max, groundwater_max, materials)
    return Where(where + ground_where, params + ground_params)


ListFilters = Annotated[Where, Depends(list_filters)]


STRIPS_LIMIT = 5000  # the map asks from street zoom on; a wider box gets a part of its holes, not the database


@app.get("/api/boreholes/strips")
def layer_strips(con: DB, bbox: str = Query(description="minLon,minLat,maxLon,maxLat (min > max crosses the antimeridian)")):
    """Depth strips of the holes in a map box, for the 3D columns."""
    clause, params = _bbox_clause(bbox)
    rows = con.execute(f"SELECT id, strip FROM boreholes WHERE {clause} LIMIT ?", [*params, STRIPS_LIMIT])
    return {"items": [{"id": r["id"], "strip": json.loads(r["strip"]) if r["strip"] else None} for r in rows]}


@app.get("/api/boreholes")
def list_boreholes(
    con: DB,
    filters: ListFilters,
    bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat (min > max crosses the antimeridian)"),
    id: list[str] = Query(default_factory=list, max_length=500, description="only these borehole ids"),
    fields: Literal["list", "map"] = Query("list", description="'map': slim points of every match"),
    limit: int = Query(1000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    """Filtered boreholes with a position. 'map' returns slim GeoJSON points; 'list' returns a GeoJSON page with the total, lithology counts and groups (document groups with their most
    frequent project name, count, released count and bounding box) of all matches, ordered by group."""
    where = filters.also("lat IS NOT NULL")
    if bbox:
        where = where.also(*_bbox_clause(bbox))
    if id:
        where = where.also(f"id IN ({','.join('?' * len(id))})", id)
    sql_where, params = where.sql(), where.params
    if fields == "map":
        rows = con.execute(f"SELECT {MAP_COLUMNS} FROM boreholes WHERE {sql_where}", params).fetchall()
        return {"type": "FeatureCollection", "total": len(rows), "features": [_feature(r) for r in rows]}
    total = con.execute(f"SELECT COUNT(*) FROM boreholes WHERE {sql_where}", params).fetchone()[0]
    rows = con.execute(f"SELECT {LIGHT_COLUMNS} FROM boreholes WHERE {sql_where} ORDER BY grp, name, id "
                       "LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    facets = con.execute(
        f"SELECT primary_code, COUNT(DISTINCT rid) n FROM layers WHERE rid IN (SELECT rid FROM boreholes WHERE {sql_where}) "
        "GROUP BY primary_code ORDER BY n DESC", params).fetchall()
    groups = con.execute(
        f"SELECT {GROUP} AS grp, COUNT(*) total, SUM({RELEASED}) released, "
        f"MIN(lon) west, MIN(lat) south, MAX(lon) east, MAX(lat) north FROM boreholes WHERE {sql_where} "
        "GROUP BY grp ORDER BY grp", params).fetchall()
    projects: dict[str, Counter] = {}
    for r in con.execute(f"SELECT {GROUP} AS grp, TRIM(project) AS project, COUNT(*) n FROM boreholes "
                         f"WHERE {sql_where} AND NULLIF(TRIM(project), '') IS NOT NULL GROUP BY grp, project", params):
        projects.setdefault(r["grp"], Counter())[r["project"]] = r["n"]
    return {"type": "FeatureCollection", "total": total, "offset": offset, "limit": limit,
            "facets": {"lithologies": [[r["primary_code"], r["n"]] for r in facets],
                       "groups": [_group_facet(r, projects.get(r["grp"], Counter())) for r in groups]},
            "features": [_feature(r) for r in rows]}


@app.get("/api/review-queue")
def review_queue(con: DB, limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0),
                 blocking: bool = Query(False, description="only records with a blocking finding"),
                 audit: bool = Query(False, description="the release audit sample instead (any status)")):
    """Everything waiting for a human, weakest first (also records without a position), a page at a time."""
    where, params = "review_status = 'pending_review'" + (
        " AND EXISTS (SELECT 1 FROM findings f WHERE f.rid = boreholes.rid AND f.severity = 'blocking')"
        if blocking else ""), []
    if audit:  # automatic releases drawn for a person to check (`pipeline eval audit`)
        uids = [r["uid"] for r in (evaluate.load_audit() or {"records": []})["records"]]
        where, params = f"uid IN ({','.join('?' * len(uids)) or 'NULL'})", uids
    total = con.execute(f"SELECT COUNT(*) FROM boreholes WHERE {where}", params).fetchone()[0]
    rows = con.execute(
        "SELECT id, name, project, location_name, country, score, confidence, loc_confidence, loc_method, "
        f"n_layers, {SOURCE_FILE}, reasons "
        f"FROM boreholes WHERE {where} ORDER BY score ASC, name, id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    items = [{**{k: r[k] for k in r.keys() if k != "reasons"}, "reasons": json.loads(r["reasons"] or "[]")}
             for r in rows]
    return {"items": items, "total": total}


@app.get("/api/review-queue/crs-groups")
def crs_groups(con: DB):
    """Documents whose waiting holes share one inferred coordinate system: a reviewer can confirm it
    for all of them at once (a document-wide CRS correction)."""
    groups: dict[tuple[str, int], dict] = {}
    rows = con.execute(
        f"SELECT boreholes.id, f.epsg, {SOURCE_FILE} FROM findings f JOIN boreholes ON boreholes.rid = f.rid "
        "WHERE f.code = 'crs_inferred' AND boreholes.review_status = 'pending_review' ORDER BY boreholes.id"
    )
    for r in rows:
        key = (store.document_group(r["id"]), r["epsg"])
        g = groups.setdefault(key, {"group": key[0], "epsg": r["epsg"], "name": export.crs_name(r["epsg"]),
                                    "count": 0, "borehole_id": r["id"], "source_file": r["source_file"]})
        g["count"] += 1
    # One CRS per document only: if a document has several inferred systems, a person looks at each hole.
    per_doc = {}
    for g in groups.values():
        per_doc.setdefault(g["group"], []).append(g)
    return sorted((gs[0] for gs in per_doc.values() if len(gs) == 1 and gs[0]["count"] >= 2),
                  key=lambda g: -g["count"])


@app.get("/api/corrections/orphans")
def orphan_corrections(con: DB):
    """Reviewer corrections of holes that no current record carries (renamed beyond recognition or gone)."""
    return store.orphan_corrections(con)


@app.get("/api/boreholes/{borehole_id:path}/similar")
def similar_boreholes(borehole_id: str, con: DB, limit: int = Query(5, ge=1, le=20)):
    """Holes of other documents with the most similar ground (lithology per depth band, water level)."""
    found = similar.similar(con, borehole_id, limit)
    if found is None:
        raise HTTPException(404, "borehole not found")
    return found


def _blocking(bh: Borehole) -> list[str]:
    """Findings that keep a record in the queue whatever its score."""
    return [i for i in bh.issues if scoring.finding_severity(i) == "blocking"]


class Review(BaseModel):
    decision: Literal["approved", "rejected", "pending_review"]
    reviewer: str = Field(min_length=1, max_length=80)
    comment: str | None = Field(None, max_length=2000)
    content_hash: str = Field(min_length=1, description="Version the reviewer saw; refused if it changed since")
    review_revision: int = Field(description="Review state the reviewer saw; refused if another decision was "
                                 "stored since")


def _review_state(con: sqlite3.Connection, borehole_id: str) -> dict:
    """What a client sends back with its next decision on this record."""
    row = con.execute("SELECT content_hash, review_revision FROM boreholes WHERE id = ?", (borehole_id,)).fetchone()
    return {"content_hash": row["content_hash"], "review_revision": row["review_revision"]}


@app.post("/api/boreholes/{borehole_id:path}/review")
def review(borehole_id: str, body: Review, con: DB):
    try:
        bh = store.record_review(con, borehole_id, ReviewStatus(body.decision), body.reviewer.strip(), body.comment,
                                 body.content_hash, body.review_revision)
    except store.StaleReview:
        raise HTTPException(409, "the record changed since you opened it; reload it and check again")
    if bh is None:
        raise HTTPException(404, "borehole not found")
    return {"borehole": bh.model_dump(mode="json"), "review_log": store.review_log(con, borehole_id),
            **_review_state(con, borehole_id)}


class CrsFix(BaseModel):
    epsg: int


class PositionFix(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(None, gt=0)


class LayerFix(BaseModel):
    orig_top: float
    orig_bottom: float
    top: float | None = None
    bottom: float | None = None
    primary: Lithology | None = None
    delete: bool = False
    description_original: str | None = Field(None, max_length=4000)
    stratigraphic_code: str | None = Field(None, max_length=80)
    stratigraphic_name: str | None = Field(None, max_length=200)
    geologic_age: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def _check(self):
        if (self.top is None) != (self.bottom is None):
            raise ValueError("give both top and bottom")
        if self.top is not None and not 0 <= self.top < self.bottom:
            raise ValueError("top must be at least 0 and above bottom")
        texts = (self.description_original, self.stratigraphic_code, self.stratigraphic_name, self.geologic_age)
        if not self.delete and self.top is None and self.primary is None and all(t is None for t in texts):
            raise ValueError("nothing to change")
        return self


class LayerAddFix(BaseModel):
    top: float = Field(ge=0)
    bottom: float
    primary: Lithology
    description_original: str | None = Field(None, max_length=4000)

    @model_validator(mode="after")
    def _check(self):
        if self.bottom <= self.top:
            raise ValueError("bottom must be below top")
        return self


class FieldFix(BaseModel):
    field: Literal[tuple(corrections.FIELDS)]  # type: ignore[valid-type]
    value: str | float | None

    @model_validator(mode="after")
    def _check(self):
        kind = corrections.FIELDS[self.field]
        if self.value is None or self.value == "":
            self.value = None
        elif kind is float:
            self.value = float(self.value)
        elif kind is date_type:
            self.value = date_type.fromisoformat(str(self.value)).isoformat()
        else:
            self.value = str(self.value).strip()[:4000 if self.field == "notes" else 300]
        if self.field == "borehole_name" and not self.value:
            raise ValueError("a borehole name cannot be empty")
        if kind is float and self.value is not None and not math.isfinite(self.value):
            raise ValueError("numeric fields must be finite")
        return self


class GroundwaterFix(BaseModel):
    depth_m: float | None = Field(None, ge=0)
    dry: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.depth_m is None and not self.dry:
            raise ValueError("give a depth or mark the hole as dry")
        return self


class ResultFix(BaseModel):
    test: Literal["spt", "core", "lab"]
    orig_depth: float
    parameter: LabParameter | None = None
    delete: bool = False
    value: float | None = None
    rqd: float | None = Field(None, ge=0, le=100)

    @model_validator(mode="after")
    def _check(self):
        if (self.test == "lab") != (self.parameter is not None):
            raise ValueError("a parameter is given for lab results only, and always")
        if not self.delete and self.value is None:
            raise ValueError("give a value or delete the result")
        if not self.delete and self.test != "lab" and not (0 <= self.value <= (100 if self.test == "core" else 1000)):
            raise ValueError("value out of range")
        if not self.delete and self.test == "spt" and self.value != int(self.value):
            raise ValueError("an SPT N value is a whole number")
        return self


FIXES = {"crs": CrsFix, "position": PositionFix, "layer": LayerFix, "layer_add": LayerAddFix, "field": FieldFix,
         "groundwater": GroundwaterFix, "test": ResultFix}


class Correction(BaseModel):
    kind: Literal["crs", "position", "layer", "layer_add", "field", "groundwater", "test"]
    scope: Literal["borehole", "document"] = "borehole"
    payload: dict
    reviewer: str = Field(min_length=1, max_length=80)
    comment: str | None = Field(None, max_length=2000)


@app.post("/api/boreholes/{borehole_id:path}/corrections")
def correct(borehole_id: str, body: Correction, con: DB):
    """Store a reviewer correction and apply it now; later runs re-apply it. A coordinate system can be
    set for all holes of the document at once (scope 'document')."""
    try:
        payload = FIXES[body.kind].model_validate(body.payload).model_dump(mode="json", exclude_none=body.kind != "field")
    except ValidationError as exc:
        raise HTTPException(422, f"invalid {body.kind} correction: {exc.errors()[0]['msg']}")
    if body.kind == "crs":
        try:
            CRS.from_epsg(payload["epsg"])
        except CRSError:
            raise HTTPException(422, f"unknown EPSG code {payload['epsg']}")
    if body.scope == "document" and body.kind != "crs":
        raise HTTPException(422, "only a coordinate system can be set for a whole document")
    if store.load_borehole(con, borehole_id) is None:
        raise HTTPException(404, "borehole not found")

    group = store.document_group(borehole_id)
    targets = store.group_borehole_ids(con, group) if body.scope == "document" else [borehole_id]
    item = {"kind": body.kind, "payload": payload, "reviewer": body.reviewer.strip()}
    updated = []
    with con:
        con.execute("BEGIN IMMEDIATE")  # read-modify-write: concurrent corrections and rebuilds wait
        for target in targets:
            bh = store.load_borehole(con, target)
            if bh is None or (body.kind == "crs" and not bh.location.coords_raw):
                continue
            if not corrections.apply(bh, [item])[0]:
                continue  # does not fit this record: leave it unchanged
            store.save_borehole(con, bh, commit=False)
            updated.append(target)
        if not updated:
            raise HTTPException(422, f"the {corrections.describe(body.kind, payload)} does not fit this record")
        store.add_correction(con, body.scope, group if body.scope == "document" else borehole_id, body.kind, payload,
                             item["reviewer"], body.comment)
        if body.kind == "crs":  # a confirmed coordinate system is also evidence for the country
            country = (store.load_borehole(con, updated[0]).location.country or "").upper()
            if country:
                store.add_knowledge(con, "region", country, "crs", {"epsg": payload["epsg"]}, "derived",
                                    f"confirmed for {group}", item["reviewer"], evidence=f"document:{group}")
    bh = store.load_borehole(con, borehole_id)
    return {"borehole": bh.model_dump(mode="json"), "review_log": store.review_log(con, borehole_id),
            "corrections": store.corrections_for(con, borehole_id), "blocking": _blocking(bh), "updated": updated,
            **_review_state(con, borehole_id)}


# After the sub-routes: the path converter would also match '<id>/similar'.
@app.get("/api/boreholes/{borehole_id:path}")
def get_borehole(borehole_id: str, con: DB):
    bh = store.load_borehole(con, borehole_id)
    if bh is None:
        raise HTTPException(404, "borehole not found")
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
    meta = con.execute("SELECT uid, version_id, content_hash, review_revision FROM boreholes WHERE id = ?",
                       (borehole_id,)).fetchone()
    return {
        "borehole": bh.model_dump(mode="json"),
        "source_pages": source_pages,
        "review_log": store.review_log(con, borehole_id),
        "corrections": store.corrections_for(con, borehole_id, meta["uid"]),
        "blocking": _blocking(bh),
        "content_hash": meta["content_hash"],
        "uid": meta["uid"], "version_id": meta["version_id"], "review_revision": meta["review_revision"],
        "threshold": config.AUTO_APPROVE_THRESHOLD,
    }


@app.post("/api/uploads")
async def upload_documents(files: list[UploadFile] = File(...)):
    """One batch of files, or zips of them, becomes one document group assembled in a single run, so
    records of one hole spread over several files are combined. Other files in the batch
    (e.g. GIS files) are skipped and reported, not a reason to reject the documents beside them."""
    batch, skipped = [], []
    for file in files:
        name = _safe_part((file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]) or "upload"
        if Path(name).suffix.lower() in ingest.SUPPORTED | {".zip"}:
            batch.append((file, name))
        else:
            skipped.append(name)
    if not batch:
        raise HTTPException(415, "no PDF, JPG, PNG, TIFF, DOCX, TXT, XLSX or ZIP file in the upload")
    from pipeline.runner import missing_credentials
    if msg := missing_credentials():
        raise HTTPException(503, msg)
    job_id = uuid.uuid4().hex[:12]
    folder = f"upload-{job_id}"
    upload_dir = config.UPLOADS_DIR / folder
    upload_dir.mkdir(parents=True, exist_ok=True)
    docs: list[ingest.SourceDoc] = []
    try:
        budget = _ByteBudget()
        for file, name in batch:
            if name.lower().endswith(".zip"):
                with tempfile.TemporaryFile() as archive:
                    while chunk := await file.read(1 << 20):
                        budget.received(len(chunk))
                        archive.write(chunk)
                    skipped += await run_in_threadpool(_extract_zip, archive, name, upload_dir, budget)
            else:
                with _unique_path(upload_dir, name).open("wb") as out:
                    while chunk := await file.read(1 << 20):
                        budget.received(len(chunk))
                        budget.stored(len(chunk))
                        out.write(chunk)
        docs = [ingest.SourceDoc(f"{folder}__{d.doc_id}", d.path, d.kind) for d in ingest.discover(upload_dir)]
        if not docs:
            raise HTTPException(415, "the upload contains no PDF, JPG, PNG, TIFF, DOCX, TXT or XLSX file")
        pages = await run_in_threadpool(_render_upload, docs, upload_dir)
        await run_in_threadpool(app.state.upload_queue.submit, uploads.UploadTask(
            job_id=job_id, folder=folder,
            docs=[uploads.UploadDoc(doc_id=d.doc_id, path=d.path, kind=d.kind) for d in docs]))
    except Exception as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        for d in docs:
            shutil.rmtree(config.PAGES_DIR / d.doc_id, ignore_errors=True)
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, uploads.QueueFull):
            raise HTTPException(503, "the upload queue is full; try again after a document finishes processing") from exc
        raise
    return {"job_id": job_id, "pages": pages, "files": [Path(d.path).as_posix() for d in docs], "skipped": skipped}


class _ByteBudget:
    """MAX_UPLOAD_BYTES applies to the whole batch: to the bytes received and to the documents stored,
    so a small zip cannot expand past the limit."""

    def __init__(self) -> None:
        self.received_bytes = self.stored_bytes = 0

    def received(self, n: int) -> None:
        self.received_bytes += n
        self._check(self.received_bytes)

    def stored(self, n: int) -> None:
        self.stored_bytes += n
        self._check(self.stored_bytes)

    @staticmethod
    def _check(total: int) -> None:
        if total > config.MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"the upload exceeds the {config.MAX_UPLOAD_BYTES // (1 << 20)} MB upload limit")


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


def _extract_zip(archive: IO[bytes], name: str, upload_dir: Path, budget: _ByteBudget) -> list[str]:
    """Extracts the documents; returns the other files it skipped."""
    skipped = []
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise HTTPException(422, f"could not read {name}: is it a valid ZIP file?") from None
    with zf:
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


@app.get("/api/uploads/{job_id}")
def upload_status(job_id: str, con: DB) -> uploads.UploadResult:
    job = uploads.result(con, job_id)
    if job is None:
        raise HTTPException(404, "upload job not found")
    if job.status == "processing":
        job.progress = app.state.upload_queue.progress.get(job_id)
    return job


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
        "demo": config.DB_PATH.name.startswith("demo"),  # synthetic fixtures, no source pages
    }


# Final depths in the histogram: 5 m bins up to 50 m, everything deeper in one.
DEPTH_BIN_M, DEPTH_BINS = 5, 10
SEVERITY_ORDER = ("blocking", "needs_look", "warning", "info")


@app.get("/api/stats/detail")
def stats_detail(con: DB, filters: ListFilters):
    """Aggregates for the statistics view over the records the list filters select, holes without a position
    included: key figures, release funnel, findings, logged metres per lithology, final depths and groups."""
    params = filters.params
    scope = f"SELECT rid FROM boreholes WHERE {filters.sql()}"

    def rows(sql: str) -> list[sqlite3.Row]:
        return con.execute(sql.replace("{scope}", scope), params).fetchall()

    totals, = rows(
        "SELECT COUNT(*) boreholes, COALESCE(SUM(n_layers), 0) layers, COUNT(DISTINCT doc_id) documents, "
        "COUNT(DISTINCT country) countries, "
        "SUM(loc_method IN ('stated_coordinates', 'linked_table', 'manual')) exact, "
        "SUM(loc_method = 'geocoded_place') approximate, SUM(lat IS NULL) unlocated, "
        "SUM(NOT EXISTS (SELECT 1 FROM findings f WHERE f.rid = b.rid AND f.severity = 'blocking')) unblocked "
        "FROM boreholes b WHERE rid IN ({scope})")
    (pages,), = rows("SELECT COALESCE(SUM(pages), 0) FROM documents WHERE doc_id IN "
                     "(SELECT doc_id FROM boreholes WHERE rid IN ({scope}))")
    by_status = {r["review_status"]: r["n"] for r in rows(
        "SELECT review_status, COUNT(*) n FROM boreholes WHERE rid IN ({scope}) GROUP BY review_status")}
    findings = sorted(rows(
        "SELECT code, severity, COUNT(DISTINCT rid) records, COUNT(*) n FROM findings WHERE rid IN ({scope}) "
        "GROUP BY code, severity"),
        key=lambda r: (SEVERITY_ORDER.index(r["severity"]) if r["severity"] in SEVERITY_ORDER else 9, -r["records"]))
    metres = rows("SELECT primary_code, SUM(bottom_m - top_m) m FROM layers WHERE rid IN ({scope}) "
                  "GROUP BY primary_code ORDER BY m DESC")
    depths = [0] * (DEPTH_BINS + 1)
    for r in rows(f"SELECT MIN(CAST(depth / {DEPTH_BIN_M} AS INTEGER), {DEPTH_BINS}) bin, COUNT(*) n "
                  "FROM boreholes WHERE rid IN ({scope}) AND depth IS NOT NULL GROUP BY bin"):
        depths[r["bin"]] = r["n"]
    group_rows = rows(
        f"SELECT {GROUP} grp, COUNT(*) n, SUM({RELEASED}) released "
        "FROM boreholes WHERE rid IN ({scope}) GROUP BY grp ORDER BY n DESC")
    top_finding: dict[str, dict] = {}  # most frequent finding that is no mere note, per group
    for r in rows(f"SELECT {GROUP} grp, f.code, COUNT(DISTINCT f.rid) n FROM findings f "
                  "JOIN boreholes b ON b.rid = f.rid WHERE f.rid IN ({scope}) "
                  "AND f.severity IN ('blocking', 'needs_look', 'warning') GROUP BY grp, f.code ORDER BY n DESC"):
        top_finding.setdefault(r["grp"], {"code": r["code"], "records": r["n"]})
    crs = rows("SELECT json_extract(data, '$.location.crs_epsg') epsg, COUNT(*) n FROM boreholes "
               "WHERE rid IN ({scope}) AND json_extract(data, '$.location.crs_epsg') IS NOT NULL GROUP BY epsg "
               "ORDER BY n DESC")
    return {
        "documents": totals["documents"], "pages": pages, "boreholes": totals["boreholes"], "layers": totals["layers"],
        "countries": totals["countries"],
        "status": {s.value: by_status.get(s.value, 0) for s in ReviewStatus},
        "position": {"exact": totals["exact"] or 0, "approximate": totals["approximate"] or 0,
                     "none": totals["unlocated"] or 0},
        "funnel": {"records": totals["boreholes"], "without_blocking": totals["unblocked"] or 0,
                   "released_automatically": by_status.get("auto_approved", 0),
                   "released": by_status.get("auto_approved", 0) + by_status.get("approved", 0)},
        "threshold": config.AUTO_APPROVE_THRESHOLD,
        "findings": [{"code": r["code"], "severity": r["severity"], "records": r["records"], "count": r["n"]}
                     for r in findings],
        "lithology_m": [[r["primary_code"], round(r["m"], 2)] for r in metres],
        "depth_bin_m": DEPTH_BIN_M,
        "depths": depths,
        "groups": [{"group": r["grp"], "count": r["n"], "released": r["released"],
                    "top_finding": top_finding.get(r["grp"])} for r in group_rows],
        "crs": [[r["epsg"], r["n"]] for r in crs],
        "audit": evaluate.audit_summary(con),  # the whole database: the sample is drawn from all releases
    }


@app.get("/api/export.csv")
def export_csv(status: Literal["all", "released"] = "released"):
    """Flat export in the spirit of BoreholeDataFormat.xlsx: one row per layer. Released records only
    by default, so unreviewed extractions do not leak into downstream design work. Streamed record by
    record from its own connection, so memory stays flat however large the database is."""
    return StreamingResponse(_csv_rows(status), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=boreholes.csv"})


def _export_rows(con: sqlite3.Connection, status: str) -> sqlite3.Cursor:
    where = "" if status == "all" else f"WHERE {RELEASED}"
    return con.execute(f"SELECT data, {SOURCE_FILE} FROM boreholes {where} ORDER BY id")


def _csv_rows(status: str) -> Iterator[str]:
    buf = io.StringIO()
    w = csv.writer(buf)

    def flush() -> str:
        text = buf.getvalue()
        buf.seek(0)
        buf.truncate()
        return text

    w.writerow([
        "BoreholeID", "BoreholeName", "SourceFile", "SourcePages", "ProjectName", "LocationName",
        "Latitude_WGS84", "Longitude_WGS84",
        "OriginalCoordinates", "OriginalCRS", "SurfaceElevation_m", "ElevationDatum", "BoreholeDepth_m",
        "LayerTop_m", "LayerBottom_m", "Lithology", "Description", "StratigraphicName", "StratigraphicCode",
        "GeologicAge", "Colour", "ReviewStatus", "ReviewedBy", "Score",
    ])
    yield flush()
    con = store.connect()
    try:
        for row in _export_rows(con, status):
            _csv_record(w, row)
            yield flush()
    finally:
        con.close()


def _csv_record(w, row: sqlite3.Row) -> None:
    bh = Borehole.model_validate_json(row["data"])
    loc = bh.location
    original_crs = f"EPSG:{loc.crs_epsg}" if loc.crs_epsg else loc.crs_proj or loc.crs_raw
    source_file = row["source_file"]
    pages = {s.page for s in bh.sources if s.page is not None and (source_file is None or s.file == source_file)}
    source_pages = ";".join(str(p) for p in sorted(pages)) or None
    for layer in bh.layers or [None]:
        w.writerow([
            bh.borehole_id, bh.borehole_name, source_file, source_pages, bh.project_name, bh.location_name,
            loc.lat, loc.lon,
            loc.coords_raw, original_crs, bh.surface_elevation_m, bh.elevation_datum, bh.depth_m,
            *((layer.top_m, layer.bottom_m, layer.primary.value, layer.description_en or layer.description_original,
               layer.stratigraphic_name, layer.stratigraphic_code, layer.geologic_age, layer.color)
              if layer else [None] * 8),
            bh.review_status.value, bh.reviewed_by, bh.score,
        ])


@app.get("/api/export.xlsx")
def export_xlsx(con: DB, status: Literal["all", "released"] = "released"):
    """Workbook in Herrenknecht's BoreholeDataFormat (metadata, log data, samples, core runs, SPT,
    groundwater, test results). Released records only by default, like the CSV."""
    content = export.workbook([(Borehole.model_validate_json(r["data"]), r["source_file"])
                               for r in _export_rows(con, status)])
    return StreamingResponse(
        iter([content]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=boreholes.xlsx"},
    )


WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
