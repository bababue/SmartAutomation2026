"""Joining sheets into holes (invented page results)."""

from pipeline.assemble import PageResult, assemble
from pipeline.extraction_models import PageExtraction, XLayer, XLog
from pipeline.scoring import finding_severity


def _log(name: str, coords: str, elevation: float, layers: list[tuple[float, float]]) -> XLog:
    empty = {k: None for k in XLog.model_fields}
    return XLog.model_validate({
        **empty, "name": name, "coords_raw": coords, "crs_raw": "UTM 32N", "elevation_m": elevation,
        "layers": [XLayer.model_validate({**{k: None for k in XLayer.model_fields}, "top_m": t, "bottom_m": b,
                                          "description_original": f"sand {t}", "primary": "sand", "secondary": []})
                   for t, b in layers],
        "spt": [], "core_runs": [], "samples": [], "groundwater": [], "lab_results": [], "unreadable": []})


def _page(n: int, coords: str, elevation: float, layers: list[tuple[float, float]], *more: XLog) -> PageResult:
    logs = [_log("BH1", coords, elevation, layers), *more]
    return PageResult("r", "r.pdf", n, "borehole_log", PageExtraction(logs=logs, coordinate_rows=[]))


def _disputed(bh) -> bool:
    return any(finding_severity(i) == "blocking" and i.startswith("sheet header differs") for i in bh.issues)


def test_sheet_with_another_header_is_not_a_continuation():
    here, nearby = "E 412000 N 5320000", "E 412040 N 5320030"  # 50 m apart

    # Another position and ground level, depths start again at the surface: a second hole, under a placeholder
    # so that nothing stored for BH1 (corrections, decisions) follows it.
    records = {b.borehole_id: b for b in assemble([_page(1, here, 300.0, [(0, 2), (2, 6)]),
                                                   _page(2, nearby, 303.0, [(0, 1.5), (1.5, 4)])])}
    assert set(records) == {"r:BH1", "r:p2"}
    assert records["r:p2"].borehole_name != "BH1"
    assert [(l.top_m, l.bottom_m) for l in records["r:BH1"].layers] == [(0, 2), (2, 6)]
    assert [(l.top_m, l.bottom_m) for l in records["r:p2"].layers] == [(0, 1.5), (1.5, 4)]
    assert all(_disputed(bh) and bh.review_status.value != "auto_approved" for bh in records.values())

    # Same coordinates, only the ground level differs: the two holes are not de-duplicated into one.
    assert len(assemble([_page(1, here, 300.0, [(0, 6)]), _page(2, here, 303.0, [(0, 4)])])) == 2

    # Two names split on the same page get distinct ids.
    records = assemble([_page(1, here, 300.0, [(0, 6)], _log("BH2", "E 413000 N 5321000", 310.0, [(0, 6)])),
                        _page(2, nearby, 303.0, [(0, 4)], _log("BH2", "E 413040 N 5321030", 313.0, [(0, 4)]))])
    assert len({b.borehole_id for b in records}) == len(records) == 4

    # The depths continue: one hole with a misread header, kept together but never released unseen.
    [bh] = assemble([_page(1, here, 300.0, [(0, 2), (2, 6)]), _page(2, nearby, 303.0, [(6, 9)])])
    assert bh.borehole_id == "r:BH1" and bh.layers[-1].bottom_m == 9 and _disputed(bh)

    # Small drifts that add up are compared against every earlier sheet, not only the first.
    [bh] = assemble([_page(1, here, 300.0, [(0, 3)]), _page(2, here, 300.4, [(3, 6)]), _page(3, here, 299.6, [(6, 9)])])
    assert _disputed(bh)

    # The same header on every sheet, also in another notation, is an ordinary continuation.
    [bh] = assemble([_page(1, here, 300.0, [(0, 2), (2, 6)]), _page(2, "412000 5320000", 300.0, [(0, 9)])])
    assert not any(i.startswith("sheet header differs") for i in bh.issues)


def test_holes_at_different_places_with_the_same_coordinates_are_blocked():
    here = "E 412000.000 N 5320000.000"

    def page(n: int, name: str, place: str | None, doc: str = "r", layers=((0, 6),)) -> PageResult:
        log = _log(name, here, 300.0, list(layers)).model_copy(update={"location_text": place})
        return PageResult(doc, f"{doc}.pdf", n, "borehole_log", PageExtraction(logs=[log], coordinate_rows=[]))

    def shared(bh) -> bool:
        return any(finding_severity(i) == "blocking" and i.startswith("same printed coordinates") for i in bh.issues)

    # A header copied from the hole at another crossing: both holes go to review, the other one is named.
    records = {b.borehole_name: b for b in assemble([page(1, "BH1", "Elm Street / Oak Road"),
                                                     page(2, "BH2", "Elm Street / Pine Road")])}
    assert len(records) == 2
    assert all(shared(bh) and bh.review_status.value != "auto_approved" for bh in records.values())
    assert any("BH1" in i for i in records["BH2"].issues if i.startswith("same printed coordinates"))

    # A redrilled hole at the same place, written differently, may share the position.
    records = assemble([page(1, "BH1", "Elm Street / Oak Road"), page(2, "BH1A", "elm street, oak road")])
    assert len(records) == 2 and not any(shared(bh) for bh in records)

    # A richer copy of the hole in another document without the conflict does not swallow the finding.
    records = assemble([page(1, "BH1", "Elm Street / Oak Road"), page(2, "BH2", "Elm Street / Pine Road"),
                        page(1, "BH1", "Elm Street / Oak Road", "s", ((0, 2), (2, 6)))])
    assert any(shared(bh) for bh in records if bh.borehole_name == "BH1")

    # Without a place name on one of them there is nothing to contradict.
    assert not any(shared(bh) for bh in assemble([page(1, "BH1", "Elm Street / Oak Road"), page(2, "BH2", None)]))


def test_coordinate_table_system_wins_over_the_log_template_but_needs_a_look():
    from pyproj import Transformer

    from pipeline.extraction_models import XCoordinateRow

    lat, lon = 59.9075, 10.7531  # Oslo Opera House
    east, north = Transformer.from_crs(4326, "ESRI:102138", always_xy=True).transform(lon, lat)
    coords = f"X {north:.1f} Y {east:.1f}"
    log = _log("BH1", coords, 5.0, [(0, 2)]).model_copy(
        update={"country": "NO", "place_lat": 59.91, "place_lon": 10.75, "place_precision_km": 2.0})
    table = XCoordinateRow(name="BH1", coords_raw=coords, crs_raw="Oslo lokal", elevation_raw="5,0", is_planned=False)
    pages = [PageResult("r", "r.pdf", 1, "borehole_log", PageExtraction(logs=[log], coordinate_rows=[])),
             PageResult("r", "r.pdf", 2, "coordinate_table", PageExtraction(logs=[], coordinate_rows=[table]))]
    [bh] = assemble(pages)
    assert bh.location.crs_proj == "ESRI:102138"
    assert abs(bh.location.lat - lat) < 1e-4 and abs(bh.location.lon - lon) < 1e-4
    assert bh.location.confidence <= 0.8
    assert any(i.startswith("coordinate system taken from the coordinate table") for i in bh.issues)
