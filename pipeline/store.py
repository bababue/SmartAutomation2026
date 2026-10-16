"""SQLite store: one file, no server, stays local (data security), R-tree for map queries,
FTS5 for full-text search over names, projects, places and layer descriptions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from schema.borehole import Borehole

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
    confidence REAL, loc_confidence REAL, loc_method TEXT, n_layers INTEGER, n_issues INTEGER,
    lithologies TEXT, strip TEXT, reviewed INTEGER DEFAULT 0, data TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS boreholes_rtree USING rtree(rid, min_lat, max_lat, min_lon, max_lon);
CREATE VIRTUAL TABLE IF NOT EXISTS boreholes_fts USING fts5(
    id UNINDEXED, name, project, location, descriptions, stratigraphy, tokenize = 'unicode61 remove_diacritics 2'
);
"""


def connect(path: Path = config.DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def reset(con: sqlite3.Connection) -> None:
    for table in ("boreholes", "boreholes_rtree", "boreholes_fts"):
        con.execute(f"DELETE FROM {table}")
    con.commit()


def save_document(con: sqlite3.Connection, doc_id: str, path: str, kind: str, pages: list[dict]) -> None:
    con.execute("INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?)", (doc_id, path, kind, len(pages)))
    con.executemany(
        "INSERT OR REPLACE INTO pages VALUES (:doc_id, :page, :kind, :language, :logs, :image, :thumb, :width, :height)",
        pages,
    )
    con.commit()


def save_borehole(con: sqlite3.Connection, bh: Borehole) -> None:
    loc = bh.location
    row = con.execute("SELECT rid FROM boreholes WHERE id = ?", (bh.borehole_id,)).fetchone()
    if row:
        con.execute("DELETE FROM boreholes WHERE rid = ?", (row["rid"],))
        con.execute("DELETE FROM boreholes_rtree WHERE rid = ?", (row["rid"],))
        con.execute("DELETE FROM boreholes_fts WHERE id = ?", (bh.borehole_id,))
    doc_id = bh.borehole_id.split(":", 1)[0]
    lithologies = sorted({layer.primary.value for layer in bh.layers})
    cur = con.execute(
        """INSERT INTO boreholes (id, name, doc_id, project, location_name, country, lat, lon, depth, elevation,
           investigation_type, confidence, loc_confidence, loc_method, n_layers, n_issues, lithologies, strip, reviewed, data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bh.borehole_id, bh.borehole_name, doc_id, bh.project_name, bh.location_name, loc.country,
            loc.lat, loc.lon, bh.depth_m or (bh.layers[-1].bottom_m if bh.layers else None), bh.surface_elevation_m,
            bh.investigation_type.value if bh.investigation_type else None, bh.confidence, loc.confidence,
            loc.method.value if loc.method else None, len(bh.layers), len(bh.issues), ",".join(lithologies),
            as_json([[layer.top_m, layer.bottom_m, layer.primary.value] for layer in bh.layers]),
            int(bh.reviewed), bh.model_dump_json(),
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
    con.commit()


def load_borehole(con: sqlite3.Connection, borehole_id: str) -> Borehole | None:
    row = con.execute("SELECT data FROM boreholes WHERE id = ?", (borehole_id,)).fetchone()
    return Borehole.model_validate_json(row["data"]) if row else None


def page_rows(con: sqlite3.Connection, doc_id: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM pages WHERE doc_id = ? ORDER BY page", (doc_id,))]


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)
