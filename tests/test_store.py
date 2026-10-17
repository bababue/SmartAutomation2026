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


@pytest.fixture()
def con(tmp_path, monkeypatch):
    path = tmp_path / "s.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store.connect, "__defaults__", (path,))
    c = store.connect(path)
    yield c
    c.close()


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
    assert bh.score_reasons[0] == "Data changed since the rejected decision by Ana"


@pytest.mark.parametrize("decision", [ReviewStatus.approved, ReviewStatus.rejected])
def test_id_change_keeps_the_decision_on_unchanged_content(con, decision):
    store.save_borehole(con, record())
    store.record_review(con, "rep:BH01", decision, "Ana", "checked")
    store.delete_document_boreholes(con, "rep")
    store.save_borehole(con, record(bid="rep:BH1"))
    bh = store.load_borehole(con, "rep:BH1")
    assert (bh.review_status, bh.reviewed_by, bh.review_comment) == (decision, "Ana", "checked")
    assert bh.reviewed_at is not None


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
    store.save_borehole(con, record(ucs=55.0))  # changed: new version
    assert con.execute("SELECT COUNT(*) FROM record_versions").fetchone()[0] == 2
    version = con.execute("SELECT version_id FROM boreholes").fetchone()[0]
    store.record_review(con, "rep:BH01", ReviewStatus.approved, "Ana", None, expected_revision=0)
    assert store.review_log(con, "rep:BH01")[0]["version_id"] == version
    with pytest.raises(store.StaleReview):  # a second reviewer working from the old state
        store.record_review(con, "rep:BH01", ReviewStatus.rejected, "Ben", None, expected_revision=0)


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
    assert "sheet header differs" in bh.score_reasons[0]
    with pytest.raises(store.StaleReview):  # approving from a screen opened before the new finding
        store.record_review(con, "rep:BH01", ReviewStatus.approved, "Ben", None,
                            expected_hash=store.content_hash(bh), expected_revision=1)


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    con = store.connect(path)
    store.save_borehole(con, record())
    con.execute("UPDATE boreholes SET uid = NULL, version_id = NULL, reasons = NULL")
    for table in ("layers", "lab_values", "findings", "record_sources", "borehole_aliases", "record_versions"):
        con.execute(f"DELETE FROM {table}")
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()
    con = store.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    assert tuple(con.execute("SELECT uid IS NOT NULL, version_id IS NOT NULL FROM boreholes").fetchone()) == (1, 1)
    assert con.execute("SELECT COUNT(*) FROM layers").fetchone()[0] == 2


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


def test_lab_projection_migration_preserves_reviews(con):
    bh = record()
    bh.lab_results = [LabResult(parameter="ucs", value=5, value_upper=10, qualifier="range", value_raw="5–10 MPa")]
    store.save_borehole(con, bh)
    store.record_review(con, bh.borehole_id, ReviewStatus.approved, "Ana", None)
    con.execute("ALTER TABLE lab_values DROP COLUMN value_upper")
    con.execute("PRAGMA user_version = 3")
    con.commit()
    path = con.execute("PRAGMA database_list").fetchone()["file"]
    migrated = store.connect(Path(path))
    try:
        assert migrated.execute("SELECT value_upper FROM lab_values").fetchone()[0] == 10
        assert store.load_borehole(migrated, bh.borehole_id).review_status == ReviewStatus.approved
    finally:
        migrated.close()
