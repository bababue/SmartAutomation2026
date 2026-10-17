"""Reads that several routes share."""

from __future__ import annotations

import sqlite3
from collections import Counter

from fastapi import HTTPException

from pipeline import export, scoring, store
from schema.borehole import Borehole

from .db import RELEASED, SOURCE_FILE
from .filters import Where


def load_record(con: sqlite3.Connection, borehole_id: str) -> dict:
    """The record with its review log and the state a client sends back with its next decision on it."""
    row = con.execute("SELECT uid, data, content_hash, review_revision FROM boreholes WHERE id = ?",
                      (borehole_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "borehole not found")
    return {"borehole": Borehole.model_validate_json(row["data"]), "review_log": store.review_log(con, borehole_id),
            "content_hash": row["content_hash"], "review_revision": row["review_revision"], "uid": row["uid"]}


def severity(bh: Borehole) -> dict[str, str]:
    return {i: scoring.finding_severity(i) for i in bh.issues}


def group_facets(con: sqlite3.Connection, where: Where) -> list[dict]:
    """The document groups among the matches, each labelled with the project name its holes give most often.
    The extracted project name varies from sheet to sheet (cut off, spelt otherwise), so it labels a group but
    does not form one."""
    projects: dict[str, Counter] = {}
    for r in con.execute("SELECT grp, TRIM(project) AS project, COUNT(*) n FROM boreholes "
                         f"WHERE {where.sql()} AND NULLIF(TRIM(project), '') IS NOT NULL GROUP BY grp, project",
                         where.params):
        projects.setdefault(r["grp"], Counter())[r["project"]] = r["n"]
    rows = con.execute(f"SELECT grp, COUNT(*) total, SUM({RELEASED}) released FROM boreholes WHERE {where.sql()} "
                       "GROUP BY grp ORDER BY grp", where.params)
    return [{"group": r["grp"], "project": projects[r["grp"]].most_common(1)[0][0] if r["grp"] in projects else None,
             "count": r["total"], "released": r["released"]} for r in rows]


def document_crs_groups(con: sqlite3.Connection) -> list[dict]:
    """Per document, the inferred coordinate system its waiting holes share, if at least two share it."""
    groups: dict[tuple[str, int], dict] = {}
    rows = con.execute(
        f"SELECT boreholes.id, f.epsg, {SOURCE_FILE} FROM findings f JOIN boreholes ON boreholes.rid = f.rid "
        "WHERE f.code = 'crs_inferred' AND f.epsg IS NOT NULL AND boreholes.review_status = 'pending_review' "
        "ORDER BY boreholes.id"
    )
    for r in rows:
        key = (store.document_group(r["id"]), r["epsg"])
        g = groups.setdefault(key, {"group": key[0], "epsg": r["epsg"], "name": export.crs_name(r["epsg"]),
                                    "count": 0, "borehole_id": r["id"], "source_file": r["source_file"]})
        g["count"] += 1
    # One CRS per document only: if a document has several inferred systems, a person looks at each hole.
    per_doc: dict[str, list[dict]] = {}
    for g in groups.values():
        per_doc.setdefault(g["group"], []).append(g)
    return sorted((gs[0] for gs in per_doc.values() if len(gs) == 1 and gs[0]["count"] >= 2),
                  key=lambda g: -g["count"])
