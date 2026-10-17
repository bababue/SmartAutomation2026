"""Search and export endpoints (synthetic data only)."""

import csv
import io

import pytest
from fastapi.testclient import TestClient

from api.main import app
from pipeline import config, store
from schema.borehole import Borehole, GeorefMethod, Layer, Location, SourceRef


def record(bid: str, file: str, pages: list[int], name: str = "BH1") -> Borehole:
    src = SourceRef(file=file, page=pages[0])
    return Borehole(
        borehole_id=bid, borehole_name=name, depth_m=4.0, location_name="Hyde Park",
        layers=[Layer(top_m=0, bottom_m=4.0, description_original="Sand", primary="sand", source=src)],
        location=Location(lat=-33.873, lon=151.211, method=GeorefMethod.stated_coordinates, confidence=0.95),
        sources=[SourceRef(file=file, page=p) for p in pages] + [SourceRef(file="elsewhere.pdf", page=9)],
        score=0.95, review_status="auto_approved",
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite"
    monkeypatch.setattr(config, "DB_PATH", path)
    con = store.connect(path)
    store.save_document(con, "XYZ01", "reports/XYZ01_site_report.pdf", "pdf", [])
    store.save_document(con, "QRS02", "reports/QRS02_logs.pdf", "pdf", [])
    store.save_borehole(con, record("XYZ01:BH1", "reports/XYZ01_site_report.pdf", [2, 1]))
    store.save_borehole(con, record("QRS02:BH7", "reports/QRS02_logs.pdf", [5], name="BH7"))
    con.close()
    return TestClient(app)


def ids(client, q):
    return sorted(f["properties"]["id"] for f in client.get("/api/boreholes", params={"q": q}).json()["features"])


def test_search_by_file_name(client):
    assert ids(client, "XYZ01") == ["XYZ01:BH1"]
    assert ids(client, "qrs02_logs") == ["QRS02:BH7"]
    assert ids(client, "site_report XYZ01") == ["XYZ01:BH1"]  # every word must match
    assert ids(client, "Hyde") == ["QRS02:BH7", "XYZ01:BH1"]  # text search still works
    assert ids(client, "nomatch") == []


def test_like_wildcards_are_literal(client):
    assert ids(client, "XYZ_1") == []


def test_map_points_include_depth_for_3d_markers(client):
    features = client.get("/api/boreholes", params={"fields": "map"}).json()["features"]
    assert {feature["properties"]["id"]: feature["properties"]["depth"] for feature in features} == {
        "XYZ01:BH1": 4.0, "QRS02:BH7": 4.0,
    }


def test_csv_lists_source_file_and_pages(client):
    rows = list(csv.reader(io.StringIO(client.get("/api/export.csv").text)))
    header = rows[0]
    assert header[:4] == ["BoreholeID", "BoreholeName", "SourceFile", "SourcePages"]
    by_id = {r[0]: dict(zip(header, r)) for r in rows[1:]}
    assert by_id["XYZ01:BH1"]["SourceFile"] == "reports/XYZ01_site_report.pdf"
    assert by_id["XYZ01:BH1"]["SourcePages"] == "1;2"  # pages of other files are left out
    assert by_id["QRS02:BH7"]["SourcePages"] == "5"


def test_filter_by_ids(client):
    features = client.get("/api/boreholes", params={"id": ["QRS02:BH7", "missing"]}).json()["features"]
    assert [f["properties"]["id"] for f in features] == ["QRS02:BH7"]
