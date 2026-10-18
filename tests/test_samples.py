"""The public logs in samples/ through every step after the model, from their committed model answers."""

import pytest

from pipeline import config, runner

SAMPLES = config.ROOT / "samples"


@pytest.fixture()
def samples_db(db_path, tmp_path, monkeypatch, con):
    monkeypatch.setattr(config, "DATASET_DIR", SAMPLES)
    monkeypatch.setattr(config, "CACHE_DIR", SAMPLES / "cache")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    runner.rebuild(runner.select([]))
    return con


def test_samples_rebuild_from_committed_cache(samples_db):
    rows = samples_db.execute("SELECT rid, name, lat, lon, depth, review_status FROM boreholes ORDER BY name").fetchall()
    # No records means a cache miss: after a PROMPT_VERSION bump, refresh samples/cache (samples/README.md).
    assert [r[1] for r in rows] == ["EWS 1/01 Im Hölderle Ebhausen", "EWS 1/18 Roggenäcker Ebhausen"]
    # Printed as bare northing/easting; ETRS89 / UTM 32N gives these positions in Ebhausen.
    assert [(round(r[2], 5), round(r[3], 5)) for r in rows] == [(48.58389, 8.68683), (48.58339, 8.68968)]
    assert [r[4] for r in rows] == [98.0, 100.0]
    pages = [tuple(r) for r in samples_db.execute("SELECT doc_id, page FROM pages ORDER BY doc_id")]
    assert pages == [("BO_7418_00676_Bohrprofil", 1), ("BO_7418_01197_Bohrprofil", 1)]  # the source page beside the record
    bounds = [[(top, bottom) for top, bottom in samples_db.execute(
        "SELECT top_m, bottom_m FROM layers WHERE rid = ? ORDER BY seq", (r[0],))] for r in rows]
    assert bounds == [[(0, 2), (2, 24), (24, 36), (36, 80), (80, 98)],
                      [(0, 14), (14, 48), (48, 50), (50, 72), (72, 84), (84, 100)]]


def test_samples_inferred_crs_waits_for_a_person(samples_db):
    statuses = {r[0] for r in samples_db.execute("SELECT review_status FROM boreholes")}
    assert statuses == {"pending_review"}
    inferred = [r[0] for r in samples_db.execute("SELECT epsg FROM findings WHERE code = 'crs_inferred'")]
    assert inferred == [25832, 25832]
