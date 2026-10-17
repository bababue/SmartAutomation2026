"""Store v3: projections, stable uids across id changes, record versions, review revision (synthetic data)."""

import sqlite3

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
