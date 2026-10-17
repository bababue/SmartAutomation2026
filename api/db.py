"""The per-request database connection and the SQL fragments several routes share."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends

from pipeline import store


def db() -> Iterator[sqlite3.Connection]:
    """One connection per request: no shared transactions between concurrent requests."""
    con = store.connect()
    try:
        yield con
    finally:
        con.close()


DB = Annotated[sqlite3.Connection, Depends(db)]

SQLITE_MAX_INT = 2**63 - 1  # larger ints overflow the parameter binding

SOURCE_FILE = "(SELECT path FROM documents d WHERE d.doc_id = boreholes.doc_id) AS source_file"

STATUS_GROUPS = {
    "released": ("auto_approved", "approved"),
    "pending": ("pending_review",),
    "rejected": ("rejected",),
}
RELEASED = f"review_status IN ({', '.join(repr(s) for s in STATUS_GROUPS['released'])})"
