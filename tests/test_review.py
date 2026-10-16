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
        log = type("Log", (), {"layers": layers})
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


def test_inferred_crs_is_explained_in_review_reasons():
    bh = make(position=0.8)
    bh.issues.append("coordinate system inferred, not stated: EPSG:32632")
    score(bh, {}, unreadable=0)
    assert bh.review_status == ReviewStatus.pending_review
    assert bh.score_reasons == ["Coordinate system inferred, not stated: EPSG:32632"]


def test_scientific_notation():
    assert _num("1.2E-6") == pytest.approx(1.2e-6)
    assert _num("3,5x10-7") == pytest.approx(3.5e-7)


@pytest.fixture()
def con(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store.connect, "__defaults__", (path,))
    c = store.connect(path)
    yield c
    c.close()


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
