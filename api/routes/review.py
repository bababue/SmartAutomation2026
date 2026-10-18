"""Reviewing records: the queue, decisions, corrections and document-wide coordinate system confirmations."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError
from pyproj import CRS
from pyproj.exceptions import CRSError

from pipeline import corrections, evaluate, store
from schema.borehole import ReviewStatus

from .. import models
from ..db import DB, SOURCE_FILE, SQLITE_MAX_INT
from ..filters import StatusFilter, Where, release_filters
from ..queries import document_crs_groups, group_facets, load_record, severity
from ..requests import FIXES, Correction, Review

router = APIRouter()

# Where a waiting record's position comes from; 'crs_inferred': its coordinate system is only inferred.
QUEUE_POSITION = {
    "printed": "lat IS NOT NULL AND loc_method IN ('stated_coordinates', 'linked_table', 'manual')",
    "estimated": "lat IS NOT NULL AND loc_method = 'geocoded_place'",
    "crs_inferred": "rid IN (SELECT rid FROM findings WHERE code = 'crs_inferred')",
    "none": "lat IS NULL",
}
QueuePosition = Literal["printed", "estimated", "crs_inferred", "none"]


def _queue_filters(finding: str | None, position: QueuePosition | None, status: StatusFilter,
                   score_min: float | None, score_max: float | None) -> Where:
    where = release_filters(status, score_min, score_max)
    if finding in ("blocking", "needs_look"):
        where = where.also("rid IN (SELECT rid FROM findings WHERE severity = ?)", [finding])
    elif finding:
        where = where.also("rid IN (SELECT rid FROM findings WHERE code = ?)", [finding])
    if position:
        where = where.also(QUEUE_POSITION[position])
    return where


@router.get("/api/review-queue")
def review_queue(con: DB, limit: int = Query(200, ge=1, le=2000),
                 offset: int = Query(0, ge=0, le=SQLITE_MAX_INT),
                 finding: str | None = Query(None, description="a finding code, or a severity: 'blocking', 'needs_look'"),
                 position: QueuePosition | None = Query(None, description="where the position comes from"),
                 status: StatusFilter = "pending",
                 score_min: float | None = Query(None, ge=0, le=1, description="lowest release score"),
                 score_max: float | None = Query(None, ge=0, le=1, description="highest release score"),
                 audit: bool = Query(False, description="the release audit sample instead (any status)")
                 ) -> models.QueuePage:
    """Records of one release state (by default everything waiting for a human), weakest first (also records
    without a position), a page at a time."""
    if audit:  # automatic releases drawn for a person to check (`pipeline eval audit`)
        uids = [r["uid"] for r in (evaluate.load_audit() or {"records": []})["records"]]
        where = Where([f"uid IN ({','.join('?' * len(uids)) or 'NULL'})"], uids)
    else:
        where = _queue_filters(finding, position, status, score_min, score_max)
    total = con.execute(f"SELECT COUNT(*) FROM boreholes WHERE {where.sql()}", where.params).fetchone()[0]
    rows = con.execute(
        "SELECT id, name, project, grp, location_name, country, score, review_status, loc_method, "
        f"n_layers, {SOURCE_FILE}, reasons "
        f"FROM boreholes WHERE {where.sql()} ORDER BY score ASC, name, id LIMIT ? OFFSET ?",
        (*where.params, limit, offset),
    ).fetchall()
    items = [{**{k: r[k] for k in r.keys() if k != "reasons"}, "reasons": json.loads(r["reasons"] or "[]")}
             for r in rows]
    return models.QueuePage.model_validate({"items": items, "total": total, "groups": group_facets(con, Where([], []))})


@router.get("/api/review-queue/findings")
def queue_findings(con: DB, position: QueuePosition | None = None, status: StatusFilter = "pending",
                   score_min: float | None = Query(None, ge=0, le=1), score_max: float | None = Query(None, ge=0, le=1)
                   ) -> list[models.QueueFinding]:
    """Findings among the records of one release state (and position source, if given), for the queue filter.
    Notes that hold nothing back are left out."""
    where = _queue_filters(None, position, status, score_min, score_max)
    rows = con.execute(
        "SELECT code, severity, COUNT(DISTINCT rid) records FROM findings WHERE severity != 'info' AND rid IN "
        f"(SELECT rid FROM boreholes WHERE {where.sql()}) GROUP BY code, severity ORDER BY records DESC", where.params)
    return [models.QueueFinding(code=r["code"], severity=r["severity"], records=r["records"]) for r in rows]


@router.get("/api/review-queue/crs-groups")
def crs_groups(con: DB) -> list[models.CrsGroup]:
    """Documents whose waiting holes share one inferred coordinate system: a reviewer can confirm it
    for all of them at once (a document-wide CRS correction)."""
    return document_crs_groups(con)


@router.get("/api/corrections/orphans")
def orphan_corrections(con: DB) -> list[models.OrphanCorrection]:
    """Reviewer corrections of holes that no current record carries (renamed beyond recognition or gone)."""
    return store.orphan_corrections(con)


@router.post("/api/boreholes/{borehole_id:path}/review")
def review(borehole_id: str, body: Review, con: DB) -> models.ReviewResult:
    try:
        bh = store.record_review(con, borehole_id, ReviewStatus(body.decision), body.reviewer, body.comment,
                                 body.content_hash, body.review_revision)
    except store.StaleReview:
        raise HTTPException(409, "the record changed since you opened it; reload it and check again")
    if bh is None:
        raise HTTPException(404, "borehole not found")
    return models.ReviewResult.model_validate(load_record(con, borehole_id))


@router.post("/api/boreholes/{borehole_id:path}/corrections")
def correct(borehole_id: str, body: Correction, con: DB) -> models.CorrectionResult:
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
    item = {"kind": body.kind, "payload": payload, "reviewer": body.reviewer}
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
                             body.reviewer, body.comment)
        if body.kind == "crs":  # a confirmed coordinate system is also evidence for the country
            country = (store.load_borehole(con, updated[0]).location.country or "").upper()
            if country:
                store.add_knowledge(con, "region", country, "crs", {"epsg": payload["epsg"]}, "derived",
                                    f"confirmed for {group}", body.reviewer, evidence=f"document:{group}")
    record = load_record(con, borehole_id)
    return models.CorrectionResult.model_validate({
        **record, "corrections": store.corrections_for(con, borehole_id, record["uid"]),
        "severity": severity(record["borehole"]), "updated": updated})
