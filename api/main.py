"""HTTP API for the map UI. Run: .venv/bin/uvicorn api.main:app --port 8000"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from datetime import date as date_type

from pydantic import BaseModel, Field, ValidationError, model_validator
from pyproj import CRS
from pyproj.exceptions import CRSError

from pipeline import config, corrections, export, ingest, knowledge, scoring, store
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
    "confidence, loc_confidence, loc_method, score, review_status, n_layers, n_issues, lithologies, strip, "
    + "(SELECT path FROM documents d WHERE d.doc_id = boreholes.doc_id) AS source_file"
)

STATUS_GROUPS = {
    "released": ("auto_approved", "approved"),
    "pending": ("pending_review",),
    "rejected": ("rejected",),
}


def _fts_query(q: str) -> str:
    tokens = re.findall(r"\w+", q, flags=re.UNICODE)
    return " AND ".join(f'"{t}"*' for t in tokens)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        # Text match, or every search word appears in the source document's file path (e.g. 'AUS01').
        tokens = re.findall(r"\w+", q, flags=re.UNICODE)
        path_match = " AND ".join("path LIKE ? ESCAPE '\\'" for _ in tokens)
        where.append("(id IN (SELECT id FROM boreholes_fts WHERE boreholes_fts MATCH ?) "
                     f"OR doc_id IN (SELECT doc_id FROM documents WHERE {path_match}))")
        params += [fq, *(f"%{_like_escape(t)}%" for t in tokens)]
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
        "n_layers, (SELECT path FROM documents d WHERE d.doc_id = boreholes.doc_id) AS source_file, "
        "data FROM boreholes WHERE review_status = 'pending_review' ORDER BY score ASC, name"
    ).fetchall()
    return [
        {**{k: r[k] for k in r.keys() if k != "data"}, "reasons": json.loads(r["data"]).get("score_reasons", [])}
        for r in rows
    ]


INFERRED_CRS = re.compile(r"^coordinate system inferred, not stated: EPSG:(\d+)")


@app.get("/api/review-queue/crs-groups")
def crs_groups(con: DB):
    """Documents whose waiting holes share one inferred coordinate system: a reviewer can confirm it
    for all of them at once (a document-wide CRS correction)."""
    groups: dict[tuple[str, int], dict] = {}
    rows = con.execute(
        "SELECT id, data, (SELECT path FROM documents d WHERE d.doc_id = boreholes.doc_id) AS source_file "
        "FROM boreholes WHERE review_status = 'pending_review' ORDER BY id"
    )
    for r in rows:
        issues = json.loads(r["data"]).get("issues", [])
        epsg = next((int(m.group(1)) for i in issues if (m := INFERRED_CRS.match(i))), None)
        if epsg is None:
            continue
        key = (store.document_group(r["id"]), epsg)
        g = groups.setdefault(key, {"group": key[0], "epsg": epsg, "name": export._crs_name(epsg),
                                    "count": 0, "borehole_id": r["id"], "source_file": r["source_file"]})
        g["count"] += 1
    # One CRS per document only: if a document has several inferred systems, a person looks at each hole.
    per_doc = {}
    for g in groups.values():
        per_doc.setdefault(g["group"], []).append(g)
    return sorted((gs[0] for gs in per_doc.values() if len(gs) == 1 and gs[0]["count"] >= 2),
                  key=lambda g: -g["count"])


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
        "corrections": store.corrections_for(con, borehole_id),
        "blocking": _blocking(bh),
        "threshold": config.AUTO_APPROVE_THRESHOLD,
    }


def _blocking(bh: Borehole) -> list[str]:
    """Findings that keep a record in the queue whatever its score."""
    return [i for i in bh.issues if any(b in i for b in scoring.BLOCKING)]


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
            self.value = str(self.value).strip()[:300]
        return self


class GroundwaterFix(BaseModel):
    depth_m: float | None = Field(None, ge=0)
    dry: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.depth_m is None and not self.dry:
            raise ValueError("give a depth or mark the hole as dry")
        return self


FIXES = {"crs": CrsFix, "position": PositionFix, "layer": LayerFix, "layer_add": LayerAddFix, "field": FieldFix,
         "groundwater": GroundwaterFix}


class Correction(BaseModel):
    kind: Literal["crs", "position", "layer", "layer_add", "field", "groundwater"]
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
                                    f"confirmed for {group}", item["reviewer"])
    bh = store.load_borehole(con, borehole_id)
    return {"borehole": bh.model_dump(mode="json"), "review_log": store.review_log(con, borehole_id),
            "corrections": store.corrections_for(con, borehole_id), "blocking": _blocking(bh), "updated": updated}


# --- knowledge -------------------------------------------------------------------------------------

_rebuild_lock = threading.Lock()
REBUILD = {"running": False, "documents": 0, "error": None}


def _rebuild_in_background(doc_ids: set[str]) -> int:
    """Re-assemble the affected documents from the cache so new knowledge shows at once (no API calls)."""
    docs = [d for d in ingest.discover() if d.doc_id in doc_ids]

    def work():
        from pipeline.__main__ import rebuild
        with _rebuild_lock:
            REBUILD.update(running=True, documents=len(docs), error=None)
            try:
                rebuild(docs)
            except Exception as exc:  # reported via /api/knowledge/status
                REBUILD["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                REBUILD["running"] = False

    if docs:
        threading.Thread(target=work, daemon=True).start()
    return len(docs)


def _affected_docs(con: sqlite3.Connection, item: dict) -> set[str]:
    rows = con.execute("SELECT doc_id, country, project FROM boreholes").fetchall()
    st, sv = item["scope_type"], item["scope_value"]
    if st == "global":
        return {r["doc_id"] for r in rows}
    if st == "document":
        return {r["doc_id"] for r in rows if r["doc_id"].split("__")[0] == sv}
    if st == "region":
        return {r["doc_id"] for r in rows if (r["country"] or "").upper() == sv}
    return {r["doc_id"] for r in rows if knowledge.norm_project(r["project"]) == sv}


class KnowledgeIn(BaseModel):
    borehole_id: str | None = Field(None, description="Record the scope is taken from (not needed for global)")
    scope_type: Literal["document", "project", "region", "global"]
    kind: Literal["crs", "term", "note"]
    payload: dict
    source: Literal["review", "external"] = "review"
    note: str | None = Field(None, max_length=2000)
    reviewer: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def _check(self):
        p = self.payload
        if self.kind == "crs":
            try:
                CRS.from_epsg(int(p.get("epsg")))
            except (CRSError, TypeError, ValueError):
                raise ValueError("unknown EPSG code")
            self.payload = {"epsg": int(p["epsg"])}
        elif self.kind == "term":
            term = str(p.get("term") or "").strip()
            if not 2 <= len(term) <= 80:
                raise ValueError("give a term of 2 to 80 characters")
            self.payload = {"term": term, "primary": Lithology(p.get("primary")).value}
        else:
            text = str(p.get("text") or "").strip()
            if not text:
                raise ValueError("write the note")
            self.payload = {"text": text[:2000]}
        if self.scope_type != "global" and not self.borehole_id:
            raise ValueError("a scope other than global needs a record")
        return self


@app.get("/api/knowledge")
def list_knowledge(con: DB, borehole_id: str | None = None):
    """Everything known (or what applies to one record), with the record's scope values for new items."""
    items = store.active_knowledge(con)
    if borehole_id is None:
        return {"items": items, "scopes": None, "rebuild": REBUILD}
    bh = store.load_borehole(con, borehole_id)
    if bh is None:
        raise HTTPException(404, "borehole not found")
    return {"items": knowledge.applicable(items, bh), "scopes": knowledge.scope_keys(bh), "rebuild": REBUILD}


@app.post("/api/knowledge")
def add_knowledge(body: KnowledgeIn, con: DB):
    scope_value = None
    if body.scope_type != "global":
        bh = store.load_borehole(con, body.borehole_id)
        if bh is None:
            raise HTTPException(404, "borehole not found")
        scope_value = knowledge.scope_keys(bh)[body.scope_type]
        if scope_value is None:
            raise HTTPException(422, f"this record has no {body.scope_type} to attach the knowledge to")
    with con:
        item = store.add_knowledge(con, body.scope_type, scope_value, body.kind, body.payload, body.source, body.note,
                                   body.reviewer.strip())
    return {"item": item, "rebuilding": _rebuild_in_background(_affected_docs(con, item))}


@app.delete("/api/knowledge/{item_id}")
def remove_knowledge(item_id: int, con: DB):
    item = store.knowledge_item(con, item_id)
    with con:
        if not item or not store.deactivate_knowledge(con, item_id):
            raise HTTPException(404, "knowledge item not found")
    return {"rebuilding": _rebuild_in_background(_affected_docs(con, item))}


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
        "BoreholeID", "BoreholeName", "SourceFile", "SourcePages", "ProjectName", "LocationName",
        "Latitude_WGS84", "Longitude_WGS84",
        "OriginalCoordinates", "OriginalCRS", "SurfaceElevation_m", "ElevationDatum", "BoreholeDepth_m",
        "LayerTop_m", "LayerBottom_m", "Lithology", "Description", "StratigraphicName", "StratigraphicCode",
        "GeologicAge", "Colour", "ReviewStatus", "ReviewedBy", "Score",
    ])
    where = "" if status == "all" else "WHERE review_status IN ('auto_approved', 'approved')"
    rows = con.execute(
        f"SELECT data, (SELECT path FROM documents d WHERE d.doc_id = boreholes.doc_id) AS source_file "
        f"FROM boreholes {where}"
    )
    for row in rows:
        bh = Borehole.model_validate_json(row["data"])
        loc = bh.location
        original_crs = f"EPSG:{loc.crs_epsg}" if loc.crs_epsg else loc.crs_raw
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
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=boreholes.csv"})


@app.get("/api/export.xlsx")
def export_xlsx(con: DB, status: Literal["all", "released"] = "released"):
    """Workbook in Herrenknecht's BoreholeDataFormat (metadata, log data, samples, core runs, SPT,
    groundwater, test results). Released records only by default, like the CSV."""
    where = "" if status == "all" else "WHERE review_status IN ('auto_approved', 'approved')"
    rows = con.execute(
        f"SELECT data, (SELECT path FROM documents d WHERE d.doc_id = boreholes.doc_id) AS source_file "
        f"FROM boreholes {where} ORDER BY id"
    ).fetchall()
    content = export.workbook([(Borehole.model_validate_json(r["data"]), r["source_file"]) for r in rows])
    return StreamingResponse(
        iter([content]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=boreholes.xlsx"},
    )


WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
