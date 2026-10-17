"""Search by ground conditions (hand-made records only)."""

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.similar import like, profile
from pipeline import config, store
from schema.borehole import Borehole, GeorefMethod, GroundwaterObservation, Layer, Location, SourceRef

SRC = SourceRef(file="logs.pdf", page=1)


def hole(bid: str, layers: list[tuple[float, float, str]], water: float | None = None) -> Borehole:
    return Borehole(
        borehole_id=bid, borehole_name=bid.split(":")[1], depth_m=layers[-1][1],
        layers=[Layer(top_m=t, bottom_m=b, description_original=c, primary=c, source=SRC) for t, b, c in layers],
        groundwater=[GroundwaterObservation(depth_m=water, kind="standing")] if water is not None else [],
        location=Location(lat=51.5007, lon=-0.1246, method=GeorefMethod.stated_coordinates, confidence=0.95),  # Big Ben
        sources=[SRC], score=0.9, review_status="auto_approved",
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    store.migrate(path)
    con = store.connect(path)
    store.save_document(con, "DOC1", "logs.pdf", [])
    store.save_borehole(con, hole("DOC1:CLAYROCK", [(0, 6, "clay"), (6, 15, "limestone")], water=2.0))
    store.save_borehole(con, hole("DOC1:SAND", [(0, 25, "medium_sand")]))  # no rock head, no water level
    store.save_borehole(con, hole("DOC1:SHALLOW", [(0, 3, "fine_sand")], water=1.0))
    con.close()
    return TestClient(app)


def found(client, **params) -> list[str]:
    res = client.get("/api/boreholes", params=params)
    assert res.status_code == 200, res.text
    return sorted(f["properties"]["id"].split(":")[1] for f in res.json()["features"])


def test_rock_head_and_water_bounds_skip_holes_without_them(client):
    assert found(client, rock_head_max=10) == ["CLAYROCK"]
    assert found(client, rock_head_max=5) == []
    assert found(client, groundwater_max=3) == ["CLAYROCK", "SHALLOW"]


def test_material_counts_only_logged_metres_of_the_group(client):
    assert found(client, material="sand:0:20:5") == ["SAND"]  # the 3 m hole cannot hold 5 m of sand
    assert found(client, material="sand:0:20:3") == ["SAND", "SHALLOW"]
    assert found(client, material="sand:10:20:10") == ["SAND"]
    assert found(client, material=["clay:0:10:3"], rock_head_max=10) == ["CLAYROCK"]
    assert found(client, material="rock:0:10:5") == []  # 4 m of limestone above 10 m


def test_unknown_group_is_rejected(client):
    assert client.get("/api/boreholes", params={"material": "lava:0:10:1"}).status_code == 422
    assert client.get("/api/boreholes", params={"material": "sand:5:2:1"}).status_code == 422


def test_like_this_fills_the_filters_from_the_hole():
    layers = [(0, 6, "clay"), (6, 15, "limestone")]
    p = profile(layers, {"rock_head_m": 6, "groundwater_m": 2})
    assert like(p, layers) == {"rock_head_max": 8, "groundwater_max": 3.0, "material": ["clay:0:6:3.6"]}
    sand = [(0, 25, "medium_sand")]
    assert like(profile(sand), sand) == {"rock_head_max": None, "groundwater_max": None, "material": ["sand:0:20:12"]}
