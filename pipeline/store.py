"""SQLite store: one file, no server, stays local (data security), R-tree for map queries,
FTS5 for full-text search over names, projects, places and layer descriptions, and a review
log for the human-in-the-loop decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from schema.borehole import Borehole, ReviewStatus

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY, path TEXT, kind TEXT, pages INTEGER
);
CREATE TABLE IF NOT EXISTS pages (
    doc_id TEXT, page INTEGER, kind TEXT, language TEXT, logs TEXT, image TEXT, thumb TEXT,
    width INTEGER, height INTEGER, PRIMARY KEY (doc_id, page)
);
CREATE TABLE IF NOT EXISTS boreholes (
    rid INTEGER PRIMARY KEY,
    id TEXT UNIQUE, name TEXT, doc_id TEXT, project TEXT, location_name TEXT, country TEXT,
    lat REAL, lon REAL, depth REAL, elevation REAL, investigation_type TEXT,
    confidence REAL, loc_confidence REAL, loc_method TEXT, score REAL, review_status TEXT,
    n_layers INTEGER, n_issues INTEGER, lithologies TEXT, strip TEXT, content_hash TEXT, data TEXT,
    extracted TEXT  -- model output before reviewer corrections (layers, position), for evaluation
);
CREATE INDEX IF NOT EXISTS boreholes_status ON boreholes (review_status, score);
CREATE VIRTUAL TABLE IF NOT EXISTS boreholes_rtree USING rtree(rid, min_lat, max_lat, min_lon, max_lon);
CREATE VIRTUAL TABLE IF NOT EXISTS boreholes_fts USING fts5(
    id UNINDEXED, name, project, location, descriptions, stratigraphy, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY, borehole_id TEXT, decision TEXT, reviewer TEXT, comment TEXT, at TEXT,
    content_hash TEXT
);
-- Reviewer corrections, re-applied after every run. scope 'borehole': target is a borehole id;
-- scope 'document': target is a document group (doc id before '__'), e.g. one CRS for all holes.
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY, scope TEXT, target TEXT, kind TEXT, payload TEXT, reviewer TEXT, comment TEXT, at TEXT
);
-- What reviewers taught the system, beyond single records. scope_type: document | project | region | global;
-- source: derived (from corrections) | review (entered from the documents) | external (phone, e-mail, site).
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY, scope_type TEXT, scope_value TEXT, kind TEXT, payload TEXT, source TEXT, note TEXT,
    created_by TEXT, created_at TEXT, confirmations INTEGER DEFAULT 1, active INTEGER DEFAULT 1, evidence TEXT
);
"""

HUMAN = (ReviewStatus.approved, ReviewStatus.rejected)


def connect(path: Path = config.DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")  # readers (API) never block the pipeline writer
    columns = {r["name"] for r in con.execute("PRAGMA table_info(boreholes)")}
    if columns and "review_status" not in columns:
        # Database from before the review workflow: derived tables, rebuildable via `pipeline rebuild`.
        con.executescript("DROP TABLE boreholes; DROP TABLE IF EXISTS boreholes_rtree; DROP TABLE IF EXISTS boreholes_fts;")
    con.executescript(SCHEMA)
    if "extracted" not in {r["name"] for r in con.execute("PRAGMA table_info(boreholes)")}:
        con.execute("ALTER TABLE boreholes ADD COLUMN extracted TEXT")  # database from before evaluation
    if "evidence" not in {r["name"] for r in con.execute("PRAGMA table_info(knowledge)")}:
        con.execute("ALTER TABLE knowledge ADD COLUMN evidence TEXT")  # from before independent confirmations
    return con


def reset(con: sqlite3.Connection) -> None:
    for table in ("boreholes", "boreholes_rtree", "boreholes_fts", "review_events"):
        con.execute(f"DELETE FROM {table}")
    con.commit()


def save_document(con: sqlite3.Connection, doc_id: str, path: str, kind: str, pages: list[dict]) -> None:
    con.execute("INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?)", (doc_id, path, kind, len(pages)))
    con.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))  # a re-render can have fewer pages
    con.executemany(
        "INSERT OR REPLACE INTO pages VALUES (:doc_id, :page, :kind, :language, :logs, :image, :thumb, :width, :height)",
        pages,
    )
    con.commit()


def content_hash(bh: Borehole) -> str:
    """Hash of everything a reviewer judged and the export publishes (not review fields): an approval
    stays only while exactly this content is stored."""
    payload = bh.model_dump(
        mode="json",
        include={"borehole_name", "location", "surface_elevation_m", "elevation_datum", "depth_m", "length_m",
                 "inclination_deg", "layers", "spt", "groundwater", "core_runs", "samples", "lab_results"},
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def extracted_summary(bh: Borehole) -> dict:
    """What the model produced, kept before reviewer corrections are applied: layers and position."""
    loc = bh.location
    return {"layers": [[layer.top_m, layer.bottom_m, layer.primary.value] for layer in bh.layers],
            "lat": loc.lat, "lon": loc.lon, "crs_epsg": loc.crs_epsg}


def _delete(con: sqlite3.Connection, borehole_id: str) -> dict | None:
    row = con.execute("SELECT rid, content_hash, data, extracted FROM boreholes WHERE id = ?",
                      (borehole_id,)).fetchone()
    if not row:
        return None
    con.execute("DELETE FROM boreholes WHERE rid = ?", (row["rid"],))
    con.execute("DELETE FROM boreholes_rtree WHERE rid = ?", (row["rid"],))
    con.execute("DELETE FROM boreholes_fts WHERE id = ?", (borehole_id,))
    return dict(row)


def delete_document_boreholes(con: sqlite3.Connection, doc_id: str) -> dict[str, dict]:
    """Remove a document's records; returns them so human decisions can be carried over."""
    previous = {}
    for row in con.execute("SELECT id FROM boreholes WHERE doc_id = ?", (doc_id,)).fetchall():
        previous[row["id"]] = _delete(con, row["id"])
    return previous


def save_borehole(con: sqlite3.Connection, bh: Borehole, commit: bool = True, previous: dict | None = None,
                  extracted: dict | None = None) -> None:
    """Insert or replace a record. `previous` is the earlier row if it was already deleted. `extracted` is the
    uncorrected model output (see `extracted_summary`); without it the previous row's is kept."""
    digest = content_hash(bh)
    previous = _delete(con, bh.borehole_id) or previous
    extracted_json = as_json(extracted) if extracted is not None else (previous or {}).get("extracted")
    if previous and previous.get("data"):
        old = Borehole.model_validate_json(previous["data"])
        if old.review_status in HUMAN:
            if previous.get("content_hash") == digest:
                # Same content: the human decision stands.
                bh.review_status, bh.reviewed_by = old.review_status, old.reviewed_by
                bh.reviewed_at, bh.review_comment = old.reviewed_at, old.review_comment
            else:
                bh.review_status = ReviewStatus.pending_review
                bh.score_reasons = [f"Data changed since the {old.review_status.value} decision by "
                                    f"{old.reviewed_by or 'a reviewer'}"] + bh.score_reasons
    _insert(con, bh, digest, extracted_json)
    if commit:
        con.commit()


def _insert(con: sqlite3.Connection, bh: Borehole, digest: str, extracted: str | None = None) -> None:
    loc = bh.location
    doc_id = bh.borehole_id.split(":", 1)[0]
    lithologies = sorted({layer.primary.value for layer in bh.layers})
    cur = con.execute(
        """INSERT INTO boreholes (id, name, doc_id, project, location_name, country, lat, lon, depth, elevation,
           investigation_type, confidence, loc_confidence, loc_method, score, review_status, n_layers, n_issues,
           lithologies, strip, content_hash, data, extracted)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bh.borehole_id, bh.borehole_name, doc_id, bh.project_name, bh.location_name, loc.country,
            loc.lat, loc.lon, bh.depth_m or (bh.layers[-1].bottom_m if bh.layers else None), bh.surface_elevation_m,
            bh.investigation_type.value if bh.investigation_type else None, bh.confidence, loc.confidence,
            loc.method.value if loc.method else None, bh.score, bh.review_status.value, len(bh.layers),
            len(bh.issues), ",".join(lithologies),
            as_json([[layer.top_m, layer.bottom_m, layer.primary.value] for layer in bh.layers]),
            digest, bh.model_dump_json(), extracted,
        ),
    )
    if loc.lat is not None and loc.lon is not None:
        con.execute("INSERT INTO boreholes_rtree VALUES (?, ?, ?, ?, ?)", (cur.lastrowid, loc.lat, loc.lat, loc.lon, loc.lon))
    descriptions = " ".join(
        " ".join(filter(None, [layer.description_original, layer.description_en, layer.primary.value]))
        for layer in bh.layers
    )
    stratigraphy = " ".join(
        " ".join(filter(None, [layer.stratigraphic_name, layer.stratigraphic_code, layer.geologic_age]))
        for layer in bh.layers
    )
    con.execute(
        "INSERT INTO boreholes_fts VALUES (?, ?, ?, ?, ?, ?)",
        (bh.borehole_id, bh.borehole_name, bh.project_name or "", bh.location_name or "", descriptions, stratigraphy),
    )


class StaleReview(Exception):
    """The record changed after the reviewer opened it."""


def record_review(con: sqlite3.Connection, borehole_id: str, decision: ReviewStatus, reviewer: str,
                  comment: str | None, expected_hash: str | None = None) -> Borehole | None:
    """Store a human decision in place (no delete/re-insert) and append it to the review log. With
    `expected_hash`, the decision is refused if the content changed since the reviewer saw it."""
    with con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT data, content_hash FROM boreholes WHERE id = ?", (borehole_id,)).fetchone()
        if row is None:
            return None
        if expected_hash and row["content_hash"] != expected_hash:
            raise StaleReview(borehole_id)
        bh = Borehole.model_validate_json(row["data"])
        now = datetime.now(timezone.utc)
        bh.review_status = decision
        bh.reviewed_by = reviewer if decision in HUMAN else None
        bh.reviewed_at = now if decision in HUMAN else None
        bh.review_comment = comment if decision in HUMAN else None
        con.execute(
            "UPDATE boreholes SET review_status = ?, data = ? WHERE id = ?",
            (decision.value, bh.model_dump_json(), borehole_id),
        )
        con.execute(
            "INSERT INTO review_events (borehole_id, decision, reviewer, comment, at, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (borehole_id, decision.value, reviewer, comment, now.isoformat(), row["content_hash"]),
        )
    return bh


def document_group(borehole_id: str) -> str:
    return borehole_id.split(":", 1)[0].split("__")[0]


def add_correction(con: sqlite3.Connection, scope: str, target: str, kind: str, payload: dict, reviewer: str,
                   comment: str | None) -> dict:
    at = datetime.now(timezone.utc).isoformat()
    cur = con.execute(
        "INSERT INTO corrections (scope, target, kind, payload, reviewer, comment, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (scope, target, kind, as_json(payload), reviewer, comment, at),
    )
    return {"id": cur.lastrowid, "scope": scope, "target": target, "kind": kind, "payload": payload,
            "reviewer": reviewer, "comment": comment, "at": at}


def corrections_for(con: sqlite3.Connection, borehole_id: str) -> list[dict]:
    """Corrections that apply to a record, oldest first (document-wide ones included)."""
    rows = con.execute(
        "SELECT * FROM corrections WHERE (scope = 'borehole' AND target = ?) OR (scope = 'document' AND target = ?) "
        "ORDER BY id", (borehole_id, document_group(borehole_id)),
    ).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def group_borehole_ids(con: sqlite3.Connection, group: str) -> list[str]:
    return [r["id"] for r in con.execute(
        "SELECT id FROM boreholes WHERE doc_id = ? OR doc_id LIKE ? ESCAPE '\\' ORDER BY id",
        (group, group.replace("_", "\\_").replace("%", "\\%") + "\\_\\_%"),
    )]


def add_knowledge(con: sqlite3.Connection, scope_type: str, scope_value: str | None, kind: str, payload: dict,
                  source: str, note: str | None, created_by: str, evidence: str | None = None) -> dict:
    """Store a knowledge item. The same fact again counts as a confirmation only with independent evidence
    (another document for derived facts, another person otherwise): repeating a correction proves nothing."""
    key = as_json(payload)
    evidence = evidence or f"by:{created_by}"
    row = con.execute(
        "SELECT id, evidence FROM knowledge WHERE active = 1 AND scope_type = ? AND IFNULL(scope_value, '') = ? "
        "AND kind = ? AND payload = ?", (scope_type, scope_value or "", kind, key),
    ).fetchone()
    if row:
        seen = set(json.loads(row["evidence"] or "[]"))
        if evidence not in seen:
            seen.add(evidence)
            con.execute("UPDATE knowledge SET evidence = ?, confirmations = ? WHERE id = ?",
                        (as_json(sorted(seen)), len(seen), row["id"]))
        return knowledge_item(con, row["id"])
    cur = con.execute(
        "INSERT INTO knowledge (scope_type, scope_value, kind, payload, source, note, created_by, created_at, evidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scope_type, scope_value, kind, key, source, note, created_by, datetime.now(timezone.utc).isoformat(),
         as_json([evidence])),
    )
    return knowledge_item(con, cur.lastrowid)


def knowledge_item(con: sqlite3.Connection, item_id: int) -> dict:
    row = con.execute("SELECT * FROM knowledge WHERE id = ?", (item_id,)).fetchone()
    return {**dict(row), "payload": json.loads(row["payload"])} if row else {}


def active_knowledge(con: sqlite3.Connection) -> list[dict]:
    return [{**dict(r), "payload": json.loads(r["payload"])}
            for r in con.execute("SELECT * FROM knowledge WHERE active = 1 ORDER BY id")]


def deactivate_knowledge(con: sqlite3.Connection, item_id: int) -> bool:
    return con.execute("UPDATE knowledge SET active = 0 WHERE id = ?", (item_id,)).rowcount > 0


def review_log(con: sqlite3.Connection, borehole_id: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT decision, reviewer, comment, at FROM review_events WHERE borehole_id = ? ORDER BY id DESC", (borehole_id,)
    )]


def load_borehole(con: sqlite3.Connection, borehole_id: str) -> Borehole | None:
    row = con.execute("SELECT data FROM boreholes WHERE id = ?", (borehole_id,)).fetchone()
    return Borehole.model_validate_json(row["data"]) if row else None


def page_rows(con: sqlite3.Connection, doc_id: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM pages WHERE doc_id = ? ORDER BY page", (doc_id,))]


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)
