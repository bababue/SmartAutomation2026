"""Reviewer corrections: applied via the API, kept across re-runs, flagged when stale (synthetic data)."""

import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer

from api.main import app
from pipeline import config, corrections, store
from pipeline.scoring import score
from schema.borehole import Borehole, GeorefMethod, Layer, Location, SourceRef

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
    monkeypatch.setattr(store.connect, "__defaults__", (path,))
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
    other = client.get("/api/boreholes/other:BH3/detail").json()["borehole"]["location"]
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


def test_invalid_corrections_are_rejected(client):
    assert post(client, "demo__a:BH1", kind="crs", payload={"epsg": 1}).status_code == 422
    assert post(client, "demo__a:BH1", kind="position", scope="document", payload={"lat": 1, "lon": 2}).status_code == 422
    assert post(client, "demo__a:BH1", kind="layer", payload={"orig_top": 0, "orig_bottom": 1.2, "top": 2, "bottom": 1}).status_code == 422
    assert post(client, "demo__a:BH1", kind="layer", payload={"orig_top": 7, "orig_bottom": 8, "delete": True}).status_code == 422
    assert client.get("/api/boreholes/demo__a:BH1/detail").json()["corrections"] == []


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
    r = post(client, groups[0]["borehole_id"], kind="crs", scope="document", payload={"epsg": 28356})
    assert r.json()["updated"] == ["demo__a:BH1", "demo__b:BH2"]
    assert client.get("/api/review-queue/crs-groups").json() == []


def test_knowledge_api(client, monkeypatch):
    import api.main as api
    started = []
    monkeypatch.setattr(api, "_rebuild_in_background", lambda docs: started.append(sorted(docs)) or len(docs))
    r = client.post("/api/knowledge", json={"borehole_id": "demo__a:BH1", "scope_type": "document", "kind": "term",
                                             "payload": {"term": "caliche", "primary": "limestone"},
                                             "source": "external", "note": "phone call with the driller", "reviewer": "Ana"})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["scope_value"] == "demo" and started == [["demo__a", "demo__b"]]
    listed = client.get("/api/knowledge", params={"borehole_id": "demo__b:BH2"}).json()
    assert [k["kind"] for k in listed["items"]] == ["term"] and listed["scopes"]["document"] == "demo"
    assert client.get("/api/knowledge", params={"borehole_id": "other:BH3"}).json()["items"] == []
    bad = {"borehole_id": "demo__a:BH1", "scope_type": "document", "kind": "crs", "payload": {"epsg": 1}, "reviewer": "Ana"}
    assert client.post("/api/knowledge", json=bad).status_code == 422
    item_id = r.json()["item"]["id"]
    assert client.delete(f"/api/knowledge/{item_id}").status_code == 200
    assert client.get("/api/knowledge").json()["items"] == []


def test_crs_confirmation_becomes_knowledge_for_the_country(client):
    con = store.connect()
    con.execute("UPDATE boreholes SET data = json_set(data, '$.location.country', 'AU')")
    con.commit()
    post(client, "demo__a:BH1", kind="crs", scope="document", payload={"epsg": 28356})
    items = client.get("/api/knowledge").json()["items"]
    assert [(k["scope_type"], k["scope_value"], k["kind"], k["payload"]) for k in items] == [("region", "AU", "crs", {"epsg": 28356})]
