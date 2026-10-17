"""Evaluation against human-checked records (synthetic data only)."""

import pytest

from pipeline import evaluate, store
from schema.borehole import (Borehole, GeorefMethod, LabParameter, LabResult, Layer, Location, ReviewStatus,
                             SourceRef)

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
    assert (doc1.boundaries_matched, doc1.boundaries) == (4, 8)  # the missing record's boundaries count as missed
    assert doc1.lithology_same == 3 and doc1.no_position == 1
    assert doc1.position_errors_m == [pytest.approx(111, abs=1)]
    doc2 = scores["DOC2"]
    assert (doc2.boundaries_matched, doc2.boundaries) == (2, 4)
    assert doc2.no_position == 1 and doc2.crs_changed == 1 and doc2.corrected == 1
    text = evaluate.report(scores)
    assert "overall" in text and "DOC1:BH2" in text


def test_renamed_hole_is_matched_by_natural_key_and_extra_boundaries_lower_precision():
    reference = {"DOC1:BH1": {**ref(), "key": "nk:DOC1|borehole|1"}}
    current = {"DOC1:B-1": {**ref(layers=[[0, 1.2, "fill"], [1.2, 3.0, "sand"], [3.0, 4.5, "sand"],
                                         [4.5, 9.0, "clay"]]), "key": "nk:DOC1|borehole|1"}}
    s = evaluate.compare(reference, current)["DOC1"]
    assert s.missing == [] and s.extra == []
    assert (s.boundaries_matched, s.boundaries, s.boundaries_current) == (4, 4, 5)
    assert s.metres_same == pytest.approx(9.0) and s.lithology_same == 2  # split layer: per-layer check misses it


def test_ambiguous_natural_key_is_not_matched():
    reference = {"DOC1:BH1": {**ref(), "key": "nk:DOC1|borehole|1"}}
    twins = {bid: {**ref(), "key": "nk:DOC1|borehole|1"} for bid in ("DOC1:BH1a", "DOC1:BH1b")}
    assert evaluate.compare(reference, twins)["DOC1"].missing == ["DOC1:BH1"]


def test_page_named_reference_is_matched_by_its_full_printed_name_on_that_page_only_if_unique():
    reference = {"DOC1__A:TP4-p12": {**ref(), "key": "nk:DOC1|test_pit|TP4P12"}}
    full = {**ref(), "key": "nk:DOC1|test_pit|TP4NORTH", "pages": [12]}
    assert evaluate.compare(reference, {"DOC1__A:TP4NORTH": full})["DOC1"].missing == []
    assert evaluate.compare(reference, {"DOC1__A:TP4NORTH": {**full, "pages": [13]}})["DOC1"].missing == ["DOC1__A:TP4-p12"]
    two = {"DOC1__A:TP4NORTH": full, "DOC1__A:TP4-SOUTH": {**full, "key": "nk:DOC1|test_pit|TP4SOUTH"}}
    assert evaluate.compare(reference, two)["DOC1"].missing == ["DOC1__A:TP4-p12"]  # two candidates: no match
    assert evaluate.same_name("DOC1:BH-1", "DOC1:bh 1 (north)") and not evaluate.same_name("DOC1:BH1", "DOC1:BH10")


def test_final_depth_groundwater_and_lab_values():
    reference = {"DOC1:BH1": {**ref(), "depth_m": 9.0, "groundwater": [2.4], "lab": [["ucs", 42.0, 6.0], ["water_content", 18.0, None]]}}
    current = {"DOC1:BH1": {**ref(), "depth_m": 9.05, "groundwater": [2.4, 3.1], "lab": [["ucs", 42.0, 6.0], ["water_content", 18.0, 2.0]]}}
    s = evaluate.compare(reference, current)["DOC1"]
    assert (s.depths_ok, s.depths, s.groundwater_matched, s.groundwater) == (1, 1, 1, 1)
    assert (s.lab_matched, s.lab) == (1, 2)  # a value at another sample depth is not the same value
    old = evaluate.compare({"DOC1:BH1": ref()}, current)["DOC1"]  # reference without these fields: not counted
    assert (old.depths, old.groundwater, old.lab) == (0, 0, 0)


def test_swapped_analytes_with_equal_values_do_not_match():
    def lab(*results: tuple[str, str, str | None]) -> list[list]:
        bh = Borehole(borehole_id="DOC1:BH1", borehole_name="BH1", lab_results=[
            LabResult(parameter=LabParameter.other, parameter_raw=name, value=1.0, value_raw=raw, qualifier=q, top_m=2.0)
            for name, raw, q in results])
        return store.extracted_summary(bh)["lab"]

    reference = lab(("Analyte A", "<1 mg/kg", "<"), ("Analyte B", "<1 mg/kg", "<"))
    assert evaluate.match_lab(reference, lab(("Analyte A", "<1 mg/kg", "<"), ("Analyte B", "<1 mg/kg", "<"))) == 2
    assert evaluate.match_lab(reference, lab(("Analyte A", "<1 mg/kg", "<"), ("Analyte C", "<1 mg/kg", "<"))) == 1
    assert evaluate.match_lab(reference, lab(("Analyte A", "<1 µg/kg", "<"), ("Analyte B", "1 mg/kg", None))) == 0
    assert evaluate.match_lab([r[:3] for r in reference], lab(("Analyte C", "<1 µg/kg", "<"))) == 1  # old snapshot


def test_holdout_survives_snapshots(tmp_path):
    path = tmp_path / "reference.json"
    evaluate.set_holdout(["DOC2"], path)
    con = store.connect(tmp_path / "test.sqlite")
    evaluate.snapshot(con, path)
    assert evaluate.load_holdout(path) == {"DOC2"}
    scores = evaluate.compare({"DOC1:BH1": ref(), "DOC2:BH1": ref(doc="DOC2")}, {"DOC1:BH1": ref()})
    assert evaluate.total(scores, {"DOC2"}).missing == ["DOC2:BH1"]
    assert "holdout" in evaluate.report(scores, {"DOC2"})
    con.close()


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


def test_lithology_metres_counts_overlapping_layers_once():
    reference = [[0.0, 4.0, "clay"]]
    current = [[0.0, 3.0, "clay"], [2.0, 4.0, "clay"], [1.0, 2.0, "sand"]]
    assert evaluate.lithology_metres(reference, current) == 4.0


def test_missing_record_counts_against_depth_groundwater_and_lab():
    reference = {"DOC1:BH1": {**ref(), "depth_m": 9.0, "groundwater": [2.0], "lab": [["ucs", 40.0, 3.0]]}}
    s = evaluate.total(evaluate.compare(reference, {}))
    assert (s.depths, s.depths_ok, s.groundwater, s.groundwater_matched, s.lab, s.lab_matched) == (1, 0, 1, 0, 1, 0)


def test_lab_evaluation_distinguishes_range_upper_bounds():
    bh = Borehole(borehole_id="r:BH1", borehole_name="BH1", lab_results=[
        LabResult(parameter="ucs", value=5, value_upper=10, qualifier="range", top_m=2)])
    reference = store.extracted_summary(bh)["lab"]
    assert evaluate.match_lab(reference, reference) == 1
    bh.lab_results[0].value_upper = 20
    assert evaluate.match_lab(reference, store.extracted_summary(bh)["lab"]) == 0
