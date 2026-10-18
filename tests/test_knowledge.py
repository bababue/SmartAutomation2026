"""Knowledge from reviewers: local grids from control points, terms, learned CRS, prompt context (synthetic)."""

import math

import pytest
from pyproj import Transformer

from pipeline import config, corrections, knowledge, store
from pipeline.knowledge import fit_grid, grid_position
from pipeline.georef import haversine_km
from pipeline.scoring import score
from schema.borehole import Borehole, GeorefMethod, Layer, Location, SourceRef

# A made-up local grid: metres from an origin in Buenos Aires, rotated by 3 degrees (like a city grid).
ORIGIN = (-34.6037, -58.3816)
TO_LOCAL = Transformer.from_crs(4326, f"+proj=aeqd +lat_0={ORIGIN[0]} +lon_0={ORIGIN[1]} +datum=WGS84", always_xy=True)


def local_coords(lat, lon):
    import math
    x, y = TO_LOCAL.transform(lon, lat)
    r = math.radians(3)
    return 50000 + x * math.cos(r) - y * math.sin(r), 30000 + x * math.sin(r) + y * math.cos(r)


HOLES = [(-34.660 + i * 0.002, -58.470 + i * 0.0015) for i in range(6)]


def test_grid_from_reviewer_positions_places_the_other_holes():
    pts = [(local_coords(*ll), ll) for ll in HOLES[:3]]
    grid = fit_grid(pts)
    assert grid and grid["n"] == 3 and grid["rms_m"] < 1
    for ll in HOLES[3:]:
        lat, lon = grid_position(grid, local_coords(*ll))
        assert haversine_km(lat, lon, *ll) < 0.01


def test_inconsistent_control_points_are_rejected():
    pts = [(local_coords(*ll), ll) for ll in HOLES[:3]]
    pts[2] = (pts[2][0], (HOLES[2][0] + 0.05, HOLES[2][1]))  # one position set 5 km off
    assert fit_grid(pts) is None


def hole(bid, ll, coords, project="Line 2"):
    src = SourceRef(file="r.pdf", page=1)
    bh = Borehole(borehole_id=bid, borehole_name=bid.split(":")[1], project_name=project, depth_m=6,
                  layers=[Layer(top_m=0, bottom_m=2, description_original="FILL", primary="fill", source=src),
                          Layer(top_m=2, bottom_m=6, description_original="CALICHE, hard", primary="unknown", source=src)],
                  location=Location(coords_raw=coords, lat=ll[0] + 0.01, lon=ll[1], method=GeorefMethod.geocoded_place,
                                    confidence=0.3, accuracy_m=3000, country="AR"),
                  sources=[src])
    bh.issues = bh.validate_log() + ["printed coordinates could not be matched to the named place",
                                     "position from place name only"]
    score(bh, {}, 0)
    return bh


def test_apply_all_uses_grid_terms_and_corrections_win(con):
    records = [hole(f"r:S{i}", ll, "X = {:.1f} Y = {:.1f}".format(*local_coords(*ll))) for i, ll in enumerate(HOLES)]
    for bh, ll in zip(records[:3], HOLES[:3]):  # a reviewer placed three holes by hand
        store.add_correction(con, "borehole", bh.borehole_id, "position", {"lat": ll[0], "lon": ll[1]}, "Ana", None)
    store.add_knowledge(con, "project", "line2", "term", {"term": "caliche", "primary": "limestone"}, "external",
                        "site engineer on the phone", "Ana")
    knowledge.apply_all(con, records)
    s3, s5 = records[3], records[5]
    assert s3.location.method == GeorefMethod.stated_coordinates and haversine_km(s3.location.lat, s3.location.lon, *HOLES[3]) < 0.02
    assert any(i.startswith("position from a local grid fitted to 3") for i in s3.issues)
    assert not any(i.startswith("position from place name only") for i in s3.issues)
    assert s3.location.confidence == 0.9  # each control point is recovered from the other two
    assert s5.location.method == GeorefMethod.geocoded_place  # four spacings beyond the control points: no extrapolation
    assert s3.layers[1].primary.value == "limestone"  # 'CALICHE, hard' names the main material
    # The reviewer's own correction still wins on that record.
    corrections.apply(records[0], store.corrections_for(con, records[0].borehole_id))
    assert records[0].location.method == GeorefMethod.manual


def test_learned_crs_needs_confirmations_to_count_as_stated(con):
    lat, lon = -33.8568, 151.2153
    e, n = Transformer.from_crs(4326, 28356, always_xy=True).transform(lon, lat)
    bh = hole("au:BH1", (lat, lon), f"E {e:.1f} N {n:.1f}")
    bh.location.country = "AU"
    item = store.add_knowledge(con, "region", "AU", "crs", {"epsg": 28356}, "derived", None, "Ana")
    notes = knowledge.apply(bh, knowledge.applicable([item], bh))
    assert bh.location.crs_epsg == 28356 and bh.location.confidence == 0.8 and notes
    for _ in range(3):  # the same document again proves nothing
        item = store.add_knowledge(con, "region", "AU", "crs", {"epsg": 28356}, "derived", None, "Ana", evidence="document:a")
    assert item["confirmations"] == 2
    for doc in ("b", "c"):
        item = store.add_knowledge(con, "region", "AU", "crs", {"epsg": 28356}, "derived", None, "Ana", evidence=f"document:{doc}")
    assert item["confirmations"] == 4
    bh2 = hole("au:BH2", (lat, lon), f"E {e:.1f} N {n:.1f}")
    bh2.location.country = "AU"
    knowledge.apply(bh2, knowledge.applicable([item], bh2))
    assert bh2.location.confidence == 0.9


def test_prompt_context_for_new_pages(con):
    store.save_borehole(con, hole("r:S1", HOLES[0], "X = 1 Y = 2"))
    store.add_knowledge(con, "region", "AR", "note", {"text": "Depths are below the kerb"}, "external", None, "Ana")
    store.add_knowledge(con, "region", "DK", "note", {"text": "Not for Argentina"}, "review", None, "Ana")
    text = knowledge.prompt_context(con, "r")
    assert "below the kerb" in text and "told by someone" in text and "Argentina" not in text
    assert knowledge.prompt_context(con, "unknown-doc") == ""



def test_collapsed_control_points_are_not_a_grid():
    pts = [(local_coords(*ll), HOLES[0]) for ll in HOLES[:3]]  # three holes, all set to the same spot
    assert fit_grid(pts) is None


FROM_LOCAL = Transformer.from_crs(TO_LOCAL.target_crs, 4326, always_xy=True)


def at_local(x, y):
    lon, lat = FROM_LOCAL.transform(x, y)
    return lat, lon


def is_helmert(grid):
    c = grid["params"]
    return c[0] == pytest.approx(c[4]) and c[1] == pytest.approx(-c[3])


def test_control_points_along_a_route_get_a_helmert_grid():
    # Six holes along an 8 km route, a few metres off its axis, each set by hand within about 10 m.
    heading, pts = math.radians(30), []
    errors = [(7, -5), (-8, 7), (6, 8), (-7, -8), (9, 5), (-5, -9)]
    for i, (ex, ey) in enumerate(errors):
        along, across = -4000 + i * 1600, (-3, 4, -2, 5, -4, 2)[i]
        x = along * math.sin(heading) + across * math.cos(heading)
        y = along * math.cos(heading) - across * math.sin(heading)
        pts.append((local_coords(*at_local(x, y)), at_local(x + ex, y + ey)))
    grid = fit_grid(pts)
    assert is_helmert(grid) and grid["loo_m"] <= 20
    assert fit_grid(pts, model="affine")["loo_m"] > 20  # cross-track terms fitted to the noise


SPREAD = [(-1000, -1000), (1000, -1000), (-1000, 1000), (1000, 1000), (0, 0), (300, -600)]


def test_well_spread_control_points_on_a_sheared_grid_stay_affine():
    def sheared(x, y):
        return 50000 + x + 0.03 * y, 30000 + 0.99 * y
    errors = [(2, -1), (-1, 2), (1, 1), (-2, -1), (1, -2), (-1, 1)]
    pts = [(sheared(x, y), at_local(x + ex, y + ey)) for (x, y), (ex, ey) in zip(SPREAD, errors)]
    grid = fit_grid(pts)
    assert not is_helmert(grid) and grid["loo_m"] <= 20
    assert haversine_km(*grid_position(grid, sheared(500, 700)), *at_local(500, 700)) < 0.005
    assert fit_grid(pts, model="helmert")["loo_m"] > 20


def test_well_spread_control_points_on_a_similar_grid_stay_helmert():
    # Affine fits these noisier positions more closely but predicts held-out holes worse.
    errors = [(6, -4), (-5, 7), (4, 5), (-7, -3), (3, -6), (-4, 4)]
    pts = [(local_coords(*at_local(x, y)), at_local(x + ex, y + ey)) for (x, y), (ex, ey) in zip(SPREAD, errors)]
    grid = fit_grid(pts)
    assert is_helmert(grid) and grid["loo_m"] <= 20
    assert fit_grid(pts, model="affine")["rms_m"] < grid["rms_m"]


def test_term_rule_does_not_override_a_named_main_material(con):
    src = SourceRef(file="r.pdf", page=1)
    bh = Borehole(borehole_id="r:S1", borehole_name="S1", location=Location(),
                  layers=[Layer(top_m=0, bottom_m=2, description_original="SAND, trace of caliche", primary="sand", source=src),
                          Layer(top_m=2, bottom_m=3, description_original="Caliche: hard", primary="sand", source=src)])
    rule = {"kind": "term", "scope_type": "global", "scope_value": None, "confirmations": 1,
            "payload": {"term": "caliche", "primary": "limestone"}}
    knowledge.apply(bh, [rule])
    assert [l.primary.value for l in bh.layers] == ["sand", "limestone"]


def test_term_rule_reads_the_main_material_after_a_core_run_offset(con):
    src = SourceRef(file="r.pdf", page=1)
    bh = Borehole(borehole_id="r:K1", borehole_name="K1", location=Location(),
                  layers=[Layer(top_m=5, bottom_m=5.2, description_original="0 - 20 cm, FLINT, grey", primary="quartzite", source=src),
                          Layer(top_m=5.2, bottom_m=6, description_original="20 - 100 cm, CHALK, some flint", primary="chalk", source=src)])
    rule = {"kind": "term", "scope_type": "global", "scope_value": None, "confirmations": 1,
            "payload": {"term": "flint", "primary": "limestone"}}
    knowledge.apply(bh, [rule])
    assert [l.primary.value for l in bh.layers] == ["limestone", "chalk"]


def test_confirmed_crs_preserves_independent_notation_doubts():
    from pipeline.georef import georeference
    geo = georeference("N 48,12,345 E 8,03,210", None, "DE", 48.20575, 8.0535, 3)
    bh = hole("r:BH1", (48.20575, 8.0535), "N 48,12,345 E 8,03,210")
    bh.location, bh.issues = geo.location, geo.issues
    assert bh.location.confidence == 0.8
    notes = knowledge.apply(bh, [{"kind": "crs", "scope_type": "region", "confirmations": 3,
                                 "payload": {"epsg": 4326}}])
    bh.issues += notes
    score(bh, {}, 0)
    assert bh.location.confidence == 0.8 and bh.location.accuracy_m > 400
    assert bh.review_status.value == "pending_review"
    assert any(i.startswith("coordinate notation or datum ambiguous") for i in bh.issues)


def freiburg_hole(*issues):
    from pipeline.georef import georeference
    lat, lon = 47.9955, 7.8529  # Freiburg Minster
    e, n = Transformer.from_crs(4326, 25832, always_xy=True).transform(lon, lat)
    geo = georeference(f"E {e:.0f} N {n:.0f}", None, "DE", lat, lon, 3)
    bh = hole("de:BH1", (lat, lon), f"E {e:.0f} N {n:.0f}")
    bh.location, bh.issues = geo.location, geo.issues + list(issues)
    assert bh.location.confidence < 0.9 and any(i.startswith("coordinate system inferred") for i in bh.issues)
    notes = knowledge.apply(bh, [{"kind": "crs", "scope_type": "region", "confirmations": 3, "payload": {"epsg": 25832}}])
    bh.issues += notes
    score(bh, {}, 0)
    return bh


def test_trusted_learned_crs_settles_inferred_and_ambiguous_systems():
    bh = freiburg_hole("CRS ambiguous: EPSG:25832, EPSG:32632")
    assert bh.location.crs_epsg == 25832 and bh.location.confidence == 0.9
    assert not any(i.startswith(("coordinate system inferred", "CRS ambiguous")) for i in bh.issues)
    assert bh.review_status.value == "auto_approved"


@pytest.mark.parametrize("finding", [
    "coordinates read differently on a second look: 'E 413000 N 5316900', 900 m from the first reading",
    "coordinate system read differently on a second look: 'Gauss-Krueger zone 3'",
    "coordinates not found on a second look",
])
def test_learned_crs_keeps_a_second_reading_that_disagrees(finding):
    bh = freiburg_hole(finding)
    assert finding in bh.issues
    assert bh.review_status.value == "pending_review"


@pytest.mark.parametrize("threshold", [0.8, 0.95])
def test_learned_crs_confidence_follows_the_release_threshold(threshold, monkeypatch):
    from pipeline.georef import georeference
    monkeypatch.setattr(config, "AUTO_APPROVE_THRESHOLD", threshold)
    lat, lon = 47.9955, 7.8529  # Freiburg Minster
    e, n = Transformer.from_crs(4326, 25832, always_xy=True).transform(lon, lat)
    for confirmations, released in ((3, True), (1, False)):
        geo = georeference(f"E {e:.0f} N {n:.0f}", None, "DE", lat, lon, 3)
        bh = hole("de:BH1", (lat, lon), f"E {e:.0f} N {n:.0f}")
        bh.location, bh.issues = geo.location, geo.issues
        bh.location.confidence = 0.75  # below either threshold: the learned system is asked
        bh.issues += knowledge.apply(bh, [{"kind": "crs", "scope_type": "region", "confirmations": confirmations,
                                           "payload": {"epsg": 25832}}])
        score(bh, {}, 0)
        assert bh.location.crs_epsg == 25832
        assert bh.location.confidence == (threshold if released else round(threshold - 0.1, 2))
        assert (bh.review_status.value == "auto_approved") is released
        assert any(f.part == "position" and f.effect == "negative" for f in bh.score_factors) is not released
