"""Store v3: projections, stable uids across id changes, record versions, review revision (synthetic data)."""

import sqlite3
from pathlib import Path

import pytest

from pipeline import config, corrections, store
from pipeline.scoring import score
from schema.borehole import (Borehole, GeorefMethod, InvestigationType, LabResult, Layer, Location, ReviewStatus,
                             SourceRef)


def record(bid="rep:BH01", name="BH-01", top_clay=4.5, ucs=42.0):
    src = SourceRef(file="rep.pdf", page=3)
    bh = Borehole(
        borehole_id=bid, borehole_name=name, investigation_type=InvestigationType.borehole, depth_m=9,
        layers=[Layer(top_m=0, bottom_m=top_clay, description_original="Sand", primary="sand", source=src),
                Layer(top_m=top_clay, bottom_m=9, description_original="Clay", primary="clay", source=src)],
        lab_results=[LabResult(parameter="ucs", value=ucs, value_raw=f"{ucs} MPa", top_m=6,
                               source=SourceRef(file="rep.pdf", page=12))],
        location=Location(lat=48.47, lon=7.94, method=GeorefMethod.stated_coordinates, confidence=0.95),
        sources=[src],
    )
    bh.issues = bh.validate_log() + ["coordinate system inferred, not stated: EPSG:32632"]
    score(bh, {}, 0)
    return bh


def test_projections_are_queryable(con):
    store.save_borehole(con, record())
    rid = con.execute("SELECT rid FROM boreholes").fetchone()[0]
    assert con.execute("SELECT primary_code FROM layers WHERE rid = ? AND top_m < 8 AND bottom_m > 6", (rid,)).fetchone()[0] == "clay"
    assert con.execute("SELECT value FROM lab_values WHERE parameter = 'ucs'").fetchone()[0] == 42.0
    assert tuple(con.execute("SELECT code, severity, epsg FROM findings").fetchone()) == ("crs_inferred", "warning", 32632)
    assert {tuple(r) for r in con.execute("SELECT file, page, role FROM record_sources")} == {("rep.pdf", 3, "log"), ("rep.pdf", 12, "lab")}
    store.save_borehole(con, record())  # replaced: no duplicate projection rows
    assert con.execute("SELECT COUNT(*) FROM layers").fetchone()[0] == 2


def test_id_change_keeps_uid_corrections_and_review_history(con):
    store.save_borehole(con, record())
    uid = con.execute("SELECT uid FROM boreholes").fetchone()[0]
    store.add_correction(con, "borehole", "rep:BH01", "layer",
                         {"orig_top": 0, "orig_bottom": 4.5, "primary": "fine_sand"}, "Ana", None)
    con.commit()
    store.record_review(con, "rep:BH01", ReviewStatus.rejected, "Ana", "wrong sheet")

    # A later run names the same hole differently (e.g. a collision rule changed the id).
    renamed = record(bid="rep:BH1", name="BH 1")
    assert store.uid_for(con, renamed) == uid  # found by its natural key
    fixes = store.corrections_for(con, renamed.borehole_id, store.uid_for(con, renamed))
    assert [c["kind"] for c in fixes] == ["layer"]
    assert corrections.apply(renamed, fixes) == [True]
    store.delete_document_boreholes(con, "rep")
    store.save_borehole(con, renamed)
    assert con.execute("SELECT uid FROM boreholes WHERE id = 'rep:BH1'").fetchone()[0] == uid
    assert [e["decision"] for e in store.review_log(con, "rep:BH1")] == ["rejected"]
    assert store.orphan_corrections(con) == []
    bh = store.load_borehole(con, "rep:BH1")  # the correction changed what the reviewer rejected
    assert bh.review_status == ReviewStatus.pending_review
    assert bh.score_factors[0].text == "Data changed since the rejected decision by Ana"


@pytest.mark.parametrize("decision", [ReviewStatus.approved, ReviewStatus.rejected])
def test_id_change_keeps_the_decision_on_unchanged_content(con, decision):
    store.save_borehole(con, record())
    store.record_review(con, "rep:BH01", decision, "Ana", "checked")
    store.delete_document_boreholes(con, "rep")
    store.save_borehole(con, record(bid="rep:BH1"))
    bh = store.load_borehole(con, "rep:BH1")
    assert (bh.review_status, bh.reviewed_by, bh.review_comment) == (decision, "Ana", "checked")
    assert bh.reviewed_at is not None


def test_changed_data_keeps_an_approval_open_on_every_later_save(con):
    store.save_borehole(con, record())
    store.record_review(con, "rep:BH01", ReviewStatus.approved, "Ana", None)
    for _ in range(2):  # the second save starts from a row that is already reopened
        store.save_borehole(con, record(ucs=55.0))
        bh = store.load_borehole(con, "rep:BH01")
        assert bh.review_status == ReviewStatus.pending_review
        assert bh.score_factors[0].text == "Data changed since the approved decision by Ana"
    store.save_borehole(con, record())  # back to what Ana approved
    assert store.load_borehole(con, "rep:BH01").review_status == ReviewStatus.approved


def test_id_change_keeps_a_reopened_record_open(con):
    store.save_borehole(con, record())
    store.record_review(con, "rep:BH01", ReviewStatus.rejected, "Ana", None)
    store.record_review(con, "rep:BH01", ReviewStatus.pending_review, "Ben", None)
    store.delete_document_boreholes(con, "rep")
    store.save_borehole(con, record(bid="rep:BH1"))
    assert store.load_borehole(con, "rep:BH1").review_status != ReviewStatus.rejected


def test_versions_only_for_changed_content_and_review_revision(con):
    store.save_borehole(con, record())
    store.save_borehole(con, record())  # same content: same version
    first = con.execute("SELECT version_id FROM boreholes").fetchone()[0]
    store.save_borehole(con, record(ucs=55.0))  # changed: new version, the unreviewed one is dropped
    version = con.execute("SELECT version_id FROM boreholes").fetchone()[0]
    assert version != first
    assert [r[0] for r in con.execute("SELECT id FROM record_versions")] == [version]
    store.record_review(con, "rep:BH01", ReviewStatus.approved, "Ana", None, expected_revision=0)
    assert store.review_log(con, "rep:BH01")[0]["version_id"] == version
    with pytest.raises(store.StaleReview):  # a second reviewer working from the old state
        store.record_review(con, "rep:BH01", ReviewStatus.rejected, "Ben", None, expected_revision=0)


def test_an_approval_names_the_blocking_findings_the_reviewer_saw(con):
    store.save_borehole(con, record())
    flagged = record()
    flagged.issues += ["sheet header differs from sheet 1"]  # same content, so no new version on save
    store.save_borehole(con, flagged)
    store.record_review(con, "rep:BH01", ReviewStatus.approved, "Ana", None)
    store.delete_document_boreholes(con, "rep")
    moved = record(bid="rep:BH1")
    moved.issues += ["sheet header differs from sheet 1"]
    store.save_borehole(con, moved)
    assert store.load_borehole(con, "rep:BH1").review_status == ReviewStatus.approved


def test_new_blocking_finding_reopens_an_approval(con):
    def rebuilt(*extra):
        bh = record()
        bh.issues += list(extra)
        return bh

    store.save_borehole(con, rebuilt("same printed coordinates as BH-02"))
    store.record_review(con, "rep:BH01", ReviewStatus.approved, "Ana", None)
    # Same rule, other wording (a new partner hole): the reviewer already saw this conflict.
    store.save_borehole(con, rebuilt("same printed coordinates as BH-02, BH-03"))
    assert store.load_borehole(con, "rep:BH01").review_status == ReviewStatus.approved

    store.save_borehole(con, rebuilt("same printed coordinates as BH-02", "sheet header differs from sheet 1"))
    bh = store.load_borehole(con, "rep:BH01")
    assert bh.review_status == ReviewStatus.pending_review
    assert "sheet header differs" in bh.score_factors[0].text
    with pytest.raises(store.StaleReview):  # approving from a screen opened before the new finding
        store.record_review(con, "rep:BH01", ReviewStatus.approved, "Ben", None,
                            expected_hash=store.content_hash(bh), expected_revision=1)


def test_reviewed_versions_survive_later_saves_and_a_rename(con):
    store.save_borehole(con, record())
    reviewed = con.execute("SELECT version_id FROM boreholes").fetchone()[0]
    store.record_review(con, "rep:BH01", ReviewStatus.approved, "Ana", "checked")
    store.save_borehole(con, record(ucs=55.0))
    store.save_borehole(con, record(ucs=60.0))  # replaces an unreviewed version: that one is dropped
    current = con.execute("SELECT version_id FROM boreholes").fetchone()[0]
    assert [r[0] for r in con.execute("SELECT id FROM record_versions ORDER BY id")] == [reviewed, current]
    # Renamed and back to the reviewed content: the decision is restored from the kept version.
    store.delete_document_boreholes(con, "rep")
    store.save_borehole(con, record(bid="rep:BH1"))
    bh = store.load_borehole(con, "rep:BH1")
    assert (bh.review_status, bh.reviewed_by) == (ReviewStatus.approved, "Ana")


# Tables and columns of the schema versions migrate() still upgrades, as far as they differ from the current one.
OLD_BOREHOLES = """CREATE TABLE boreholes (
    rid INTEGER PRIMARY KEY,
    id TEXT UNIQUE, name TEXT, doc_id TEXT, project TEXT, location_name TEXT, country TEXT,
    lat REAL, lon REAL, depth REAL, elevation REAL, investigation_type TEXT,
    confidence REAL, loc_confidence REAL, loc_method TEXT, score REAL, review_status TEXT,
    n_layers INTEGER, n_issues INTEGER, lithologies TEXT, strip TEXT, content_hash TEXT, data TEXT,
    extracted TEXT, ground TEXT, reasons TEXT, uid TEXT, version_id INTEGER, review_revision INTEGER NOT NULL DEFAULT 0
)"""


def downgrade(path: Path, version: int) -> None:
    """Turn a current database into one as the code of `version` (3 or 4) left it, rows kept."""
    old = sqlite3.connect(path)
    current = [r[1] for r in old.execute("PRAGMA table_info(boreholes)")]
    old.executescript(f"""
        ALTER TABLE boreholes RENAME TO boreholes_new;
        {OLD_BOREHOLES};
        INSERT INTO boreholes ({", ".join(current)}) SELECT {", ".join(current)} FROM boreholes_new;
        DROP TABLE boreholes_new;
        CREATE INDEX boreholes_doc ON boreholes (doc_id);
        CREATE UNIQUE INDEX boreholes_uid ON boreholes (uid);
        CREATE INDEX boreholes_queue ON boreholes (review_status, score, name, id);
        ALTER TABLE documents ADD COLUMN kind TEXT;
        ALTER TABLE pages ADD COLUMN language TEXT; ALTER TABLE pages ADD COLUMN logs TEXT;
        ALTER TABLE pages ADD COLUMN thumb TEXT; ALTER TABLE pages ADD COLUMN width INTEGER;
        ALTER TABLE pages ADD COLUMN height INTEGER;
        PRAGMA user_version = {version};
    """)
    if version == 3:
        old.executescript("CREATE INDEX boreholes_status ON boreholes (review_status, score); "
                          "ALTER TABLE lab_values DROP COLUMN value_upper; DROP TABLE upload_jobs; DROP TABLE llm_calls;")
    old.commit()
    old.close()


def schema_shape(con) -> dict:
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {"columns": {t: [r[1] for r in con.execute(f"PRAGMA table_xinfo('{t}')")] for t in tables},
            "indexes": sorted(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'index'"))}


@pytest.mark.parametrize("version", [3, 4])
def test_old_database_is_migrated_with_human_data(con, db_path, tmp_path, version):
    store.save_borehole(con, record())
    store.save_borehole(con, record(bid="rep:BH02", name="BH-02"))
    store.save_document(con, "rep", "rep.pdf", [(1, "rep/0001.jpg")])
    con.execute("UPDATE pages SET kind = 'borehole_log'")
    con.commit()
    store.record_review(con, "rep:BH01", ReviewStatus.approved, "Ana", None)
    reviewed = con.execute("SELECT version_id FROM boreholes WHERE id = 'rep:BH01'").fetchone()[0]
    store.save_borehole(con, record(ucs=55.0))  # reopened: the reviewed version stays
    store.record_review(con, "rep:BH02", ReviewStatus.rejected, "Ben", "wrong sheet")
    store.add_correction(con, "borehole", "rep:BH02", "field", {"field": "depth_m", "value": 9.0}, "Ben", None)
    store.add_knowledge(con, "global", None, "term", {"term": "Mu", "meaning": "silt"}, "review", None, "Ben")
    store.record_llm_call(con, {"at": "2026-01-02T03:04:05+00:00", "doc_id": "rep", "page": 1, "stage": "classify",
                                "model": "m", "input_tokens": 10, "outcome": "ok"})
    con.execute("INSERT INTO upload_jobs (id, task, status, created_at) VALUES ('j1', '{}', 'done', 'then')")
    uid = con.execute("SELECT uid FROM boreholes WHERE id = 'rep:BH01'").fetchone()[0]
    con.execute("INSERT INTO record_versions (uid, content_hash, data, created_at) VALUES (?, 'x', '{}', 'then')",
                (uid,))  # an unreviewed replaced version, as older code kept them
    con.commit()
    kept_tables = ("review_events", "corrections", "knowledge", "borehole_aliases")
    if version == 4:
        kept_tables += ("llm_calls", "upload_jobs")
    human = {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in kept_tables}
    rows = con.execute("SELECT rid, id, uid, review_status, content_hash, data FROM boreholes ORDER BY rid").fetchall()
    kept = {reviewed} | {r[0] for r in con.execute("SELECT version_id FROM boreholes")}
    versions = con.execute(f"SELECT * FROM record_versions WHERE id IN ({', '.join(map(str, kept))}) ORDER BY id"
                           ).fetchall()
    con.close()
    downgrade(db_path, version)

    store.migrate(db_path)
    migrated = store.connect(db_path)
    store.migrate(tmp_path / "fresh.sqlite")
    fresh = store.connect(tmp_path / "fresh.sqlite")
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        assert schema_shape(migrated) == schema_shape(fresh)
        for table, before in human.items():
            assert migrated.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == before
        assert migrated.execute("SELECT rid, id, uid, review_status, content_hash, data FROM boreholes ORDER BY rid"
                                ).fetchall() == rows
        assert migrated.execute("SELECT * FROM record_versions ORDER BY id").fetchall() == versions
        pages = [tuple(r) for r in migrated.execute("SELECT page, kind, image FROM pages")]
        assert pages == [(1, "borehole_log", "rep/0001.jpg")]
        assert migrated.execute("SELECT COUNT(*) FROM boreholes_fts").fetchone()[0] == 2
        assert migrated.execute("SELECT COUNT(*) FROM boreholes_rtree").fetchone()[0] == 2
        assert migrated.execute("SELECT value_upper IS NULL FROM lab_values").fetchone()[0] == 1
        assert store.group_borehole_ids(migrated, "rep") == ["rep:BH01", "rep:BH02"]
        assert store.load_borehole(migrated, "rep:BH02").review_status == ReviewStatus.rejected
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute("UPDATE boreholes SET review_status = 'released' WHERE id = 'rep:BH01'")
    finally:
        migrated.close()
        fresh.close()


def test_database_before_v3_is_refused(tmp_path):
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE boreholes (rid INTEGER PRIMARY KEY, id TEXT, data TEXT)")
    old.execute("INSERT INTO boreholes (id, data) VALUES ('rep:BH01', '{}')")
    old.commit()
    old.close()
    with pytest.raises(RuntimeError, match="schema version 0"):
        store.migrate(path)
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM boreholes").fetchone()[0] == 1


def test_migrate_creates_a_fresh_database_once(tmp_path):
    path = tmp_path / "new.sqlite"
    store.migrate(path)
    store.migrate(path)
    con = store.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()


def test_share_package(con, tmp_path, monkeypatch):
    import argparse
    from pipeline.__main__ import cmd_share
    store.save_borehole(con, record())
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    (tmp_path / "pages" / "rep").mkdir(parents=True)
    (tmp_path / "pages" / "rep" / "0001.jpg").write_bytes(b"jpg")
    out = tmp_path / "pkg"
    cmd_share(argparse.Namespace(out=str(out), no_pages=False, with_cache=False))
    copy = sqlite3.connect(out / "boreholes.sqlite")
    assert copy.execute("SELECT COUNT(*) FROM boreholes").fetchone()[0] == 1
    assert (out / "pages" / "rep" / "0001.jpg").exists() and "Confidential" in (out / "README.md").read_text()
    assert not (out / "cache").exists()


@pytest.mark.parametrize("field,value", [("project_name", "Invented project"), ("project_id", "invented-42"),
                                         ("location_name", "Invented site"), ("notes", "Check the source"),
                                         ("drilling_method", "rotary")])
def test_metadata_changes_reopen_review(con, field, value):
    original = record()
    store.save_borehole(con, original)
    store.record_review(con, original.borehole_id, ReviewStatus.approved, "Ana", None)
    changed = record().model_copy(update={field: value})
    store.save_borehole(con, changed)
    assert store.load_borehole(con, changed.borehole_id).review_status == ReviewStatus.pending_review
    assert con.execute("SELECT COUNT(*) FROM record_versions").fetchone()[0] == 2


def test_lab_projection_migration_preserves_reviews(con, db_path):
    bh = record()
    bh.lab_results = [LabResult(parameter="ucs", value=5, value_upper=10, qualifier="range", value_raw="5–10 MPa")]
    store.save_borehole(con, bh)
    store.record_review(con, bh.borehole_id, ReviewStatus.approved, "Ana", None)
    con.close()
    downgrade(db_path, 3)
    store.migrate(db_path)
    migrated = store.connect(db_path)
    try:
        assert migrated.execute("SELECT value_upper FROM lab_values").fetchone()[0] == 10
        assert store.load_borehole(migrated, bh.borehole_id).review_status == ReviewStatus.approved
    finally:
        migrated.close()


def test_demo_data_refuses_the_real_database(con):
    from fixtures import demo_data
    store.save_borehole(con, record())
    with pytest.raises(SystemExit, match="DB_PATH=data/demo.sqlite"):
        demo_data.main()
    assert con.execute("SELECT COUNT(*) FROM boreholes").fetchone()[0] == 1


def test_a_rerendered_document_keeps_its_page_classes_and_drops_lost_pages(con):
    store.save_document(con, "rep", "rep.pdf", [(1, "rep/0001.jpg"), (2, "rep/0002.jpg")])
    con.execute("UPDATE pages SET kind = 'borehole_log'")
    con.commit()
    store.save_document(con, "rep", "rep.pdf", [(1, "rep/0001.jpg")])
    assert [tuple(r) for r in con.execute("SELECT page, kind FROM pages")] == [(1, "borehole_log")]
    assert con.execute("SELECT pages FROM documents").fetchone()[0] == 1


def test_reset_keeps_review_decisions(con):
    store.save_borehole(con, record())
    store.record_review(con, "rep:BH01", ReviewStatus.rejected, "Ana", None)
    store.reset(con)
    assert con.execute("SELECT COUNT(*) FROM boreholes").fetchone()[0] == 0
    version = con.execute("SELECT decision, version_id FROM review_events").fetchone()
    assert version[0] == "rejected"
    assert con.execute("SELECT COUNT(*) FROM record_versions WHERE id = ?", (version[1],)).fetchone()[0] == 1


def test_share_package_without_git(con, tmp_path, monkeypatch):
    import argparse
    import subprocess
    from pipeline.__main__ import cmd_share

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", no_git)
    out = tmp_path / "pkg"
    cmd_share(argparse.Namespace(out=str(out), no_pages=True, with_cache=False))
    assert "commit unknown" in (out / "README.md").read_text()


def test_a_new_blocking_finding_stops_a_review_screen_opened_before_it(con):
    store.save_borehole(con, record())
    seen = con.execute("SELECT content_hash, review_revision FROM boreholes").fetchone()
    store.save_borehole(con, record())  # an identical rebuild leaves open review screens valid
    assert con.execute("SELECT review_revision FROM boreholes").fetchone()[0] == seen["review_revision"]
    flagged = record()
    flagged.issues += ["gap 4.5–5.0 m between layers"]  # same content hash, new blocking finding
    store.save_borehole(con, flagged)
    with pytest.raises(store.StaleReview):
        store.record_review(con, "rep:BH01", ReviewStatus.approved, "Ana", None,
                            expected_hash=seen["content_hash"], expected_revision=seen["review_revision"])
    assert store.load_borehole(con, "rep:BH01").review_status != ReviewStatus.approved


def test_a_hole_of_another_document_with_the_same_name_keeps_its_uid_and_corrections(con):
    a, b = record(bid="folder__a:BH01", name="BH-01"), record(bid="folder__b:BH01", name="BH-01")
    store.save_borehole(con, a)
    store.save_borehole(con, b)  # the group's natural key now points to b's hole
    uid_b = con.execute("SELECT uid FROM boreholes WHERE id = 'folder__b:BH01'").fetchone()[0]
    store.add_correction(con, "borehole", b.borehole_id, "position", {"lat": 47.99, "lon": 7.85}, "Ana", None)
    con.commit()

    renamed = record(bid="folder__a:BH1", name="BH 1")  # a's hole under another id, b's document not rerun
    assert store.uid_for(con, renamed) != uid_b
    assert store.corrections_for(con, renamed.borehole_id, store.uid_for(con, renamed)) == []

    # Both documents rewritten, the renamed hole saved first while b's old record is already deleted.
    previous = {**store.delete_document_boreholes(con, "folder__a"), **store.delete_document_boreholes(con, "folder__b")}
    store.save_borehole(con, renamed, commit=False)
    store.save_borehole(con, b, previous=previous[b.borehole_id])
    uids = dict(con.execute("SELECT id, uid FROM boreholes").fetchall())
    assert uids["folder__b:BH01"] == uid_b and uids["folder__a:BH1"] != uid_b
