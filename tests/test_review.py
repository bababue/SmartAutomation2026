"""Scoring, automatic release and preservation of human decisions (synthetic data only)."""

import pytest
from fastapi.testclient import TestClient

from pipeline import config, store
from pipeline.assemble import _layers, _num, id_key, norm_name
from pipeline.extraction_models import XLayer
from pipeline.scoring import score, text_agreement
from schema.borehole import Borehole, GeorefMethod, Layer, Location, ReviewStatus, SourceRef


def make(position: float = 0.95, method=GeorefMethod.stated_coordinates, layers=None) -> Borehole:
    src = SourceRef(file="demo.pdf", page=1)
    layers = layers if layers is not None else [
        Layer(top_m=0, bottom_m=1.2, description_original="Fill", primary="fill", source=src),
        Layer(top_m=1.2, bottom_m=4.5, description_original="Sand, dense", primary="sand", source=src),
        Layer(top_m=4.5, bottom_m=9.0, description_original="Clay, stiff", primary="clay", source=src),
    ]
    bh = Borehole(
        borehole_id="demo:BH1", borehole_name="BH1", depth_m=9.0, layers=layers,
        location=Location(lat=48.47, lon=7.94, method=method, confidence=position),
    )
    bh.issues = bh.validate_log()
    return bh


def test_clean_record_is_released_automatically():
    bh = make()
    score(bh, {}, unreadable=0)
    assert bh.score >= config.AUTO_APPROVE_THRESHOLD
    assert bh.review_status == ReviewStatus.auto_approved


def test_approximate_position_needs_review():
    bh = make(position=0.3, method=GeorefMethod.geocoded_place)
    score(bh, {}, unreadable=0)
    assert bh.review_status == ReviewStatus.pending_review
    assert any("place name" in r for r in bh.score_reasons)


def test_gap_lowers_score_below_threshold():
    bh = make()
    bh.layers[1].bottom_m = 4.0  # gap 4.0–4.5
    bh.issues = bh.validate_log()
    score(bh, {}, unreadable=0)
    assert bh.review_status == ReviewStatus.pending_review


def test_text_layer_disagreement_lowers_confidence():
    bh = make()
    text = "Borehole log " + "x" * 300 + " 1.20 4.50 9.00"
    assert text_agreement(bh, {("demo.pdf", 1): text}) == 1.0
    wrong = "Borehole log " + "x" * 300 + " 1.30 4.70 9.20"
    score(bh, {("demo.pdf", 1): wrong}, unreadable=0)
    assert bh.confidence < 0.6
    assert bh.review_status == ReviewStatus.pending_review


def test_layers_without_bottoms_are_not_fabricated():
    def x(top, bottom):
        return XLayer(top_m=top, bottom_m=bottom, description_original="a", description_en=None, primary="sand",
                      secondary=[], uscs=None, stratigraphic_name=None, stratigraphic_code=None,
                      stratigraphic_color=None, geologic_age=None, color=None, consistency=None,
                      relative_density=None, weathering=None, rock_strength=None, moisture=None, bbox=None)

    class Log:
        sheet = None
        layers = [x(0, None), x(None, 5), x(5, 10)]

    layers = _layers([(Log, SourceRef(file="f", page=1))], final_depth=10)
    # The unknown internal boundary is not invented; only the fully known layer remains.
    assert [(l.top_m, l.bottom_m) for l in layers] == [(5, 10)]


def _xlayer(top, bottom, description="a", primary="sand"):
    return XLayer(top_m=top, bottom_m=bottom, description_original=description, description_en=None, primary=primary,
                  secondary=[], uscs=None, stratigraphic_name=None, stratigraphic_code=None,
                  stratigraphic_color=None, geologic_age=None, color=None, consistency=None,
                  relative_density=None, weathering=None, rock_strength=None, moisture=None, bbox=None)


def _sheets(*pages):
    out = []
    for page, layers in enumerate(pages, start=1):
        log = type("Log", (), {"layers": layers, "sheet": None})
        out.append((log, SourceRef(file="f", page=page)))
    return out


def test_unit_restated_on_later_sheet_is_clipped():
    sheets = _sheets([_xlayer(0, 5, "sand"), _xlayer(5, 20, "granite, moderately weathered", "plutonic_rock")],
                     [_xlayer(5, 30, "joints at 22-25 m", "plutonic_rock"), _xlayer(30, 40, "clay", "clay")])
    notes: list[str] = []
    layers = _layers(sheets, final_depth=40, notes=notes)
    assert [(l.top_m, l.bottom_m) for l in layers] == [(0, 5), (5, 20), (20, 30), (30, 40)]
    assert len(notes) == 1


def test_overlap_without_restated_top_is_kept():
    # The top does not match any earlier layer: possibly a misread, so the overlap stays visible.
    sheets = _sheets([_xlayer(0, 20)], [_xlayer(15, 30, "b")])
    layers = _layers(sheets, final_depth=30)
    assert [(l.top_m, l.bottom_m) for l in layers] == [(0, 20), (15, 30)]


def test_leading_zeros_in_hole_names():
    assert norm_name("BH-01") == norm_name("BH 1") == "BH1"
    assert norm_name("BH-10") == "BH10"
    assert norm_name("TP-0") == "TP0"
    assert id_key("BH-01") == "BH01"  # IDs keep the written form
    assert norm_name("BH No. 1") == "BH1"
    assert id_key("BH_ZZ01") == "BHZZ01"


def test_number_groups_keep_holes_apart():
    assert norm_name("BH1-01") == norm_name("BH1-1") == norm_name("BH 1.1") == "BH1-1"
    assert norm_name("BH10-1") == "BH10-1" != norm_name("BH1-01")
    assert id_key("BH1-01") == "BH101"  # existing IDs unchanged ...
    assert id_key("BH1-01", separate_numbers=True) == "BH1-01"  # ... unless they collide


def test_colliding_ids_are_separated():
    from pipeline.assemble import _unique_ids

    a = make()
    a.borehole_id, a.borehole_name = "doc:BH101", "BH1-01"
    b = make()
    b.borehole_id, b.borehole_name = "doc:BH101", "BH10-1"
    c = make()
    c.borehole_id, c.borehole_name = "doc:BH2", "BH-2"
    _unique_ids([a, b, c])
    assert [x.borehole_id for x in (a, b, c)] == ["doc:BH1-01", "doc:BH10-1", "doc:BH2"]


def test_inferred_crs_is_explained_in_review_reasons():
    bh = make(position=0.8)
    bh.issues.append("coordinate system inferred, not stated: EPSG:32632")
    score(bh, {}, unreadable=0)
    assert bh.review_status == ReviewStatus.pending_review
    assert bh.score_reasons == ["Coordinate system inferred, not stated: EPSG:32632"]


def test_scientific_notation():
    assert _num("1.2E-6") == pytest.approx(1.2e-6)
    assert _num("3,5x10-7") == pytest.approx(3.5e-7)


def test_human_decision_survives_rerun_with_same_content(con):
    bh = make(position=0.3, method=GeorefMethod.geocoded_place)
    score(bh, {}, 0)
    store.save_borehole(con, bh)
    store.record_review(con, bh.borehole_id, ReviewStatus.approved, "Anna", "position checked on site plan")

    rerun = make(position=0.3, method=GeorefMethod.geocoded_place)
    score(rerun, {}, 0)
    store.save_borehole(con, rerun)
    saved = store.load_borehole(con, bh.borehole_id)
    assert saved.review_status == ReviewStatus.approved and saved.reviewed_by == "Anna"


def test_human_decision_resets_when_content_changes(con):
    bh = make()
    score(bh, {}, 0)
    store.save_borehole(con, bh)
    store.record_review(con, bh.borehole_id, ReviewStatus.rejected, "Ben", None)

    changed = make()
    changed.layers[2].bottom_m = 12.0
    score(changed, {}, 0)
    store.save_borehole(con, changed)
    saved = store.load_borehole(con, bh.borehole_id)
    assert saved.review_status == ReviewStatus.pending_review
    assert "Data changed" in saved.score_reasons[0]


def test_review_api(con):
    from api.main import app

    bh = make(position=0.3, method=GeorefMethod.geocoded_place)
    score(bh, {}, 0)
    store.save_borehole(con, bh)
    client = TestClient(app)
    assert [r["id"] for r in client.get("/api/review-queue").json()] == ["demo:BH1"]
    res = client.post("/api/boreholes/demo:BH1/review", json={"decision": "approved", "reviewer": "Anna"})
    assert res.status_code == 200
    assert res.json()["review_log"][0]["reviewer"] == "Anna"
    assert client.get("/api/review-queue").json() == []
    released = client.get("/api/boreholes", params={"status": "released"}).json()["features"]
    assert [f["properties"]["id"] for f in released] == ["demo:BH1"]
    assert client.post("/api/boreholes/missing/review", json={"decision": "approved", "reviewer": "A"}).status_code == 404


def test_lithology_carried_over_sheet_break_only():
    sheets = _sheets([_xlayer(0, 20, "granite, slightly weathered", "plutonic_rock")],
                     [_xlayer(20, 30, "joints at 22-25 m", "unknown"), _xlayer(30, 40, "medium rock", "unknown")])
    notes: list[str] = []
    layers = _layers(sheets, final_depth=40, notes=notes)
    # The unit running over the break inherits; the next unit on the same sheet stays unknown.
    assert [l.primary.value for l in layers] == ["plutonic_rock", "plutonic_rock", "unknown"]
    assert len(notes) == 1


def test_document_crs_prefers_resolvable_statement():
    from pipeline.assemble import PageResult, document_crs
    pages = [
        PageResult("d", "d.pdf", 1, "text", None, "Report. Levels in m AHD. All coordinates are MGA94 Zone 56."),
        PageResult("d", "d.pdf", 2, "borehole_log", None, "", None),
        PageResult("d", "d.pdf", 3, "legend", None, "", "Grid: MGA94 Zone 56"),
    ]
    text, page = document_crs(pages)
    assert "MGA94" in text and page in (1, 3)
    assert document_crs([PageResult("d", "d.pdf", 1, "text", None, "Site investigation in Korea")]) is None


def test_position_outlier_is_flagged():
    from pipeline.assemble import flag_position_outliers
    holes = []
    for i, (lat, lon) in enumerate([(48.47, 7.94), (48.471, 7.941), (48.472, 7.939), (49.9, 7.94)]):
        bh = make()
        bh.borehole_id = f"demo:BH{i}"
        bh.location = Location(lat=lat, lon=lon, method=GeorefMethod.stated_coordinates, confidence=0.95)
        holes.append(bh)
    flag_position_outliers(holes)
    assert [b.location.confidence for b in holes] == [0.95, 0.95, 0.95, 0.5]
    assert any("km from the other holes" in i for i in holes[3].issues)


def test_groundwater_repeated_on_every_sheet_is_kept_once():
    from pipeline.assemble import _unique_groundwater
    from schema.borehole import GroundwaterObservation
    src = [SourceRef(file="f", page=i) for i in (1, 2, 3)]
    obs = [GroundwaterObservation(depth_m=4.5, kind="standing", raw="GL(-) 4.5 M", source=s) for s in src]
    obs.append(GroundwaterObservation(depth_m=6.0, kind="struck", raw="6.0", source=src[2]))
    assert [(g.depth_m, g.source.page) for g in _unique_groundwater(obs)] == [(4.5, 1), (6.0, 3)]


def test_location_text_without_source_notes():
    from pipeline.assemble import clean_location_text

    assert clean_location_text("Hyde Park, Sydney (from text file path)") == "Hyde Park, Sydney"
    assert clean_location_text("Marienplatz (from text file path reports/site/log 3.txt)") == "Marienplatz"
    assert clean_location_text("Harbour Bridge (reports/bridge.pdf)") == "Harbour Bridge"
    assert clean_location_text("Opera House (opera_logs.PDF), Sydney") == "Opera House, Sydney"
    assert clean_location_text("(see file name)") is None
    # Ordinary place names stay as printed
    for name in ["Hyde Park (North)", "Frankfurt (Main)", "Station Rd / Main St", "St. Paul's (Site B)",
                 "Coastal Walk (Path 3)", "Lot 5 (Area A/B)", "Landfill (East)"]:
        assert clean_location_text(name) == name
    assert clean_location_text(None) is None


def test_partial_log_is_not_released_even_with_a_good_position():
    src = SourceRef(file="demo.pdf", page=2)
    bh = make(layers=[Layer(top_m=5, bottom_m=7, description_original="Sand", primary="sand", source=src),
                      Layer(top_m=7, bottom_m=10, description_original="Clay", primary="clay", source=src)])
    bh.depth_m = 10
    bh.issues = bh.validate_log()
    score(bh, {}, unreadable=0)
    assert bh.review_status == ReviewStatus.pending_review


def test_release_needs_positive_evidence():
    bh = make()
    bh.depth_m = None  # no stated final depth and no text layer: nothing confirms the depths
    bh.issues = bh.validate_log()
    score(bh, {}, unreadable=0)
    assert bh.confidence <= 0.85 and bh.review_status == ReviewStatus.pending_review
    assert any("confirms" in r for r in bh.score_reasons)

    text = "Borehole log " + "x" * 300 + " 1.20 4.50 9.00"  # text layer confirms the boundaries
    score(bh, {("demo.pdf", 1): text}, unreadable=0)
    assert bh.review_status == ReviewStatus.auto_approved


def test_feet_logs_need_a_person():
    from pipeline.assemble import unit_findings
    from pipeline.extraction_models import XLog
    def log(unit):
        return XLog.model_validate({**{k: None for k in XLog.model_fields}, "name": "BH1", "depth_unit_raw": unit,
                                    "layers": [], "spt": [], "core_runs": [], "samples": [], "groundwater": [],
                                    "lab_results": [], "unreadable": []})
    assert unit_findings([log("m"), log(None)]) == []
    found = unit_findings([log("Depth (ft)")])
    assert found and found[0].startswith("depths printed in feet")
    bh = make()
    bh.issues = bh.validate_log() + found
    score(bh, {}, unreadable=0)
    assert bh.review_status == ReviewStatus.pending_review


def _sheet_logs(*sheets):
    return [(type("Log", (), {"sheet": s, "layers": []}), SourceRef(file="f", page=i))
            for i, s in enumerate(sheets, start=1)]


def test_missing_sheets_block_release():
    from pipeline.assemble import missing_sheets

    assert missing_sheets(_sheet_logs("1 of 3", "3 of 3")) == ["missing sheets: 2 of 3"]
    assert missing_sheets(_sheet_logs("Hoja 1 de 2")) == ["missing sheets: 2 of 2"]
    assert missing_sheets(_sheet_logs("Sheet 1/2", "Sheet 2/2")) == []
    assert missing_sheets(_sheet_logs(None, None)) == []
    assert missing_sheets(_sheet_logs("5 of 40", "6 of 40")) == []  # report page numbers, not this hole's sheets
    bh = make()
    bh.issues.append("missing sheets: 2 of 3")
    score(bh, {}, unreadable=0)
    assert bh.review_status == ReviewStatus.pending_review


def test_inferred_boundaries_are_reported():
    class Log:
        sheet = None
        layers = [_xlayer(None, 2.0), _xlayer(2.0, 5.0), _xlayer(5.0, None)]

    notes: list[str] = []
    layers = _layers([(Log, SourceRef(file="f", page=1))], final_depth=8.0, notes=notes)
    assert [(l.top_m, l.bottom_m) for l in layers] == [(0, 2), (2, 5), (5, 8)]
    assert notes == ["boundary inferred: top of the first layer not readable, set to 0 m",
                     "boundary inferred: bottom of the last layer not readable, set to the final depth 8 m"]


def test_core_runs_and_samples_are_checked():
    from schema.borehole import SPT, CoreRun, Sample

    bh = make()  # final depth 9 m
    bh.core_runs = [CoreRun(top_m=3, bottom_m=4.5, total_core_recovery_pct=60, solid_core_recovery_pct=50, rqd_pct=80),
                    CoreRun(top_m=4.5, bottom_m=6, total_core_recovery_pct=135),
                    CoreRun(top_m=6, bottom_m=7.5, total_core_recovery_pct=108)]  # core gain: plausible
    bh.samples = [Sample(name="S1", top_m=2), Sample(name="UD4", top_m=9.0, bottom_m=9.5)]
    bh.spt = [SPT(depth_m=10.5, raw="12-15-20")]
    issues = bh.validate_log()
    assert "core run 3–4.5 m: RQD 80% above TCR 60%" in issues
    assert "core run 4.5–6 m: TCR 135% outside 0–120%" in issues
    assert not any(i.startswith("core run 6–7.5 m") for i in issues)
    assert "sample UD4 at 9.5 m below final depth 9 m" in issues
    assert "SPT at 10.5 m below final depth 9 m" in issues
    assert not any("S1" in i or "SCR" in i for i in issues)


def test_final_depth_is_the_deepest_stated_over_all_sheets():
    from pipeline.assemble import build_borehole
    from pipeline.extraction_models import XLog
    def sheet(sheet, final, layers):
        return XLog.model_validate({**{k: None for k in XLog.model_fields}, "name": "BH1", "sheet": sheet,
                                    "final_depth_m": final, "layers": [_xlayer(t, b) for t, b in layers],
                                    "spt": [], "core_runs": [], "samples": [], "groundwater": [],
                                    "lab_results": [], "unreadable": []})
    logs = [(sheet("1 of 2", 8.0, [(0, 8)]), SourceRef(file="f", page=1)),
            (sheet("2 of 2", 10.5, [(8, 10.5)]), SourceRef(file="f", page=2))]
    bh, _ = build_borehole("d", "f", logs, {}, None)
    assert bh.depth_m == 10.5 and not any(i.startswith("layers end at") for i in bh.issues)


def test_unit_repeated_from_next_sheet_top_is_kept_once():
    sheets = _sheets([_xlayer(0, 5.75, "a"), _xlayer(5.75, 6.6, "SAND, medium - coarse")],
                     [_xlayer(6.0, 6.6, "SAND, medium - coarse"), _xlayer(6.6, 9.0, "clay", "clay")])
    notes: list[str] = []
    layers = _layers(sheets, final_depth=9, notes=notes)
    assert [(l.top_m, l.bottom_m) for l in layers] == [(0, 5.75), (5.75, 6.6), (6.6, 9.0)]
    assert len(notes) == 1


def test_small_discontinuities_do_not_block():
    bh = make()
    bh.layers[1].bottom_m = 4.45  # 5 cm not covered (core loss)
    bh.issues = bh.validate_log()
    assert any(i.startswith("minor discontinuity 4.45–4.5") for i in bh.issues)
    assert not any(i.startswith("gap") for i in bh.issues)
    bh.layers[1].bottom_m = 4.2  # 30 cm: a real gap
    assert any(i.startswith("gap 4.2–4.5") for i in bh.validate_log())


def test_next_sheet_shows_where_the_unit_ends():
    sheets = _sheets([_xlayer(0, 5.52, "sand"), _xlayer(5.52, 6.6, "LIMESTONE", "limestone")],
                     [_xlayer(5.52, 6.0, "LIMESTONE, muddy", "limestone"), _xlayer(6.0, 6.21, "FLINT", "unknown")])
    layers = _layers(sheets, final_depth=6.21)
    assert [(l.top_m, l.bottom_m) for l in layers] == [(0, 5.52), (5.52, 6.0), (6.0, 6.21)]


def test_core_loss_fills_only_gaps_the_recovery_explains():
    from pipeline.assemble import fill_core_loss
    from schema.borehole import CoreRun
    bh = make()
    bh.layers[1].bottom_m = 4.2  # 0.3 m gap in a 1.5 m run with 80 % recovery (0.3 m lost)
    bh.core_runs = [CoreRun(top_m=3.5, bottom_m=5.0, total_core_recovery_pct=80)]
    notes = fill_core_loss(bh)
    assert len(notes) == 1 and not any(i.startswith("gap") for i in bh.validate_log())
    assert [l.primary.value for l in bh.layers] == ["fill", "sand", "no_recovery", "clay"]

    bh = make()
    bh.layers[1].bottom_m = 3.8  # 0.7 m gap is more than the 0.3 m lost: stays a gap
    bh.core_runs = [CoreRun(top_m=3.5, bottom_m=5.0, total_core_recovery_pct=80)]
    assert fill_core_loss(bh) == [] and any(i.startswith("gap") for i in bh.validate_log())


def test_core_piece_offsets_are_not_checked_against_the_text_layer():
    src = SourceRef(file="demo.pdf", page=1)
    pieces = [Layer(top_m=t, bottom_m=b, description_original=f"{round((t - 6) * 100)} - {round((b - 6) * 100)} cm, LIMESTONE",
                    primary="limestone", source=src) for t, b in [(6.0, 6.21), (6.21, 6.5), (6.5, 6.93), (6.93, 7.4)]]
    bh = make(layers=pieces)
    text = "Core log " + "x" * 300 + " 0 - 21 cm 21 - 50 cm 50 - 93 cm 93 - 140 cm"
    assert text_agreement(bh, {("demo.pdf", 1): text}) is None  # nothing it could confirm


def test_log_without_printed_depths_is_not_penalised():
    bh = make()
    text = "Borehole log " + "x" * 300 + " Undr. shear strength 100 200 300 400 Job 1241009 Date 2012"
    score(bh, {("demo.pdf", 1): text}, unreadable=0)
    assert bh.confidence >= 0.85 and any("not printed" in r for r in bh.score_reasons)


def test_boreholes_and_test_pits_with_the_same_number_stay_apart():
    from pipeline.assemble import PageResult, assemble, hole_key
    from pipeline.extraction_models import PageExtraction, XLog

    assert hole_key("Test Pit No. 2", None) == hole_key("T.P. 2", None) == hole_key("2", "test_pit") == ("test_pit", "2")
    assert hole_key("BH-2", None) == ("borehole", "2") and hole_key("S-2", "borehole") == ("borehole", "S2")

    def page(n, kind, name, layers):
        log = XLog.model_validate({**{k: None for k in XLog.model_fields}, "name": name,
                                   "layers": [_xlayer(t, b) for t, b in layers], "spt": [], "core_runs": [],
                                   "samples": [], "groundwater": [], "lab_results": [], "unreadable": []})
        return PageResult("r", "r.pdf", n, kind, PageExtraction(logs=[log], coordinate_rows=[]))

    records = assemble([page(1, "borehole_log", "2", [(0, 0.3), (0.3, 6.0)]),
                        page(2, "test_pit_log", "Test Pit No. 2", [(0, 1.6), (1.6, 2.5)]),
                        page(3, "test_pit_log", "3", [(0, 2.0)])])
    by_id = {b.borehole_id: b for b in records}
    assert set(by_id) == {"r:2", "r:TESTPIT2", "r:3"}
    assert [l.bottom_m for l in by_id["r:2"].layers] == [0.3, 6.0]  # not merged with the test pit
    assert by_id["r:3"].investigation_type.value == "test_pit"


def test_defect_intervals_are_not_printed_boundaries():
    from pipeline.scoring import prints_depths
    bh = make()
    text = "x" * 300 + " 1.15-1.31m: joint 2.27-2.47m: joint UCS 2.10-2.22m 28.9 MPa SPT at 7.5m"
    assert not prints_depths(bh, {("demo.pdf", 1): text})
    assert prints_depths(bh, {("demo.pdf", 1): "x" * 300 + " 1.30 4.70 9.20"})


def test_release_rules_from_the_second_review():
    from schema.borehole import CoreRun
    page = ("demo.pdf", 1)
    # Integer depths printed, extraction reads other values: a contradiction, not "not printed".
    bh = make()
    text = "x" * 300 + " depth 1 2 3 4 5 6 7 8 9 SAND CLAY"
    score(bh, {page: text}, unreadable=0)
    assert bh.review_status == ReviewStatus.pending_review
    # Text confirms the first page only; the scanned second page must not ride along.
    src2 = SourceRef(file="demo.pdf", page=2)
    bh = make(layers=[Layer(top_m=0, bottom_m=1.2, description_original="Fill", primary="fill", source=SourceRef(file="demo.pdf", page=1)),
                      Layer(top_m=1.2, bottom_m=4.5, description_original="Sand", primary="sand", source=SourceRef(file="demo.pdf", page=1)),
                      Layer(top_m=4.5, bottom_m=6.0, description_original="Clay", primary="clay", source=src2),
                      Layer(top_m=6.0, bottom_m=7.5, description_original="Clay", primary="clay", source=src2),
                      Layer(top_m=7.5, bottom_m=9.0, description_original="Clay", primary="clay", source=src2)])
    bh.issues = bh.validate_log()
    score(bh, {page: "x" * 300 + " 1.20 4.50"}, unreadable=0)
    assert bh.review_status == ReviewStatus.pending_review
    # Contradictory core runs and inferred boundaries cap below release.
    for issue in ("core run 3–4.5 m: RQD 80% above TCR 60%", "boundary inferred: top of the first layer not readable, set to 0 m"):
        bh = make()
        bh.issues = bh.validate_log() + [issue]
        score(bh, {}, unreadable=0)
        assert bh.review_status == ReviewStatus.pending_review, issue


def test_small_sheet_totals_are_always_checked():
    from pipeline.assemble import missing_sheets
    def log(sheet):
        return (type("L", (), {"sheet": sheet})(), SourceRef(file="f", page=1))
    assert missing_sheets([log("1 of 3")]) == ["missing sheets: 2, 3 of 3"]
    assert missing_sheets([log("Page 2 of 20")]) == []  # report pagination


def test_stale_approval_is_refused_and_lab_values_are_part_of_the_review(con):
    from schema.borehole import LabResult
    bh = make()
    score(bh, {}, 0)
    store.save_borehole(con, bh)
    seen = con.execute("SELECT content_hash FROM boreholes WHERE id = ?", (bh.borehole_id,)).fetchone()[0]
    store.record_review(con, bh.borehole_id, ReviewStatus.approved, "Anna", None, expected_hash=seen)

    rerun = make()
    rerun.lab_results = [LabResult(parameter="ucs", value=100.0, value_raw="100 MPa")]  # was not there when approved
    score(rerun, {}, 0)
    store.save_borehole(con, rerun)
    assert store.load_borehole(con, bh.borehole_id).review_status == ReviewStatus.pending_review
    with pytest.raises(store.StaleReview):  # a browser still showing the old version cannot approve
        store.record_review(con, bh.borehole_id, ReviewStatus.approved, "Anna", None, expected_hash=seen)


def test_merge_rules_keep_contradictions_and_core_loss_has_a_budget():
    from pipeline.assemble import fill_core_loss
    from schema.borehole import CoreRun
    # Page 2 says limestone from 0 m where page 1 had sand and clay: kept as an overlap, not clipped away.
    sheets = _sheets([_xlayer(0, 5, "sand"), _xlayer(5, 10, "clay", "clay")], [_xlayer(0, 15, "LIMESTONE", "limestone")])
    layers = _layers(sheets, final_depth=15)
    assert (0, 15) in [(l.top_m, l.bottom_m) for l in layers]
    # The same interval read with another material is kept for a person to decide.
    sheets = _sheets([_xlayer(0, 5, "sand")], [_xlayer(0, 5, "CLAY", "clay")])
    assert len(_layers(sheets, final_depth=5)) == 2
    # 10 % of a 10 m run is lost (1 m): two gaps of 0.8 m cannot both be core loss.
    bh = make(layers=[Layer(top_m=0, bottom_m=3, description_original="a", primary="sandstone"),
                      Layer(top_m=3.8, bottom_m=6, description_original="b", primary="sandstone"),
                      Layer(top_m=6.8, bottom_m=10, description_original="c", primary="sandstone")])
    bh.depth_m = 10
    bh.core_runs = [CoreRun(top_m=0, bottom_m=10, total_core_recovery_pct=90)]
    assert len(fill_core_loss(bh)) == 1
    assert any(i.startswith("gap 6.0–6.8") for i in bh.validate_log())


def test_document_with_conflicting_crs_statements_gets_none():
    from pipeline.assemble import PageResult, document_crs
    pages = [PageResult("d", "d.pdf", 1, "text", None, "All coordinates MGA94 Zone 56"),
             PageResult("d", "d.pdf", 2, "text", None, "Coordinates in MGA2020 Zone 56 " * 3)]  # one page, repeated
    assert document_crs(pages) is None  # 1 page each: comparable support, a person decides
    pages.append(PageResult("d", "d.pdf", 3, "text", None, "Grid: MGA94 Zone 56"))
    pages.append(PageResult("d", "d.pdf", 4, "text", None, "Grid: MGA94 Zone 56"))
    assert "MGA94" in document_crs(pages)[0]


def test_toughone_patterns():
    from pipeline.assemble import extend_sampled_layers, hole_key
    assert hole_key("Hull nr. 7", None) == hole_key("7", "borehole") == ("borehole", "7")
    assert hole_key("CPT 1", None) == ("probe", "1") != hole_key("BH 1", None)
    # Last row with no thickness: what the hand auger hit, not a layer.
    sheets = _sheets([_xlayer(0, 0.8, "sandy CLAY", "clay"), _xlayer(0.8, 1.6, "silt", "silt"), _xlayer(1.6, 1.6, "Hard rock, refusal", "unknown")])
    notes: list[str] = []
    assert [(l.top_m, l.bottom_m) for l in _layers(sheets, final_depth=1.6, notes=notes)] == [(0, 0.8), (0.8, 1.6)]
    assert notes and notes[0].startswith("reached at the final depth 1.6 m")
    # Sample-wise description: 12.0-12.45 described, next description at 13.5.
    bh = make(layers=[Layer(top_m=10.5, bottom_m=12.0, description_original="a", primary="clay"),
                      Layer(top_m=12.0, bottom_m=12.45, description_original="b", primary="silt"),
                      Layer(top_m=13.5, bottom_m=15.0, description_original="c", primary="silt")])
    assert len(extend_sampled_layers(bh)) == 1 and bh.layers[1].bottom_m == 13.5
