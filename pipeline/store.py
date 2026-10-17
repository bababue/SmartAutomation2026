"""SQLite store: one file, no server, stays local (data security), R-tree for map queries,
FTS5 for full-text search over names, projects, places and layer descriptions, and a review
log for the human-in-the-loop decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from schema.borehole import Borehole, ReviewStatus

from . import config, ground, scoring

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
    extracted TEXT,  -- model output before reviewer corrections (layers, position), for evaluation
    ground TEXT  -- derived ground model values (pipeline/ground.py)
);
CREATE INDEX IF NOT EXISTS boreholes_status ON boreholes (review_status, score);
CREATE VIRTUAL TABLE IF NOT EXISTS boreholes_rtree USING rtree(rid, min_lat, max_lat, min_lon, max_lon);
CREATE INDEX IF NOT EXISTS boreholes_doc ON boreholes (doc_id);
-- Full-text rows share the borehole's rid as rowid, so updates and deletes are point operations.
CREATE VIRTUAL TABLE IF NOT EXISTS boreholes_fts USING fts5(
    name, project, location, descriptions, stratigraphy, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY, borehole_id TEXT, decision TEXT, reviewer TEXT, comment TEXT, at TEXT,
    content_hash TEXT
);
CREATE INDEX IF NOT EXISTS review_events_borehole ON review_events (borehole_id, id);
-- Reviewer corrections, re-applied after every run. scope 'borehole': target is a borehole id;
-- scope 'document': target is a document group (doc id before '__'), e.g. one CRS for all holes.
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY, scope TEXT, target TEXT, kind TEXT, payload TEXT, reviewer TEXT, comment TEXT, at TEXT
);
CREATE INDEX IF NOT EXISTS corrections_target ON corrections (scope, target, id);
-- What reviewers taught the system, beyond single records. scope_type: document | project | region | global;
-- source: derived (from corrections) | review (entered from the documents) | external (phone, e-mail, site).
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY, scope_type TEXT, scope_value TEXT, kind TEXT, payload TEXT, source TEXT, note TEXT,
    created_by TEXT, created_at TEXT, confirmations INTEGER DEFAULT 1, active INTEGER DEFAULT 1, evidence TEXT
);
CREATE INDEX IF NOT EXISTS knowledge_scope ON knowledge (scope_type, scope_value, kind);
"""

# Version 3: queryable projections (layers, lab values, findings, sources), stable borehole uids with
# aliases, immutable record versions, review revision. Older databases are migrated in connect().
SCHEMA_VERSION = 3

SCHEMA_V3 = """
-- Projections of the current records for queries, rebuilt from 'data' at any time.
CREATE TABLE IF NOT EXISTS layers (
    rid INTEGER NOT NULL, seq INTEGER NOT NULL, top_m REAL, bottom_m REAL, primary_code TEXT, strat_code TEXT,
    geologic_age TEXT, PRIMARY KEY (rid, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS layers_lith ON layers (primary_code, top_m, bottom_m);
CREATE TABLE IF NOT EXISTS lab_values (
    rid INTEGER NOT NULL, seq INTEGER NOT NULL, parameter TEXT, value REAL, qualifier TEXT, top_m REAL,
    bottom_m REAL, sample TEXT, PRIMARY KEY (rid, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS lab_param ON lab_values (parameter, value);
CREATE TABLE IF NOT EXISTS findings (
    rid INTEGER NOT NULL, seq INTEGER NOT NULL, code TEXT, severity TEXT, epsg INTEGER, text TEXT,
    PRIMARY KEY (rid, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS findings_code ON findings (code, epsg);
CREATE TABLE IF NOT EXISTS record_sources (
    rid INTEGER NOT NULL, file TEXT NOT NULL, page INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL,
    PRIMARY KEY (rid, file, page, role)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sources_page ON record_sources (file, page);
-- Identity: a durable uid per physical hole, ids and natural keys ('nk:group|type|number') are aliases.
CREATE TABLE IF NOT EXISTS borehole_aliases (alias TEXT PRIMARY KEY, uid TEXT NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS aliases_uid ON borehole_aliases (uid);
-- Every distinct content a record had: what a reviewer saw stays reconstructable.
CREATE TABLE IF NOT EXISTS record_versions (
    id INTEGER PRIMARY KEY, uid TEXT NOT NULL, content_hash TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS versions_uid ON record_versions (uid, id);
CREATE INDEX IF NOT EXISTS boreholes_queue ON boreholes (review_status, score, name, id);
"""


HUMAN = (ReviewStatus.approved, ReviewStatus.rejected)


def _add_columns(con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    present = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in present:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_v3(con: sqlite3.Connection) -> None:
    """Projections, uids, aliases and a first version for every stored record (no rebuild needed)."""
    with con:
        _add_columns(con, "boreholes", {"reasons": "TEXT", "uid": "TEXT", "version_id": "INTEGER",
                                        "review_revision": "INTEGER NOT NULL DEFAULT 0"})
        _add_columns(con, "corrections", {"uid": "TEXT"})
        _add_columns(con, "review_events", {"uid": "TEXT", "version_id": "INTEGER"})
        for table in ("layers", "lab_values", "findings", "record_sources"):  # derived: always rebuilt
            con.execute(f"DROP TABLE IF EXISTS {table}")
        for statement in SCHEMA_V3.split(";"):  # executescript would commit the open transaction
            if statement.strip():
                con.execute(statement)
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS boreholes_uid ON boreholes (uid)")
        con.execute("CREATE INDEX IF NOT EXISTS corrections_uid ON corrections (uid, id)")
        con.execute("CREATE INDEX IF NOT EXISTS review_events_uid ON review_events (uid, id)")
        for row in con.execute("SELECT rid, id, data, content_hash, uid FROM boreholes").fetchall():
            try:
                bh = Borehole.model_validate_json(row["data"])
            except ValidationError:
                continue
            uid = row["uid"] or _resolve_uid(con, bh) or uuid.uuid4().hex
            version_id = _version(con, uid, row["content_hash"], row["data"])
            con.execute("UPDATE boreholes SET uid = ?, version_id = ?, reasons = ? WHERE rid = ?",
                        (uid, version_id, as_json(bh.score_reasons), row["rid"]))
            _remember_aliases(con, bh, uid)
            _write_projections(con, row["rid"], bh)
        con.execute("""UPDATE corrections SET uid = (SELECT uid FROM boreholes b WHERE b.id = corrections.target)
                       WHERE scope = 'borehole' AND uid IS NULL""")
        con.execute("""UPDATE review_events SET uid = (SELECT uid FROM boreholes b WHERE b.id = review_events.borehole_id)
                       WHERE uid IS NULL""")
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def connect(path: Path = config.DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")  # readers (API) never block the pipeline writer
    columns = {r["name"] for r in con.execute("PRAGMA table_info(boreholes)")}
    if columns and "review_status" not in columns:
        # Database from before the review workflow: derived tables, rebuildable via `pipeline rebuild`.
        con.executescript("DROP TABLE boreholes; DROP TABLE IF EXISTS boreholes_rtree; DROP TABLE IF EXISTS boreholes_fts;")
    fts_columns = {r["name"] for r in con.execute("PRAGMA table_info(boreholes_fts)")}
    rebuild_fts = "id" in fts_columns  # old full-text table keyed by a text id: derived, rebuilt below
    if rebuild_fts:
        con.execute("DROP TABLE boreholes_fts")
    con.executescript(SCHEMA)
    if rebuild_fts:
        with con:
            for row in con.execute("SELECT rid, data FROM boreholes").fetchall():
                _insert_fts(con, row["rid"], Borehole.model_validate_json(row["data"]))
    if "extracted" not in {r["name"] for r in con.execute("PRAGMA table_info(boreholes)")}:
        con.execute("ALTER TABLE boreholes ADD COLUMN extracted TEXT")  # database from before evaluation
    if "evidence" not in {r["name"] for r in con.execute("PRAGMA table_info(knowledge)")}:
        con.execute("ALTER TABLE knowledge ADD COLUMN evidence TEXT")  # from before independent confirmations
    if "ground" not in {r["name"] for r in con.execute("PRAGMA table_info(boreholes)")}:
        with con:  # derived from the stored records, no rebuild needed
            con.execute("ALTER TABLE boreholes ADD COLUMN ground TEXT")
            for row in con.execute("SELECT rid, data FROM boreholes").fetchall():
                try:
                    summary = ground.summarize(Borehole.model_validate_json(row["data"])).model_dump_json()
                except ValidationError:
                    continue  # stays empty until the next rebuild
                con.execute("UPDATE boreholes SET ground = ? WHERE rid = ?", (summary, row["rid"]))
    if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        _migrate_v3(con)
    con.execute("CREATE INDEX IF NOT EXISTS boreholes_queue ON boreholes (review_status, score, name, id)")
    return con


def reset(con: sqlite3.Connection) -> None:
    for table in ("boreholes", "boreholes_rtree", "boreholes_fts", "review_events", "layers", "lab_values",
                  "findings", "record_sources", "borehole_aliases", "record_versions"):
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
    """What the model produced, kept before reviewer corrections are applied, in the form `eval` compares.
    `key` (the natural key) matches the same hole when a variant names it differently. A lab value is
    [parameter, value, top_m, qualifier, name, unit], name and unit as assembly tells chemistry rows apart."""
    from .assemble import _lab_name
    loc = bh.location
    return {"key": natural_key(bh),
            "layers": [[layer.top_m, layer.bottom_m, layer.primary.value] for layer in bh.layers],
            "lat": loc.lat, "lon": loc.lon, "crs_epsg": loc.crs_epsg, "depth_m": bh.depth_m,
            "groundwater": sorted(gw.depth_m for gw in bh.groundwater if gw.depth_m is not None),
            "lab": [[r.parameter.value, r.value, r.top_m, r.qualifier, *_lab_name(r)] for r in bh.lab_results]}


def _delete(con: sqlite3.Connection, borehole_id: str) -> dict | None:
    row = con.execute("SELECT rid, content_hash, data, extracted, uid, review_revision FROM boreholes WHERE id = ?",
                      (borehole_id,)).fetchone()
    if not row:
        return None
    con.execute("DELETE FROM boreholes WHERE rid = ?", (row["rid"],))
    con.execute("DELETE FROM boreholes_rtree WHERE rid = ?", (row["rid"],))
    con.execute("DELETE FROM boreholes_fts WHERE rowid = ?", (row["rid"],))
    for table in ("layers", "lab_values", "findings", "record_sources"):
        con.execute(f"DELETE FROM {table} WHERE rid = ?", (row["rid"],))
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
    revision = (previous or {}).get("review_revision") or 0
    if previous and previous.get("data"):
        old = Borehole.model_validate_json(previous["data"])
        if old.review_status in HUMAN:
            new_blocking = _new_blocking(old, bh) if old.review_status == ReviewStatus.approved else []
            if previous.get("content_hash") == digest and not new_blocking:
                # Same content: the human decision stands.
                bh.review_status, bh.reviewed_by = old.review_status, old.reviewed_by
                bh.reviewed_at, bh.review_comment = old.reviewed_at, old.review_comment
            elif previous.get("content_hash") == digest:
                revision += 1  # same hash: only the revision stops a review screen opened before from approving
                bh.review_status = ReviewStatus.pending_review
                bh.score_reasons = [f"New blocking finding since the approval by {old.reviewed_by or 'a reviewer'}: "
                                    + "; ".join(new_blocking)] + bh.score_reasons
            else:
                bh.review_status = ReviewStatus.pending_review
                bh.score_reasons = [f"Data changed since the {old.review_status.value} decision by "
                                    f"{old.reviewed_by or 'a reviewer'}"] + bh.score_reasons
    uid = (previous or {}).get("uid") or _resolve_uid(con, bh) or uuid.uuid4().hex
    if con.execute("SELECT 1 FROM boreholes WHERE uid = ? AND id != ?", (uid, bh.borehole_id)).fetchone():
        uid = uuid.uuid4().hex  # another current record is that hole: never share an identity
    version_id = _version(con, uid, digest, bh.model_dump_json())
    _insert(con, bh, digest, extracted_json, uid, version_id, revision)
    _remember_aliases(con, bh, uid)
    if commit:
        con.commit()


def _new_blocking(old: Borehole, bh: Borehole) -> list[str]:
    """Blocking findings of `bh` whose rule the reviewer did not see on `old`. Keyed by code and the blocking
    rule rather than the text, which carries depths and partner names that vary between runs; the code alone
    would lump distinct rules together under 'other'."""
    def rules(issue: str) -> set[tuple[str, str]]:
        if not scoring.is_blocking(issue):
            return set()
        return {(scoring.finding_code(issue), rule) for rule in scoring.BLOCKING if rule in issue}
    seen = set().union(*(rules(i) for i in old.issues))
    return [i for i in bh.issues if rules(i) - seen]


def natural_key(bh: Borehole) -> str:
    """The hole by what it is: document group, type and number ('nk:report|borehole|12')."""
    from .assemble import hole_key  # assembly rules define what counts as the same hole
    kind, number = hole_key(bh.borehole_name, bh.investigation_type.value if bh.investigation_type else None)
    return f"nk:{document_group(bh.borehole_id)}|{kind or ''}|{number}"


def _resolve_uid(con: sqlite3.Connection, bh: Borehole) -> str | None:
    """The uid this record had before: by its natural key (the same hole by definition, also while its old
    record under another id is still stored during a rebuild), else by its id unless another current record
    holds that uid."""
    row = con.execute("SELECT uid FROM borehole_aliases WHERE alias = ?", (natural_key(bh),)).fetchone()
    if row:
        return row["uid"]
    row = con.execute("SELECT uid FROM borehole_aliases WHERE alias = ?", (bh.borehole_id,)).fetchone()
    if row and not con.execute("SELECT 1 FROM boreholes WHERE uid = ? AND id != ?", (row["uid"], bh.borehole_id)).fetchone():
        return row["uid"]
    return None


def uid_for(con: sqlite3.Connection, bh: Borehole) -> str | None:
    row = con.execute("SELECT uid FROM boreholes WHERE id = ?", (bh.borehole_id,)).fetchone()
    return row["uid"] if row and row["uid"] else _resolve_uid(con, bh)


def _remember_aliases(con: sqlite3.Connection, bh: Borehole, uid: str) -> None:
    for alias in (bh.borehole_id, natural_key(bh)):
        con.execute("INSERT INTO borehole_aliases (alias, uid) VALUES (?, ?) ON CONFLICT (alias) DO UPDATE SET uid = excluded.uid",
                    (alias, uid))


def _version(con: sqlite3.Connection, uid: str, digest: str, data: str) -> int:
    """The id of this content as a record version; a new version only when the content changed."""
    last = con.execute("SELECT id, content_hash FROM record_versions WHERE uid = ? ORDER BY id DESC LIMIT 1",
                       (uid,)).fetchone()
    if last and last["content_hash"] == digest:
        return last["id"]
    cur = con.execute("INSERT INTO record_versions (uid, content_hash, data, created_at) VALUES (?, ?, ?, ?)",
                      (uid, digest, data, datetime.now(timezone.utc).isoformat()))
    return cur.lastrowid


def _write_projections(con: sqlite3.Connection, rid: int, bh: Borehole) -> None:
    from .scoring import EPSG_IN_FINDING, finding_code, finding_severity
    con.executemany("INSERT INTO layers VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(rid, i, l.top_m, l.bottom_m, l.primary.value, l.stratigraphic_code, l.geologic_age)
                     for i, l in enumerate(bh.layers)])
    con.executemany("INSERT INTO lab_values VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(rid, i, r.parameter.value, r.value, r.qualifier, r.top_m, r.bottom_m, r.sample_name)
                     for i, r in enumerate(bh.lab_results)])
    con.executemany("INSERT INTO findings VALUES (?, ?, ?, ?, ?, ?)",
                    [(rid, i, finding_code(t), finding_severity(t),
                      int(m.group(1)) if (m := EPSG_IN_FINDING.search(t)) else None, t)
                     for i, t in enumerate(bh.issues)])
    sources = {(s.file, s.page or 0, "log") for s in bh.sources}
    sources |= {(r.source.file, r.source.page or 0, "lab") for r in bh.lab_results if r.source}
    con.executemany("INSERT OR IGNORE INTO record_sources VALUES (?, ?, ?, ?)", [(rid, *src) for src in sources])


def _insert(con: sqlite3.Connection, bh: Borehole, digest: str, extracted: str | None = None, uid: str | None = None,
            version_id: int | None = None, review_revision: int = 0) -> None:
    loc = bh.location
    doc_id = bh.borehole_id.split(":", 1)[0]
    lithologies = sorted({layer.primary.value for layer in bh.layers})
    cur = con.execute(
        """INSERT INTO boreholes (id, name, doc_id, project, location_name, country, lat, lon, depth, elevation,
           investigation_type, confidence, loc_confidence, loc_method, score, review_status, n_layers, n_issues,
           lithologies, strip, content_hash, data, extracted, ground, reasons, uid, version_id, review_revision)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bh.borehole_id, bh.borehole_name, doc_id, bh.project_name, bh.location_name, loc.country,
            loc.lat, loc.lon, bh.depth_m or (bh.layers[-1].bottom_m if bh.layers else None), bh.surface_elevation_m,
            bh.investigation_type.value if bh.investigation_type else None, bh.confidence, loc.confidence,
            loc.method.value if loc.method else None, bh.score, bh.review_status.value, len(bh.layers),
            len(bh.issues), ",".join(lithologies),
            as_json([[layer.top_m, layer.bottom_m, layer.primary.value] for layer in bh.layers]),
            digest, bh.model_dump_json(), extracted, ground.summarize(bh).model_dump_json(),
            as_json(bh.score_reasons), uid, version_id, review_revision,
        ),
    )
    _write_projections(con, cur.lastrowid, bh)
    if loc.lat is not None and loc.lon is not None:
        con.execute("INSERT INTO boreholes_rtree VALUES (?, ?, ?, ?, ?)", (cur.lastrowid, loc.lat, loc.lat, loc.lon, loc.lon))
    _insert_fts(con, cur.lastrowid, bh)


def _insert_fts(con: sqlite3.Connection, rid: int, bh: Borehole) -> None:
    descriptions = " ".join(
        " ".join(filter(None, [layer.description_original, layer.description_en, layer.primary.value]))
        for layer in bh.layers
    )
    stratigraphy = " ".join(
        " ".join(filter(None, [layer.stratigraphic_name, layer.stratigraphic_code, layer.geologic_age]))
        for layer in bh.layers
    )
    con.execute(
        "INSERT INTO boreholes_fts (rowid, name, project, location, descriptions, stratigraphy) VALUES (?, ?, ?, ?, ?, ?)",
        (rid, bh.borehole_name, bh.project_name or "", bh.location_name or "", descriptions, stratigraphy),
    )


class StaleReview(Exception):
    """The record changed after the reviewer opened it."""


def record_review(con: sqlite3.Connection, borehole_id: str, decision: ReviewStatus, reviewer: str,
                  comment: str | None, expected_hash: str | None = None,
                  expected_revision: int | None = None) -> Borehole | None:
    """Store a human decision in place (no delete/re-insert) and append it to the review log. With
    `expected_hash`, the decision is refused if the content changed since the reviewer saw it; with
    `expected_revision`, if another decision was stored in the meantime."""
    with con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT data, content_hash, uid, version_id, review_revision FROM boreholes WHERE id = ?",
                          (borehole_id,)).fetchone()
        if row is None:
            return None
        if expected_hash and row["content_hash"] != expected_hash:
            raise StaleReview(borehole_id)
        if expected_revision is not None and row["review_revision"] != expected_revision:
            raise StaleReview(borehole_id)
        bh = Borehole.model_validate_json(row["data"])
        now = datetime.now(timezone.utc)
        bh.review_status = decision
        bh.reviewed_by = reviewer if decision in HUMAN else None
        bh.reviewed_at = now if decision in HUMAN else None
        bh.review_comment = comment if decision in HUMAN else None
        con.execute(
            "UPDATE boreholes SET review_status = ?, data = ?, review_revision = review_revision + 1 WHERE id = ?",
            (decision.value, bh.model_dump_json(), borehole_id),
        )
        con.execute(  # the version id records exactly which content the decision was about
            "INSERT INTO review_events (borehole_id, decision, reviewer, comment, at, content_hash, uid, version_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (borehole_id, decision.value, reviewer, comment, now.isoformat(), row["content_hash"], row["uid"],
             row["version_id"]),
        )
    return bh


def document_group(borehole_id: str) -> str:
    return borehole_id.split(":", 1)[0].split("__")[0]


def add_correction(con: sqlite3.Connection, scope: str, target: str, kind: str, payload: dict, reviewer: str,
                   comment: str | None) -> dict:
    at = datetime.now(timezone.utc).isoformat()
    uid = None
    if scope == "borehole":  # follows the hole even if its id changes later
        row = con.execute("SELECT uid FROM boreholes WHERE id = ?", (target,)).fetchone()
        uid = row["uid"] if row else None
    cur = con.execute(
        "INSERT INTO corrections (scope, target, kind, payload, reviewer, comment, at, uid) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (scope, target, kind, as_json(payload), reviewer, comment, at, uid),
    )
    return {"id": cur.lastrowid, "scope": scope, "target": target, "kind": kind, "payload": payload,
            "reviewer": reviewer, "comment": comment, "at": at, "uid": uid}


def corrections_for(con: sqlite3.Connection, borehole_id: str, uid: str | None = None) -> list[dict]:
    """Corrections that apply to a record, oldest first (document-wide ones included). With the record's
    uid, corrections made under an earlier id of the same hole are found too."""
    rows = con.execute(
        "SELECT * FROM corrections WHERE (scope = 'borehole' AND (target = ? OR uid = ?)) "
        "OR (scope = 'document' AND target = ?) ORDER BY id", (borehole_id, uid or "", document_group(borehole_id)),
    ).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def orphan_corrections(con: sqlite3.Connection) -> list[dict]:
    """Corrections of holes that no current record carries any more (renamed beyond recognition or gone)."""
    rows = con.execute(
        "SELECT * FROM corrections c WHERE scope = 'borehole' AND NOT EXISTS "
        "(SELECT 1 FROM boreholes b WHERE b.id = c.target OR (c.uid IS NOT NULL AND b.uid = c.uid)) ORDER BY id"
    ).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def group_borehole_ids(con: sqlite3.Connection, group: str) -> list[str]:
    return [r["id"] for r in con.execute(
        "SELECT id FROM boreholes WHERE doc_id = ? OR doc_id LIKE ? ESCAPE '\\' ORDER BY id",
        (group, group.replace("_", "\\_").replace("%", "\\%") + "\\_\\_%"),
    )]


def knowledge_item(con: sqlite3.Connection, item_id: int) -> dict:
    row = con.execute("SELECT * FROM knowledge WHERE id = ?", (item_id,)).fetchone()
    return {**dict(row), "payload": json.loads(row["payload"])} if row else {}


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


def active_knowledge(con: sqlite3.Connection) -> list[dict]:
    return [{**dict(r), "payload": json.loads(r["payload"])}
            for r in con.execute("SELECT * FROM knowledge WHERE active = 1 ORDER BY id")]


def review_log(con: sqlite3.Connection, borehole_id: str) -> list[dict]:
    """Decisions on this hole, also those made under an earlier id (via its uid), newest first."""
    row = con.execute("SELECT uid FROM boreholes WHERE id = ?", (borehole_id,)).fetchone()
    uid = row["uid"] if row else None
    return [dict(r) for r in con.execute(
        "SELECT decision, reviewer, comment, at, version_id FROM review_events WHERE borehole_id = ? OR "
        "(uid IS NOT NULL AND uid = ?) ORDER BY id DESC", (borehole_id, uid or ""),
    )]


def load_borehole(con: sqlite3.Connection, borehole_id: str) -> Borehole | None:
    row = con.execute("SELECT data FROM boreholes WHERE id = ?", (borehole_id,)).fetchone()
    return Borehole.model_validate_json(row["data"]) if row else None


def page_rows(con: sqlite3.Connection, doc_id: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM pages WHERE doc_id = ? ORDER BY page", (doc_id,))]


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)
