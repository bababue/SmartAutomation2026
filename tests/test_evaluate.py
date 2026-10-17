"""Evaluation against human-checked records (synthetic data only)."""

import pytest

from pipeline import evaluate, store
from schema.borehole import Borehole, GeorefMethod, Layer, Location, ReviewStatus, SourceRef

# Brandenburg Gate; 0.001° latitude is about 111 m
LAT, LON = 52.5163, 13.3777


def ref(doc="DOC1", layers=None, lat=LAT, lon=LON, epsg=25833):
    return {"doc_id": doc, "layers": layers if layers is not None else [[0, 1.2, "fill"], [1.2, 4.5, "sand"],
                                                                        [4.5, 9.0, "clay"]],
            "lat": lat, "lon": lon, "crs_epsg": epsg}


def test_boundaries_match_within_tolerance_once():
    assert evaluate.match_boundaries([0, 1.2, 4.5], [0, 1.25, 4.7]) == 2
    assert evaluate.match_boundaries([1.0, 1.05], [1.02]) == 1  # one current boundary serves one reference


def test_lithology_compared_on_matched_layers_only():
    result = evaluate.compare_layers([[0, 1.2, "fill"], [1.2, 4.5, "sand"], [4.5, 9.0, "clay"]],
                                     [[0, 1.2, "fill"], [1.2, 4.0, "sand"], [4.0, 9.0, "silt"]])
    assert result == {"boundaries": 4, "boundaries_matched": 3, "layers_matched": 1, "lithology_same": 1}
    result = evaluate.compare_layers([[0, 2, "sand"]], [[0.05, 2.05, "gravel"]])
    assert result["layers_matched"] == 1 and result["lithology_same"] == 0


def test_position_error_in_metres():
    assert evaluate.position_error_m(ref(), ref(lat=LAT + 0.001)) == pytest.approx(111, abs=1)
    assert evaluate.position_error_m(ref(), ref(lat=None)) is None


def test_compare_per_document():
    reference = {"DOC1:BH1": ref(), "DOC1:BH2": ref(), "DOC2:BH1": ref(doc="DOC2")}
    current = {
        "DOC1:BH1": ref(lat=LAT + 0.001),
        "DOC1:BH3": ref(),  # not in the reference
        "DOC2:BH1": ref(doc="DOC2", layers=[[0, 9.0, "clay"]], lat=None, epsg=None),
        "DOC3:BH1": ref(doc="DOC3"),  # document without reference: ignored
    }
    scores = evaluate.compare(reference, current, corrected={"DOC2:BH1"})
    assert set(scores) == {"DOC1", "DOC2"}
    doc1 = scores["DOC1"]
    assert doc1.missing == ["DOC1:BH2"] and doc1.extra == ["DOC1:BH3"]
    assert doc1.boundaries_matched == doc1.boundaries == 4 and doc1.lithology_same == 3
    assert doc1.position_errors_m == [pytest.approx(111, abs=1)]
    doc2 = scores["DOC2"]
    assert (doc2.boundaries_matched, doc2.boundaries) == (2, 4)
    assert doc2.no_position == 1 and doc2.crs_changed == 1 and doc2.corrected == 1
    text = evaluate.report(scores)
    assert "overall" in text and "DOC1:BH2" in text


def test_snapshot_keeps_earlier_approvals(tmp_path):
    con = store.connect(tmp_path / "test.sqlite")
    src = SourceRef(file="demo.pdf", page=1)

    def save(bid, status, primary="sand"):
        bh = Borehole(borehole_id=bid, borehole_name=bid.split(":")[1], review_status=status,
                      layers=[Layer(top_m=0, bottom_m=3, description_original="x", primary=primary, source=src)],
                      location=Location(lat=LAT, lon=LON, method=GeorefMethod.stated_coordinates, confidence=1))
        store.save_borehole(con, bh)

    save("DOC1:BH1", ReviewStatus.approved)
    save("DOC1:BH2", ReviewStatus.pending_review)
    path = tmp_path / "eval" / "reference.json"
    assert evaluate.snapshot(con, path) == (1, 0, 1)
    save("DOC1:BH1", ReviewStatus.pending_review)  # re-run changed it, back to review
    save("DOC1:BH2", ReviewStatus.approved, primary="clay")
    assert evaluate.snapshot(con, path) == (1, 0, 2)
    reference = evaluate.load_reference(path)
    assert reference["DOC1:BH2"]["layers"] == [[0, 3, "clay"]]
    con.close()


def test_corrected_record_still_shows_the_model_error(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from pipeline import config

    path = tmp_path / "test.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store.connect, "__defaults__", (path,))
    con = store.connect(path)
    src = SourceRef(file="demo.pdf", page=1)
    bh = Borehole(borehole_id="DOC1:BH1", borehole_name="BH1", depth_m=5,
                  layers=[Layer(top_m=0, bottom_m=2, description_original="Sand", primary="sand", source=src),
                          Layer(top_m=2, bottom_m=5, description_original="Silt", primary="clay", source=src)],
                  location=Location(lat=LAT, lon=LON, method=GeorefMethod.stated_coordinates, confidence=1))
    store.save_borehole(con, bh, extracted=store.extracted_summary(bh))  # as the pipeline stores it

    r = TestClient(app).post("/api/boreholes/DOC1:BH1/corrections", json={
        "reviewer": "Anna", "kind": "layer", "payload": {"orig_top": 2, "orig_bottom": 5, "primary": "silt"}})
    assert r.status_code == 200, r.text
    store.record_review(con, "DOC1:BH1", ReviewStatus.approved, "Anna", None)
    evaluate.snapshot(con, tmp_path / "reference.json")
    reference = evaluate.load_reference(tmp_path / "reference.json")
    assert reference["DOC1:BH1"]["layers"][1] == [2, 5, "silt"]

    model, fallback = evaluate.current_entries(con)
    assert fallback == 0 and model["DOC1:BH1"]["layers"][1] == [2, 5, "clay"]  # survived the API save
    assert evaluate.compare(reference, model)["DOC1"].lithology_same == 1  # the model's mistake shows
    final, _ = evaluate.current_entries(con, corrected=True)
    assert evaluate.compare(reference, final)["DOC1"].lithology_same == 2
    con.close()


def test_database_without_extracted_column_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    # The schema as it was before the column existed
    old.executescript(store.SCHEMA.replace(
        "data TEXT,\n    extracted TEXT,  -- model output before reviewer corrections (layers, position), for evaluation\n"
        "    ground TEXT  -- derived ground model values (pipeline/ground.py)",
        "data TEXT"))
    assert "extracted" not in {r[1] for r in old.execute("PRAGMA table_info(boreholes)")}
    old.execute("INSERT INTO boreholes (id, doc_id, review_status, data) VALUES ('A:B', 'A', 'approved', '{}')")
    old.commit()
    old.close()
    con = store.connect(path)
    assert con.execute("SELECT id, extracted FROM boreholes").fetchone()[:] == ("A:B", None)
    con.close()
