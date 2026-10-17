"""Knowledge from reviewers: local grids from control points, terms, learned CRS, prompt context (synthetic)."""

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


@pytest.fixture()
def con(tmp_path, monkeypatch):
    path = tmp_path / "k.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store.connect, "__defaults__", (path,))
    c = store.connect(path)
    yield c
    c.close()


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


def test_term_rule_does_not_override_a_named_main_material(con):
    src = SourceRef(file="r.pdf", page=1)
    bh = Borehole(borehole_id="r:S1", borehole_name="S1", location=Location(),
                  layers=[Layer(top_m=0, bottom_m=2, description_original="SAND, trace of caliche", primary="sand", source=src),
                          Layer(top_m=2, bottom_m=3, description_original="Caliche: hard", primary="sand", source=src)])
    rule = {"kind": "term", "scope_type": "global", "scope_value": None, "confirmations": 1,
            "payload": {"term": "caliche", "primary": "limestone"}}
    knowledge.apply(bh, [rule])
    assert [l.primary.value for l in bh.layers] == ["sand", "limestone"]
