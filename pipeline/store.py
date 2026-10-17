"""SQLite store: one file, no server, stays local (data security), R-tree for map queries,
FTS5 for full-text search over names, projects, places and layer descriptions, and a review
log for the human-in-the-loop decisions."""

from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from schema.borehole import Borehole, ReviewStatus

from . import config, ground, scoring
from .georef import haversine_km

REVIEW_STATUSES = ", ".join(f"'{s.value}'" for s in ReviewStatus)

# A hole's document group (store.document_group): the document, or the folder of single images.
GROUP_SQL = "CASE WHEN instr(doc_id, '__') > 0 THEN substr(doc_id, 1, instr(doc_id, '__') - 1) ELSE doc_id END"

BOREHOLES = f"""
CREATE TABLE IF NOT EXISTS boreholes (
    rid INTEGER PRIMARY KEY,
    id TEXT UNIQUE, uid TEXT, name TEXT, doc_id TEXT, project TEXT, location_name TEXT, country TEXT,
    lat REAL, lon REAL, depth REAL, elevation REAL, investigation_type TEXT,
    confidence REAL, loc_confidence REAL, loc_method TEXT, score REAL, reasons TEXT,
    review_status TEXT NOT NULL CHECK (review_status IN ({REVIEW_STATUSES})),
    review_revision INTEGER NOT NULL DEFAULT 0,
    n_layers INTEGER, strip TEXT, content_hash TEXT, version_id INTEGER, data TEXT,
    extracted TEXT,  -- model output before reviewer corrections (layers, position), for evaluation
    ground TEXT,  -- derived ground model values (pipeline/ground.py)
    grp TEXT GENERATED ALWAYS AS ({GROUP_SQL}) VIRTUAL
)"""

SCHEMA = BOREHOLES + """;
CREATE UNIQUE INDEX IF NOT EXISTS boreholes_uid ON boreholes (uid);
CREATE INDEX IF NOT EXISTS boreholes_doc ON boreholes (doc_id);
CREATE INDEX IF NOT EXISTS boreholes_grp ON boreholes (grp);
CREATE INDEX IF NOT EXISTS boreholes_queue ON boreholes (review_status, score, name, id);
CREATE VIRTUAL TABLE IF NOT EXISTS boreholes_rtree USING rtree(rid, min_lat, max_lat, min_lon, max_lon);
-- Full-text rows share the borehole's rid as rowid, so updates and deletes are point operations.
CREATE VIRTUAL TABLE IF NOT EXISTS boreholes_fts USING fts5(
    name, project, location, descriptions, stratigraphy, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS documents (doc_id TEXT PRIMARY KEY, path TEXT, pages INTEGER);
-- kind: the page's classification by the last run that classified it.
CREATE TABLE IF NOT EXISTS pages (doc_id TEXT, page INTEGER, kind TEXT, image TEXT, PRIMARY KEY (doc_id, page));
-- Projections of the current records for queries, rebuilt from 'data' at any time.
CREATE TABLE IF NOT EXISTS layers (
    rid INTEGER NOT NULL, seq INTEGER NOT NULL, top_m REAL, bottom_m REAL, primary_code TEXT, strat_code TEXT,
    geologic_age TEXT, PRIMARY KEY (rid, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS layers_lith ON layers (primary_code, top_m, bottom_m);
CREATE TABLE IF NOT EXISTS lab_values (
    rid INTEGER NOT NULL, seq INTEGER NOT NULL, parameter TEXT, value REAL, qualifier TEXT, top_m REAL,
    bottom_m REAL, sample TEXT, value_upper REAL, PRIMARY KEY (rid, seq)
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
-- The content a review decision was about (review_events.version_id) and each hole's current content.
CREATE TABLE IF NOT EXISTS record_versions (
    id INTEGER PRIMARY KEY, uid TEXT NOT NULL, content_hash TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS versions_uid ON record_versions (uid, id);
CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY, borehole_id TEXT, decision TEXT, reviewer TEXT, comment TEXT, at TEXT,
    content_hash TEXT, uid TEXT, version_id INTEGER
);
CREATE INDEX IF NOT EXISTS review_events_borehole ON review_events (borehole_id, id);
CREATE INDEX IF NOT EXISTS review_events_uid ON review_events (uid, id);
-- Reviewer corrections, re-applied after every run. scope 'borehole': target is a borehole id and uid the hole.
-- scope 'document': target is a document group (doc id before '__'), e.g. one CRS for all holes.
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY, scope TEXT, target TEXT, kind TEXT, payload TEXT, reviewer TEXT, comment TEXT, at TEXT,
    uid TEXT
);
CREATE INDEX IF NOT EXISTS corrections_target ON corrections (scope, target, id);
CREATE INDEX IF NOT EXISTS corrections_uid ON corrections (uid, id);
-- What reviewers taught the system, beyond single records. scope_type: document | project | region | global.
-- source: derived (from corrections) | review (entered from the documents) | external (phone, e-mail, site).
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY, scope_type TEXT, scope_value TEXT, kind TEXT, payload TEXT, source TEXT, note TEXT,
    created_by TEXT, created_at TEXT, confirmations INTEGER DEFAULT 1, active INTEGER DEFAULT 1, evidence TEXT
);
CREATE INDEX IF NOT EXISTS knowledge_scope ON knowledge (scope_type, scope_value, kind);
CREATE TABLE IF NOT EXISTS upload_jobs (
    id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL
);
-- Every model call a run made (cache hits make none): what went where, as sizes, never the content.
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, job_id TEXT, doc_id TEXT, page INTEGER, stage TEXT, model TEXT,
    endpoint TEXT, image_bytes INTEGER, text_chars INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    cost_usd REAL, seconds REAL, outcome TEXT
);
CREATE INDEX IF NOT EXISTS llm_calls_at ON llm_calls (at);
CREATE INDEX IF NOT EXISTS llm_calls_job ON llm_calls (job_id);
"""

SCHEMA_VERSION = 5
# Oldest file migrate() can bring up to date; older ones predate stable uids and the review log's versions.
OLDEST_VERSION = 3

HUMAN = (ReviewStatus.approved, ReviewStatus.rejected)


def _statements(script: str) -> list[str]:
    """executescript would commit the open migration transaction."""
    return [s for s in script.split(";") if s.strip()]


def _migrate_v4(con: sqlite3.Connection) -> None:
    """Upper bounds of lab value ranges."""
    con.execute("ALTER TABLE lab_values ADD COLUMN value_upper REAL")
    for row in con.execute("SELECT rid, data FROM boreholes").fetchall():
        try:
            bh = Borehole.model_validate_json(row["data"])
        except ValidationError:
            continue
        con.execute("DELETE FROM lab_values WHERE rid = ?", (row["rid"],))
        _write_lab_values(con, row["rid"], bh)


def _migrate_v5(con: sqlite3.Connection) -> None:
    """Review status check and document group column (a table rebuild, rids kept for the R-tree and full
    text), unread columns dropped, record versions that no decision or current record points to pruned."""
    con.execute("ALTER TABLE boreholes RENAME TO boreholes_v4")
    con.execute(BOREHOLES)
    columns = ", ".join(r["name"] for r in con.execute("PRAGMA table_info(boreholes)"))
    con.execute(f"INSERT INTO boreholes ({columns}) SELECT {columns} FROM boreholes_v4")
    con.execute("DROP TABLE boreholes_v4")
    con.execute("ALTER TABLE documents DROP COLUMN kind")
    for column in ("language", "logs", "thumb", "width", "height"):
        con.execute(f"ALTER TABLE pages DROP COLUMN {column}")
    con.execute("""DELETE FROM record_versions
                   WHERE id NOT IN (SELECT version_id FROM review_events WHERE version_id IS NOT NULL)
                   AND id NOT IN (SELECT version_id FROM boreholes WHERE version_id IS NOT NULL)""")


MIGRATIONS = {4: _migrate_v4, 5: _migrate_v5}


def connect(path: Path | None = None) -> sqlite3.Connection:
    """A connection to a database that `migrate` has brought up to date."""
    con = sqlite3.connect(path or config.DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def migrate(path: Path | None = None) -> None:
    """Create or upgrade the database; once per process, before the first `connect`."""
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return
        con.execute("PRAGMA journal_mode=WAL")  # persistent: readers (API) never block the pipeline writer
        with con:
            con.execute("BEGIN IMMEDIATE")  # another process may be migrating the same file
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            fresh = not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'boreholes'").fetchone()
            if not fresh and not OLDEST_VERSION <= version < SCHEMA_VERSION:
                raise RuntimeError(f"{path} has schema version {version}; this code reads versions "
                                   f"{OLDEST_VERSION} to {SCHEMA_VERSION}")
            if not fresh:
                for target in range(version + 1, SCHEMA_VERSION + 1):
                    MIGRATIONS[target](con)
            for statement in _statements(SCHEMA):
                con.execute(statement)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if not fresh:
            try:
                con.execute("VACUUM")  # returns the pruned versions' space to the file system
            except sqlite3.OperationalError:
                pass  # busy: the free pages are reused by later writes anyway
    finally:
        con.close()


def reset(con: sqlite3.Connection) -> None:
    """Remove all records; the review log stays with the versions it points to, it holds human decisions."""
    for table in ("boreholes", "boreholes_rtree", "boreholes_fts", "layers", "lab_values",
                  "findings", "record_sources", "borehole_aliases"):
        con.execute(f"DELETE FROM {table}")
    con.execute("DELETE FROM record_versions WHERE id NOT IN "
                "(SELECT version_id FROM review_events WHERE version_id IS NOT NULL)")
    con.commit()


def save_document(con: sqlite3.Connection, doc_id: str, path: str, images: list[tuple[int, str]]) -> None:
    """A document and its rendered pages as (page, image path); a page keeps its classification."""
    con.execute("INSERT OR REPLACE INTO documents (doc_id, path, pages) VALUES (?, ?, ?)", (doc_id, path, len(images)))
    con.execute(f"DELETE FROM pages WHERE doc_id = ? AND page NOT IN ({', '.join('?' * len(images))})",
                (doc_id, *(page for page, _ in images)))  # a re-render can have fewer pages
    con.executemany("INSERT INTO pages (doc_id, page, image) VALUES (?, ?, ?) "
                    "ON CONFLICT (doc_id, page) DO UPDATE SET image = excluded.image",
                    [(doc_id, page, image) for page, image in images])
    con.commit()


def delete_documents(con: sqlite3.Connection, doc_ids: list[str]) -> None:
    """A document that was registered but never processed (a cancelled upload); it has no records."""
    for doc_id in doc_ids:
        con.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
        con.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
    con.commit()


LLM_CALL_FIELDS = ("at", "job_id", "doc_id", "page", "stage", "model", "endpoint", "image_bytes", "text_chars",
                   "input_tokens", "output_tokens", "cost_usd", "seconds", "outcome")


def record_llm_call(con: sqlite3.Connection, call: dict) -> None:
    con.execute(f"INSERT INTO llm_calls ({', '.join(LLM_CALL_FIELDS)}) VALUES ({', '.join('?' * len(LLM_CALL_FIELDS))})",
                [call.get(f) for f in LLM_CALL_FIELDS])
    con.commit()


def llm_calls_today(con: sqlite3.Connection) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return con.execute("SELECT COUNT(*) FROM llm_calls WHERE at >= ?", (today,)).fetchone()[0]


def _reviewed_content(bh: Borehole) -> dict:
    """Everything a reviewer judged and the export publishes (not review fields)."""
    return bh.model_dump(mode="json", exclude={"borehole_id", "confidence", "issues", "score", "score_reasons",
                                               "review_status", "reviewed_by", "reviewed_at", "review_comment"})


def content_hash(bh: Borehole) -> str:
    """An approval stays only while exactly this content is stored."""
    return hashlib.sha256(json.dumps(_reviewed_content(bh), sort_keys=True).encode()).hexdigest()


def _delete(con: sqlite3.Connection, borehole_id: str) -> dict | None:
    row = con.execute("SELECT rid, content_hash, data, extracted, uid, version_id, review_revision FROM boreholes "
                      "WHERE id = ?", (borehole_id,)).fetchone()
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
    uncorrected model output (see `evaluate.extracted_summary`); without it the previous row's is kept."""
    digest = content_hash(bh)
    previous = _delete(con, bh.borehole_id) or previous
    extracted_json = as_json(extracted) if extracted is not None else (previous or {}).get("extracted")
    revision = (previous or {}).get("review_revision") or 0
    uid = (previous or {}).get("uid") or _resolve_uid(con, bh) or uuid.uuid4().hex
    if con.execute("SELECT 1 FROM boreholes WHERE uid = ? AND id != ?", (uid, bh.borehole_id)).fetchone():
        uid = uuid.uuid4().hex  # another current record is that hole: never share an identity
    decision = None
    if previous and previous.get("data"):
        old = Borehole.model_validate_json(previous["data"])
        decision = (old, content_hash(old))
    if not decision or decision[0].review_status not in HUMAN:
        # The review log holds the decision: a row reopened because its data changed still had one, and the
        # next save must reopen it again rather than score it as never reviewed. Stored under another id
        # before, the decision follows the uid, as corrections do.
        decision = _logged_decision(con, uid) or decision
    if decision and _blocking(decision[0]) != _blocking(bh):
        revision += 1  # the content hash leaves findings out: this stops a review screen opened before them
    if decision and decision[0].review_status in HUMAN:
        _carry_decision(bh, *decision, digest)
    version_id = _version(con, uid, digest, bh.model_dump_json())
    _insert(con, bh, digest, extracted_json, uid, version_id, revision)
    _remember_aliases(con, bh, uid)
    if previous and previous.get("version_id") not in (None, version_id):
        _drop_unreviewed_version(con, previous["version_id"])
    if commit:
        con.commit()


def _logged_decision(con: sqlite3.Connection, uid: str) -> tuple[Borehole, str] | None:
    """The latest review of this hole as (record as reviewed, its content hash). The hash is recomputed from the
    stored version, so a field dropped from the model is not a data change. Without a stored version the
    reviewer counts as having seen no findings."""
    event = con.execute("SELECT decision, reviewer, comment, at, content_hash, version_id FROM review_events "
                        "WHERE uid = ? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if not event:
        return None
    version = con.execute("SELECT data FROM record_versions WHERE id = ?", (event["version_id"],)).fetchone()
    old = Borehole.model_validate_json(version["data"]) if version else Borehole.model_construct(issues=[])
    old.review_status, old.reviewed_by = ReviewStatus(event["decision"]), event["reviewer"]
    old.reviewed_at = datetime.fromisoformat(event["at"]) if event["at"] else None
    old.review_comment = event["comment"]
    return old, content_hash(old) if version else event["content_hash"]


def _carry_decision(bh: Borehole, old: Borehole, old_digest: str, digest: str) -> None:
    """Carry the human decision on `old` over to `bh`."""
    new_blocking = _new_blocking(old, bh) if old.review_status == ReviewStatus.approved else []
    if old_digest == digest and not new_blocking:
        bh.review_status, bh.reviewed_by = old.review_status, old.reviewed_by
        bh.reviewed_at, bh.review_comment = old.reviewed_at, old.review_comment
    elif old_digest == digest:
        bh.review_status = ReviewStatus.pending_review
        bh.score_reasons = [f"New blocking finding since the approval by {old.reviewed_by or 'a reviewer'}: "
                            + "; ".join(new_blocking)] + bh.score_reasons
    else:
        bh.review_status = ReviewStatus.pending_review
        bh.score_reasons = [f"Data changed since the {old.review_status.value} decision by "
                            f"{old.reviewed_by or 'a reviewer'}"] + bh.score_reasons


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


def _blocking(bh: Borehole) -> set[str]:
    return {i for i in bh.issues if scoring.is_blocking(i)}


def natural_key(bh: Borehole) -> str:
    """The hole by what it is: document group, type and number ('nk:report|borehole|12')."""
    from .assemble import hole_key  # assembly rules define what counts as the same hole
    kind, number = hole_key(bh.borehole_name, bh.investigation_type.value if bh.investigation_type else None)
    return f"nk:{document_group(bh.borehole_id)}|{kind or ''}|{number}"


def _resolve_uid(con: sqlite3.Connection, bh: Borehole) -> str | None:
    """The uid this record had before: by its natural key, else by its id unless another current record holds
    that uid. Two documents of a group can each have a hole of one natural key, so it names a hole of this
    record's own document only, and one that no current record of another document holds. A write replaces
    whole documents: the hole's old record under another id may still be stored then."""
    doc = doc_of(bh.borehole_id)
    row = con.execute("SELECT uid FROM borehole_aliases WHERE alias = ?", (natural_key(bh),)).fetchone()
    if row and doc in _documents(con, row["uid"]) and not con.execute(
            "SELECT 1 FROM boreholes WHERE uid = ? AND doc_id != ?", (row["uid"], doc)).fetchone():
        return row["uid"]
    row = con.execute("SELECT uid FROM borehole_aliases WHERE alias = ?", (bh.borehole_id,)).fetchone()
    if row and not con.execute("SELECT 1 FROM boreholes WHERE uid = ? AND id != ?", (row["uid"], bh.borehole_id)).fetchone():
        return row["uid"]
    return None


def _documents(con: sqlite3.Connection, uid: str) -> set[str]:
    """The documents this hole had a record in, under any id."""
    return {doc_of(r["alias"]) for r in con.execute("SELECT alias FROM borehole_aliases WHERE uid = ?", (uid,))
            if not r["alias"].startswith("nk:")}


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


def _drop_unreviewed_version(con: sqlite3.Connection, version_id: int) -> None:
    """Forget a replaced version unless a decision was about it or a current record still is it."""
    con.execute("DELETE FROM record_versions WHERE id = :v AND NOT EXISTS (SELECT 1 FROM review_events WHERE "
                "version_id = :v) AND NOT EXISTS (SELECT 1 FROM boreholes b WHERE b.uid = record_versions.uid "
                "AND b.version_id = :v)", {"v": version_id})


def _write_projections(con: sqlite3.Connection, rid: int, bh: Borehole) -> None:
    from .scoring import EPSG_IN_FINDING, finding_code, finding_severity
    con.executemany("INSERT INTO layers VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(rid, i, l.top_m, l.bottom_m, l.primary.value, l.stratigraphic_code, l.geologic_age)
                     for i, l in enumerate(bh.layers)])
    _write_lab_values(con, rid, bh)
    con.executemany("INSERT INTO findings VALUES (?, ?, ?, ?, ?, ?)",
                    [(rid, i, finding_code(t), finding_severity(t),
                      int(m.group(1)) if (m := EPSG_IN_FINDING.search(t)) else None, t)
                     for i, t in enumerate(bh.issues)])
    sources = {(s.file, s.page or 0, "log") for s in bh.sources}
    sources |= {(r.source.file, r.source.page or 0, "lab") for r in bh.lab_results if r.source}
    con.executemany("INSERT OR IGNORE INTO record_sources VALUES (?, ?, ?, ?)", [(rid, *src) for src in sources])


def _write_lab_values(con: sqlite3.Connection, rid: int, bh: Borehole) -> None:
    con.executemany("INSERT INTO lab_values (rid, seq, parameter, value, qualifier, top_m, bottom_m, sample, value_upper) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(rid, i, r.parameter.value, r.value, r.qualifier, r.top_m, r.bottom_m, r.sample_name, r.value_upper)
                     for i, r in enumerate(bh.lab_results)])


def _insert(con: sqlite3.Connection, bh: Borehole, digest: str, extracted: str | None, uid: str, version_id: int,
            review_revision: int) -> None:
    loc = bh.location
    doc_id = doc_of(bh.borehole_id)
    cur = con.execute(
        """INSERT INTO boreholes (id, name, doc_id, project, location_name, country, lat, lon, depth, elevation,
           investigation_type, confidence, loc_confidence, loc_method, score, review_status, n_layers,
           strip, content_hash, data, extracted, ground, reasons, uid, version_id, review_revision)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bh.borehole_id, bh.borehole_name, doc_id, bh.project_name, bh.location_name, loc.country,
            loc.lat, loc.lon, bh.depth_m or (bh.layers[-1].bottom_m if bh.layers else None), bh.surface_elevation_m,
            bh.investigation_type.value if bh.investigation_type else None, bh.confidence, loc.confidence,
            loc.method.value if loc.method else None, bh.score, bh.review_status.value, len(bh.layers),
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
        version_id = row["version_id"]
        # The content hash leaves findings out: a blocking finding added since the version was stored gets a
        # version of its own, so the decision names what the reviewer saw.
        version = con.execute("SELECT data FROM record_versions WHERE id = ?", (version_id,)).fetchone()
        if row["uid"] and (version is None or _blocking(Borehole.model_validate_json(version["data"])) != _blocking(bh)):
            version_id = con.execute(
                "INSERT INTO record_versions (uid, content_hash, data, created_at) VALUES (?, ?, ?, ?)",
                (row["uid"], row["content_hash"], row["data"], datetime.now(timezone.utc).isoformat())).lastrowid
        now = datetime.now(timezone.utc)
        bh.review_status = decision
        bh.reviewed_by = reviewer if decision in HUMAN else None
        bh.reviewed_at = now if decision in HUMAN else None
        bh.review_comment = comment if decision in HUMAN else None
        con.execute(
            "UPDATE boreholes SET review_status = ?, data = ?, version_id = ?, review_revision = review_revision + 1 "
            "WHERE id = ?",
            (decision.value, bh.model_dump_json(), version_id, borehole_id),
        )
        con.execute(  # the version id records exactly which content the decision was about
            "INSERT INTO review_events (borehole_id, decision, reviewer, comment, at, content_hash, uid, version_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (borehole_id, decision.value, reviewer, comment, now.isoformat(), row["content_hash"], row["uid"],
             version_id),
        )
    return bh


def doc_of(borehole_id: str) -> str:
    return borehole_id.split(":", 1)[0]


def document_group(borehole_id: str) -> str:
    return doc_of(borehole_id).split("__")[0]


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
        "SELECT * FROM corrections WHERE scope = 'borehole' AND target = ? "
        "UNION SELECT * FROM corrections WHERE scope = 'borehole' AND uid = ? "
        "UNION SELECT * FROM corrections WHERE scope = 'document' AND target = ? ORDER BY id",
        (borehole_id, uid or "", document_group(borehole_id)),
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
    return [r["id"] for r in con.execute("SELECT id FROM boreholes WHERE grp = ? ORDER BY id", (group,))]


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


MAX_CHANGES = 20


def changes_since_review(con: sqlite3.Connection, uid: str | None, bh: Borehole) -> dict | None:
    """The latest human decision on this hole with what changed since in `bh`, so a reviewer re-checks only
    that; None without such a decision or without a change."""
    event = con.execute("SELECT decision, reviewer, at, content_hash, version_id FROM review_events WHERE uid = ? "
                        "AND decision IN (?, ?) ORDER BY id DESC LIMIT 1", (uid, *(s.value for s in HUMAN))).fetchone()
    if not event:
        return None
    version = con.execute("SELECT data FROM record_versions WHERE id = ?", (event["version_id"],)).fetchone()
    try:
        old = Borehole.model_validate_json(version["data"]) if version else None
    except ValidationError:
        old = None
    if old is None:
        if event["content_hash"] == content_hash(bh):
            return None
        changes = [f"The {event['decision']} version is not stored, so the changes cannot be listed"]
    else:
        changes = _changes(_reviewed_content(old), _reviewed_content(bh))
        if not changes:
            return None
    if len(changes) > MAX_CHANGES:
        changes = changes[:MAX_CHANGES - 1] + [f"… and {len(changes) - MAX_CHANGES + 1} more changes"]
    return {"decision": event["decision"], "reviewer": event["reviewer"], "at": event["at"], "changes": changes}


HEADINGS = {"depth_m": "Final depth", "surface_elevation_m": "Ground level", "length_m": "Length along the hole"}
LABELS = {"primary": "lithology", "secondary": "secondary components", "description_original": "description",
          "description_en": "translation", "coords_raw": "coordinates as printed", "crs_raw": "coordinate system as printed",
          "crs_epsg": "coordinate system (EPSG)", "method": "position source", "confidence": "position confidence",
          "n_value": "N"}
UNITS = {"_m": " m", "_pct": " %", "_deg": "°"}


def _key(*values) -> tuple:
    return tuple(round(v, 2) if isinstance(v, float) else v for v in values)


def _range(d: dict) -> str:
    return f"{_num(d['top_m'])}–{_num(d['bottom_m'])} m"


# Lists of entries: label, the key that matches an entry before and after, a summary of an added or removed one.
# Layers match by lithology: a moved boundary changes the depths of two layers, an inserted layer shifts none.
ITEMS = {
    "layers": ("Layer", lambda d: d["primary"], lambda d: f"{_range(d)}, {d['primary']}"),
    "core_runs": ("Core run", lambda d: _key(d["top_m"], d["bottom_m"]), _range),
    "samples": ("Sample", lambda d: _key(d["name"], d["top_m"]),
                lambda d: f"{d['name'] or 'unnamed'} at {_num(d['top_m'])} m"),
    "spt": ("SPT", lambda d: _key(d["depth_m"]), lambda d: f"{d['raw'] or d['n_value']} at {_num(d['depth_m'])} m"),
    "groundwater": ("Groundwater", lambda d: _key(d["depth_m"], d["kind"]),
                    lambda d: "dry" if d["dry"] else f"{_num(d['depth_m'])} m, {d['kind']}"),
    "lab_results": ("Lab value", lambda d: _key(d["parameter"], d["top_m"], d["sample_name"]),
                    lambda d: f"{d['parameter']} {_num(d['value'])}"
                    + (f" at {_num(d['top_m'])} m" if d["top_m"] is not None else "")),
}
# Where on the page a value was read: a re-extraction moves these boxes without changing what was read.
SOURCE_FIELDS = {"source", "sources"}


def _changes(old: dict, new: dict) -> list[str]:
    lines = []
    for field, after in new.items():
        before = old.get(field)
        if before == after or field in SOURCE_FIELDS:
            continue
        if field == "location":
            lines += _location_changes(before or {}, after)
        elif field in ITEMS:
            lines += _item_changes(field, before or [], after)
        else:
            lines.append(_line(_heading(field), field, before, after))
    return lines or (["Only the references to the source pages changed"] if old != new else [])


def _location_changes(old: dict, new: dict) -> list[str]:
    lines = []
    if (old.get("lat"), old.get("lon")) != (new["lat"], new["lon"]):
        before, after = _position(old), _position(new)
        moved = ""
        if before and after:
            metres = haversine_km(old["lat"], old["lon"], new["lat"], new["lon"]) * 1000
            moved = f" (moved {metres / 1000:.1f} km)" if metres >= 1000 else f" (moved {metres:.0f} m)"
        lines.append(f"Position: {before or 'none'} → {after or 'none'}{moved}")
    return lines + [_line(_heading(field), field, old.get(field), after)
                    for field, after in new.items()
                    if field not in ("lat", "lon", *SOURCE_FIELDS) and old.get(field) != after]


def _position(location: dict) -> str | None:
    if location.get("lat") is None or location.get("lon") is None:
        return None
    return f"{location['lat']:.5f}, {location['lon']:.5f}"


def _item_changes(field: str, old: list[dict], new: list[dict]) -> list[str]:
    label, match, summary = ITEMS[field]
    lines = []
    matcher = difflib.SequenceMatcher(None, [match(d) for d in old], [match(d) for d in new], autojunk=False)
    for _, i1, i2, j1, j2 in matcher.get_opcodes():
        pairs = list(zip(range(i1, i2), range(j1, j2)))  # a replaced block is paired in order
        for i, j in pairs:
            lines += [_line(f"{label} {j + 1}: {_label(f)}", f, old[i].get(f), after)
                      for f, after in new[j].items() if f not in SOURCE_FIELDS and old[i].get(f) != after]
        lines += [f"{label} {j + 1} added: {summary(new[j])}" for j in range(j1 + len(pairs), j2)]
        lines += [f"{label} removed: {summary(old[i])}" for i in range(i1 + len(pairs), i2)]
    return lines


def _label(field: str) -> str:
    if field in LABELS:
        return LABELS[field]
    for suffix in UNITS:
        field = field.removesuffix(suffix)
    return field.replace("_", " ")


def _heading(field: str) -> str:
    label = HEADINGS.get(field) or _label(field)
    return label[:1].upper() + label[1:] + ":"


def _line(label: str, field: str, before, after) -> str:
    old, new = _text(before), _text(after)
    if old == new:  # differs beyond what the shortened text shows
        return f"{label.rstrip(':')} changed"
    unit = next((u for suffix, u in UNITS.items() if field.endswith(suffix)), "")
    return f"{label} {old} → {new}{unit}"


def _num(value: float | None) -> str:
    return "—" if value is None else format(value, ".10g")


def _text(value) -> str:
    if value is None or value == [] or value == {}:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return _num(value)
    if isinstance(value, dict) and "file" in value:  # a source reference
        text = f"{value['file']} p. {value['page']}" if value.get("page") else value["file"]
    elif isinstance(value, dict):
        text = " ".join(_text(v) for v in value.values() if v is not None)
    elif isinstance(value, list):
        text = ", ".join(_text(v) for v in value)
    else:
        text = str(value)
    text = text if len(text) <= 40 else text[:39] + "…"
    return f"“{text}”" if " " in text and not isinstance(value, (dict, list)) else text


def load_borehole(con: sqlite3.Connection, borehole_id: str) -> Borehole | None:
    row = con.execute("SELECT data FROM boreholes WHERE id = ?", (borehole_id,)).fetchone()
    return Borehole.model_validate_json(row["data"]) if row else None


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)
