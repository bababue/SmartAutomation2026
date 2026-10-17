"""Reviewer corrections: applied via the API, kept across re-runs, flagged when stale (synthetic data)."""

import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer

from api.main import app
from pipeline import config, corrections, store
from pipeline.scoring import score
from schema.borehole import SPT, Borehole, CoreRun, GeorefMethod, LabResult, Layer, Location, SourceRef

# Sydney landmark, MGA94 zone 56, coordinates printed without a CRS
LAT, LON = -33.8568, 151.2153
E, N = Transformer.from_crs(4326, 28356, always_xy=True).transform(LON, LAT)


def record(bid: str, east: float = E) -> Borehole:
    src = SourceRef(file="demo.pdf", page=1)
    bh = Borehole(
        borehole_id=bid, borehole_name=bid.split(":")[1], depth_m=9.0,
        layers=[Layer(top_m=0, bottom_m=1.2, description_original="Fill", primary="fill", source=src),
                Layer(top_m=1.2, bottom_m=4.0, description_original="Sand", primary="sand", source=src),
                Layer(top_m=4.5, bottom_m=9.0, description_original="Clay", primary="unknown", source=src)],
        location=Location(coords_raw=f"E {east:.1f} N {N:.1f}", lat=LAT + 0.01, lon=LON, crs_epsg=32756,
                          method=GeorefMethod.stated_coordinates, confidence=0.8),
        sources=[src],
    )
    bh.issues = bh.validate_log() + ["coordinate system inferred, not stated: EPSG:32756",
                                     "unreadable: Coordinate reference system not printed",
                                     "unreadable: Elevation datum not printed"]
    score(bh, {}, 0)
    return bh


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    store.migrate(path)
    con = store.connect(path)
    for bid, east in [("demo__a:BH1", E), ("demo__b:BH2", E + 50), ("other:BH3", E)]:
        store.save_borehole(con, record(bid, east))
    con.close()
    return TestClient(app)


def post(client, bid, **body):
    return client.post(f"/api/boreholes/{bid}/corrections", json={"reviewer": "Anna", **body})


def test_crs_for_whole_document(client):
    r = post(client, "demo__a:BH1", kind="crs", scope="document", payload={"epsg": 28356})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == ["demo__a:BH1", "demo__b:BH2"]  # same folder, not 'other'
    loc = r.json()["borehole"]["location"]
    assert loc["crs_epsg"] == 28356 and loc["confidence"] == 1.0
    assert abs(loc["lat"] - LAT) < 1e-4
    issues = r.json()["borehole"]["issues"]
    assert not any(i.startswith("coordinate system inferred") for i in issues)
    assert any(i.startswith("corrected by Anna") for i in issues)
    assert "unreadable: Coordinate reference system not printed" not in issues  # settled by the reviewer
    assert "unreadable: Elevation datum not printed" in issues  # not about the position
    other = client.get("/api/boreholes/other:BH3").json()["borehole"]["location"]
    assert other["crs_epsg"] == 32756


def test_layer_fix_closes_gap_and_survives_rerun(client):
    r = post(client, "demo__a:BH1", kind="layer",
             payload={"orig_top": 1.2, "orig_bottom": 4.0, "top": 1.2, "bottom": 4.5, "primary": "fine_sand"})
    assert r.status_code == 200, r.text
    bh = r.json()["borehole"]
    assert not any(i.startswith("gap") for i in bh["issues"])
    assert bh["layers"][1]["bottom_m"] == 4.5 and bh["layers"][1]["primary"] == "fine_sand"
    assert "coordinate system inferred, not stated: EPSG:32756" in bh["issues"]  # unrelated findings stay

    # A re-run assembles the uncorrected record again; the stored correction is re-applied.
    con = store.connect()
    rerun = record("demo__a:BH1")
    corrections.apply(rerun, store.corrections_for(con, rerun.borehole_id))
    assert rerun.layers[1].bottom_m == 4.5 and not any(i.startswith("gap") for i in rerun.issues)


def test_earlier_correction_notes_stay(client):
    post(client, "demo__a:BH1", kind="layer", payload={"orig_top": 4.5, "orig_bottom": 9.0, "primary": "clay"})
    r = post(client, "demo__a:BH1", kind="crs", payload={"epsg": 28356})
    notes = [i for i in r.json()["borehole"]["issues"] if i.startswith("corrected by")]
    assert len(notes) == 2


def test_stale_correction_blocks_release(client):
    con = store.connect()
    post(client, "demo__a:BH1", kind="layer", payload={"orig_top": 4.5, "orig_bottom": 9.0, "primary": "clay"})
    rerun = record("demo__a:BH1")
    rerun.layers[2].top_m = 4.6  # new extraction reads the boundary differently
    corrections.apply(rerun, store.corrections_for(con, rerun.borehole_id))
    assert any(i.startswith("stale correction") for i in rerun.issues)
    assert rerun.review_status.value == "pending_review"


def test_layers_added_for_a_missing_sheet_go_stale_when_the_sheet_is_extracted():
    # The reviewer typed in a sheet the extraction lacked: extended the last unit and added one below it.
    fixes = [{"kind": "layer", "reviewer": "Anna", "payload": {"orig_top": 4.5, "orig_bottom": 9.0, "top": 4.5,
                                                               "bottom": 10.0}},
             {"kind": "layer_add", "reviewer": "Anna", "payload": {"top": 10.0, "bottom": 12.0, "primary": "gravel"}},
             {"kind": "layer_add", "reviewer": "Anna", "payload": {"top": 12.0, "bottom": 13.0, "primary": "sandstone"}}]
    rerun = record("demo__a:BH1")
    src = SourceRef(file="demo.pdf", page=2)
    rerun.layers += [Layer(top_m=9.0, bottom_m=10.1, description_original="Clay", primary="clay", source=src),
                     Layer(top_m=10.1, bottom_m=12.0, description_original="Gravel", primary="gravel", source=src),
                     Layer(top_m=12.0, bottom_m=13.0, description_original="Rock", primary="unknown", source=src)]
    assert corrections.apply(rerun, fixes) == [False, False, True]  # the same interval: the reviewer's reading
    assert [(l.top_m, l.bottom_m) for l in rerun.layers][2:] == [(4.5, 9.0), (9.0, 10.1), (10.1, 12.0), (12.0, 13.0)]
    assert rerun.layers[-1].primary.value == "sandstone"
    assert sum(i.startswith("stale correction") for i in rerun.issues) == 2
    assert rerun.review_status.value == "pending_review"


def test_invalid_corrections_are_rejected(client):
    assert post(client, "demo__a:BH1", kind="crs", payload={"epsg": 1}).status_code == 422
    assert post(client, "demo__a:BH1", kind="position", scope="document", payload={"lat": 1, "lon": 2}).status_code == 422
    assert post(client, "demo__a:BH1", kind="layer", payload={"orig_top": 0, "orig_bottom": 1.2, "top": 2, "bottom": 1}).status_code == 422
    assert post(client, "demo__a:BH1", kind="layer", payload={"orig_top": 7, "orig_bottom": 8, "delete": True}).status_code == 422
    assert client.get("/api/boreholes/demo__a:BH1").json()["corrections"] == []


def test_fill_in_missing_data(client):
    bid = "demo__a:BH1"
    assert post(client, bid, kind="field", payload={"field": "surface_elevation_m", "value": "35,7".replace(",", ".")}).status_code == 200
    assert post(client, bid, kind="field", payload={"field": "start_date", "value": "2014-08-05"}).status_code == 200
    r = post(client, bid, kind="layer_add", payload={"top": 4.0, "bottom": 4.5, "primary": "silt",
                                                      "description_original": "SILT, grey"})
    assert r.status_code == 200, r.text
    bh = r.json()["borehole"]
    assert bh["surface_elevation_m"] == 35.7 and bh["start_date"] == "2014-08-05"
    assert [(l["top_m"], l["primary"]) for l in bh["layers"]][2] == (4.0, "silt")
    assert not any(i.startswith("gap") for i in bh["issues"])  # the added layer closes the gap
    r = post(client, bid, kind="layer", payload={"orig_top": 4.5, "orig_bottom": 9.0, "stratigraphic_code": "qh",
                                                  "description_original": "CLAY, stiff"})
    layer = r.json()["borehole"]["layers"][3]
    assert layer["stratigraphic_code"] == "qh" and layer["description_original"] == "CLAY, stiff"
    r = post(client, bid, kind="groundwater", payload={"depth_m": 2.4})
    assert [g["depth_m"] for g in r.json()["borehole"]["groundwater"]] == [2.4]
    assert post(client, bid, kind="field", payload={"field": "lat", "value": 1}).status_code == 422
    assert post(client, bid, kind="groundwater", payload={}).status_code == 422

    # All of it survives a re-run of the uncorrected record.
    con = store.connect()
    rerun = record(bid)
    assert all(corrections.apply(rerun, store.corrections_for(con, bid)))
    assert rerun.surface_elevation_m == 35.7 and len(rerun.layers) == 4 and rerun.groundwater[0].depth_m == 2.4


def test_crs_groups_offer_one_confirmation_per_document(client):
    groups = client.get("/api/review-queue/crs-groups").json()
    # demo__a and demo__b form the 'demo' document (2 holes); 'other' has a single hole.
    assert [(g["group"], g["epsg"], g["count"]) for g in groups] == [("demo", 32756, 2)]
    assert client.get("/api/boreholes/demo__b:BH2").json()["crs_group"]["group"] == "demo"
    assert client.get("/api/boreholes/other:BH3").json()["crs_group"] is None
    r = post(client, groups[0]["borehole_id"], kind="crs", scope="document", payload={"epsg": 28356})
    assert r.json()["updated"] == ["demo__a:BH1", "demo__b:BH2"]
    assert client.get("/api/review-queue/crs-groups").json() == []


def test_crs_confirmation_becomes_knowledge_for_the_country(client):
    con = store.connect()
    con.execute("UPDATE boreholes SET data = json_set(data, '$.location.country', 'AU')")
    con.commit()
    post(client, "demo__a:BH1", kind="crs", scope="document", payload={"epsg": 28356})
    items = store.active_knowledge(store.connect())
    assert [(k["scope_type"], k["scope_value"], k["kind"], k["payload"]) for k in items] == [("region", "AU", "crs", {"epsg": 28356})]


def test_test_results_and_notes(client):
    bid = "demo__a:BH1"
    con = store.connect()
    bh = store.load_borehole(con, bid)
    bh.spt = [SPT(depth_m=1.5, n_value=12), SPT(depth_m=3.0, refusal=True)]
    bh.core_runs = [CoreRun(top_m=4.5, bottom_m=6.0, total_core_recovery_pct=80)]
    bh.lab_results = [LabResult(parameter="water_content", value=21.0, top_m=2.0, qualifier="<")]
    store.save_borehole(con, bh)
    assert post(client, bid, kind="test", payload={"test": "spt", "orig_depth": 3.0, "value": 14}).status_code == 200
    assert post(client, bid, kind="test", payload={"test": "core", "orig_depth": 4.5, "value": 90, "rqd": 55}).status_code == 200
    assert post(client, bid, kind="test", payload={"test": "lab", "parameter": "water_content", "orig_depth": 2.0,
                                                    "delete": True}).status_code == 200
    r = post(client, bid, kind="field", payload={"field": "notes", "value": "Casing to 3 m."})
    out = r.json()["borehole"]
    assert [(s["depth_m"], s["n_value"], s["refusal"]) for s in out["spt"]] == [(1.5, 12, False), (3.0, 14, False)]
    assert (out["core_runs"][0]["total_core_recovery_pct"], out["core_runs"][0]["rqd_pct"]) == (90, 55)
    assert out["lab_results"] == [] and out["notes"] == "Casing to 3 m."
    # wrong depth, missing parameter, fractional N, delete of a result that is gone
    assert post(client, bid, kind="test", payload={"test": "spt", "orig_depth": 2.0, "value": 5}).status_code == 422
    assert post(client, bid, kind="test", payload={"test": "lab", "orig_depth": 2.0, "value": 5}).status_code == 422
    assert post(client, bid, kind="test", payload={"test": "spt", "orig_depth": 1.5, "value": 5.5}).status_code == 422
    assert post(client, bid, kind="test", payload={"test": "lab", "parameter": "water_content", "orig_depth": 2.0,
                                                    "delete": True}).status_code == 422
