"""Key figures, the statistics view and the extraction quality report."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter

from pipeline import assemble, config, evaluate, quality
from schema.borehole import ReviewStatus

from .. import models
from ..db import DB, RELEASED
from ..filters import ListFilters

router = APIRouter()

# Final depths in the histogram: 5 m bins up to 50 m, everything deeper in one.
DEPTH_BIN_M, DEPTH_BINS = 5, 10
SEVERITY_ORDER = ("blocking", "needs_look", "warning", "info")
FROM_COORDINATES = "lat IS NOT NULL AND loc_method IN ('stated_coordinates', 'linked_table')"
# First match wins: a person's decision over what was learned from people, an open ambiguity over a guess.
CRS_DETERMINATION = " ".join(f"WHEN SUM({cond}) THEN '{key}'" for key, cond in (
    ("reviewer", "f.code = 'correction' AND f.text LIKE '%: coordinate system EPSG:%'"),
    ("knowledge", "f.code IN ('crs_from_knowledge', 'position_from_grid')"),
    ("ambiguous", "f.code = 'crs_ambiguous'"),
    ("document", "f.code = 'crs_from_document'"),
    ("inferred", "f.code IN ('crs_inferred', 'axes_inferred')"),
))


@router.get("/api/stats")
def stats(con: DB) -> models.Stats:
    total = con.execute("SELECT COUNT(*) n, SUM(lat IS NOT NULL) located FROM boreholes").fetchone()
    by_status = {r["review_status"]: r["n"] for r in con.execute(
        "SELECT review_status, COUNT(*) n FROM boreholes GROUP BY review_status")}
    return models.Stats(boreholes=total["n"], located=total["located"] or 0,
                        status={s: by_status.get(s.value, 0) for s in ReviewStatus},
                        threshold=config.AUTO_APPROVE_THRESHOLD, demo=config.DB_PATH.name.startswith("demo"))


@router.get("/api/stats/detail")
def stats_detail(con: DB, filters: ListFilters) -> models.StatsDetail:
    """Aggregates for the statistics view over the records the list filters select, holes without a position
    included: key figures, how positions and coordinate systems were obtained, release funnel, findings, logged
    metres per lithology, final depths and groups."""
    params = filters.params
    scope = f"SELECT rid FROM boreholes WHERE {filters.sql()}"

    def rows(sql: str) -> list[sqlite3.Row]:
        return con.execute(sql.replace("{scope}", scope), params).fetchall()

    totals, = rows(
        "SELECT COUNT(*) boreholes, COALESCE(SUM(n_layers), 0) layers, COUNT(DISTINCT doc_id) documents, "
        "COUNT(DISTINCT country) countries, "
        "SUM(lat IS NOT NULL AND loc_method = 'stated_coordinates') printed, "
        "SUM(lat IS NOT NULL AND loc_method = 'linked_table') linked_table, "
        "SUM(lat IS NOT NULL AND loc_method = 'manual') manual, "
        "SUM(lat IS NOT NULL AND loc_method = 'geocoded_place') place_name, SUM(lat IS NULL) none, "
        "SUM(NOT EXISTS (SELECT 1 FROM findings f WHERE f.rid = b.rid AND f.severity = 'blocking')) unblocked, "
        f"SUM(score >= {config.AUTO_APPROVE_THRESHOLD} AND NOT EXISTS (SELECT 1 FROM findings f WHERE f.rid = b.rid "
        "AND f.severity = 'blocking')) passed_rule, "
        "SUM(review_status = 'pending_review' AND EXISTS (SELECT 1 FROM findings f WHERE f.rid = b.rid "
        "AND f.severity = 'blocking')) waiting_blocking, "
        "SUM(lat IS NULL AND EXISTS (SELECT 1 FROM findings f WHERE f.rid = b.rid AND f.code = 'not_georeferenced' "
        f"AND f.text LIKE '%{assemble.SHARED_PLACE}')) withheld "
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
        f"SELECT grp, COUNT(*) n, SUM({RELEASED}) released "
        "FROM boreholes WHERE rid IN ({scope}) GROUP BY grp ORDER BY n DESC")
    top_finding: dict[str, dict] = {}  # most frequent finding that is no mere note, per group
    for r in rows("SELECT grp, f.code, COUNT(DISTINCT f.rid) n FROM findings f "
                  "JOIN boreholes b ON b.rid = f.rid WHERE f.rid IN ({scope}) "
                  "AND f.severity IN ('blocking', 'needs_look', 'warning') GROUP BY grp, f.code ORDER BY n DESC"):
        top_finding.setdefault(r["grp"], {"code": r["code"], "records": r["n"]})
    from_coordinates = f"rid IN ({{scope}}) AND {FROM_COORDINATES}"
    crs = rows("SELECT json_extract(data, '$.location.crs_epsg') epsg, COUNT(*) n FROM boreholes "
               f"WHERE {from_coordinates} GROUP BY epsg ORDER BY n DESC")
    how = {r["how"]: r["n"] for r in rows(
        f"SELECT how, COUNT(*) n FROM (SELECT CASE {CRS_DETERMINATION} ELSE 'stated' END how FROM boreholes b "
        f"LEFT JOIN findings f ON f.rid = b.rid WHERE b.{from_coordinates} GROUP BY b.rid) GROUP BY how")}
    return models.StatsDetail.model_validate({
        "documents": totals["documents"], "pages": pages, "boreholes": totals["boreholes"], "layers": totals["layers"],
        "countries": totals["countries"],
        "status": {s.value: by_status.get(s.value, 0) for s in ReviewStatus},
        "position": {k: totals[k] or 0 for k in models.PositionCounts.model_fields},
        "crs_determination": {k: how.get(k, 0) for k in models.CrsDetermination.model_fields},
        "positions_withheld": totals["withheld"] or 0,
        "waiting_blocking": totals["waiting_blocking"] or 0,
        "funnel": {"records": totals["boreholes"], "without_blocking": totals["unblocked"] or 0,
                   "passed_rule": totals["passed_rule"] or 0,
                   "released": by_status.get("auto_approved", 0) + by_status.get("approved", 0)},
        "threshold": config.AUTO_APPROVE_THRESHOLD,
        "findings": [{"code": r["code"], "severity": r["severity"], "records": r["records"], "count": r["n"]}
                     for r in findings],
        "lithology_m": [[r["primary_code"], round(r["m"], 2)] for r in metres],
        "depth_bin_m": DEPTH_BIN_M,
        "depths": depths,
        "groups": [{"group": r["grp"], "count": r["n"], "released": r["released"],
                    "top_finding": top_finding.get(r["grp"])} for r in group_rows],
        "crs": [[r["epsg"], r["n"]] for r in crs if r["epsg"] is not None],
        "crs_without_epsg": sum(r["n"] for r in crs if r["epsg"] is None),
    })


@router.get("/api/stats/quality")
def stats_quality(con: DB) -> models.Quality:
    """Extraction quality and model comparison: computed from data/eval where it exists, else the committed
    snapshot. The release audit and the recorded model calls cover the whole database."""
    if config.REFERENCE_PATH.exists():
        source, data = "live", quality.evaluation(con)
    else:
        data = quality.load_snapshot()
        source = "snapshot" if data else "none"
    return models.Quality(source=source, evaluation=data, audit=evaluate.audit_summary(con), usage=quality.usage(con))
