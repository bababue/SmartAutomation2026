"""Query API on the v3 projections: depth-aware lithology, lab ranges, bbox, map points, pages (synthetic)."""

import pytest
from fastapi.testclient import TestClient

from api.main import app
from pipeline import config, store
from pipeline.scoring import score
from schema.borehole import Borehole, GeorefMethod, LabResult, Layer, Location, SourceRef


def hole(bid, lat, lon, layers, ucs=None):
    src = SourceRef(file="q.pdf", page=1)
    bh = Borehole(borehole_id=bid, borehole_name=bid.split(":")[1], depth_m=layers[-1][1],
                  layers=[Layer(top_m=t, bottom_m=b, primary=p, description_original=p, source=src) for t, b, p in layers],
                  lab_results=[LabResult(parameter="ucs", value=ucs, value_raw=str(ucs), top_m=5)] if ucs else [],
                  location=Location(lat=lat, lon=lon, method=GeorefMethod.stated_coordinates, confidence=0.95),
                  sources=[src])
    bh.issues = bh.validate_log()
    score(bh, {}, 0)
    return bh


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "q.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store.connect, "__defaults__", (path,))
    con = store.connect(path)
    for bh in [
        hole("q:A", 10.0, 179.5, [(0, 12, "sand"), (12, 30, "clay")], ucs=120),   # clay deep, strong rock test
        hole("q:B", 10.0, -179.5, [(0, 5, "clay"), (5, 20, "limestone")], ucs=40),  # clay shallow, east of the dateline
        hole("q:C", 48.0, 8.0, [(0, 9, "gravel")]),
    ]:
        store.save_borehole(con, bh)
    con.close()
    return TestClient(app)


def ids(r):
    return sorted(f["properties"]["id"] for f in r.json()["features"])


def test_lithology_within_a_depth_range(client):
    assert ids(client.get("/api/boreholes?lithology=clay")) == ["q:A", "q:B"]
    assert ids(client.get("/api/boreholes?lithology=clay&depth_from=10&depth_to=20")) == ["q:A"]


def test_lab_value_range(client):
    assert ids(client.get("/api/boreholes?lab=ucs&lab_min=100")) == ["q:A"]
    assert ids(client.get("/api/boreholes?lab=ucs&lab_max=50")) == ["q:B"]


def test_bbox_across_the_antimeridian_and_on_its_edge(client):
    assert ids(client.get("/api/boreholes?bbox=179,9,-179,11")) == ["q:A", "q:B"]
    assert ids(client.get("/api/boreholes?bbox=7,47,8,48")) == ["q:C"]  # a point on the box edge is inside


def test_map_points_are_slim_and_lists_are_paged(client):
    m = client.get("/api/boreholes?fields=map").json()
    assert m["total"] == 3 and set(m["features"][0]["properties"]) == {"id", "name", "location_name", "depth", "review_status", "score", "loc_method"}
    page = client.get("/api/boreholes?limit=2&offset=0").json()
    assert page["total"] == 3 and len(page["features"]) == 2
    assert dict(map(tuple, page["facets"]["lithologies"]))["clay"] == 2
    rest = client.get("/api/boreholes?limit=2&offset=2").json()
    assert len(rest["features"]) == 1


def test_layer_strips_are_limited_to_the_visible_bbox(client):
    assert client.get("/api/boreholes?fields=layers").status_code == 422
    result = client.get("/api/boreholes?fields=layers&bbox=179,9,-179,11").json()
    assert {item["id"]: item["strip"] for item in result["items"]} == {
        "q:A": [[0.0, 12.0, "sand"], [12.0, 30.0, "clay"]],
        "q:B": [[0.0, 5.0, "clay"], [5.0, 20.0, "limestone"]],
    }


def test_review_queue_pages(client):
    r = client.get("/api/review-queue?limit=1")
    assert r.status_code == 200 and int(r.headers["X-Total-Count"]) >= len(r.json())
    assert client.get("/api/corrections/orphans").json() == []


def test_review_queue_blocking_only(client):
    con = store.connect()
    store.save_borehole(con, hole("q:G", 47.0, 8.0, [(0, 5, "sand"), (7, 10, "clay")]))  # gap: blocking
    con.close()
    every = {item["id"] for item in client.get("/api/review-queue").json()}
    r = client.get("/api/review-queue?blocking=true")
    assert [item["id"] for item in r.json()] == ["q:G"] and r.headers["X-Total-Count"] == "1"
    assert "q:G" in every


@pytest.mark.parametrize("qualifier,value,upper,query,expected", [
    ("<", 5, None, "lab_min=4.5", False), ("<", 5, None, "lab_max=5", True),
    (">", 5, None, "lab_max=6", False), (">", 5, None, "lab_min=5", True),
    ("range", 5, 10, "lab_min=4&lab_max=11", True),
    ("range", 5, 10, "lab_min=6", False), ("range", 5, 10, "lab_max=9", False),
    (None, 5, None, "lab_min=5&lab_max=5", True),
])
def test_lab_filters_require_the_whole_value_to_match(client, qualifier, value, upper, query, expected):
    bh = hole("q:qualified", 48.0, 8.0, [(0, 9, "gravel")])
    bh.lab_results = [LabResult(parameter="ucs", value=value, value_upper=upper, qualifier=qualifier, top_m=2)]
    con = store.connect()
    store.save_borehole(con, bh)
    con.close()
    response = client.get(f"/api/boreholes?id=q:qualified&lab=ucs&{query}")
    assert ids(response) == (["q:qualified"] if expected else [])


@pytest.mark.parametrize("bbox", ["bad", "1,2,3", "1,2,3,4,5", "nan,0,8,1", "0,5,8,1", "-181,0,8,1"])
def test_invalid_bbox_returns_validation_error(client, bbox):
    assert client.get("/api/boreholes", params={"bbox": bbox}).status_code == 422


@pytest.mark.parametrize("value", [None, "", "   "])
def test_required_name_cannot_be_cleared(client, value):
    response = client.post("/api/boreholes/q:A/corrections", json={"kind": "field", "reviewer": "Ana",
                            "payload": {"field": "borehole_name", "value": value}})
    assert response.status_code == 422
    assert client.get("/api/boreholes/q:A/detail").json()["borehole"]["borehole_name"] == "A"
