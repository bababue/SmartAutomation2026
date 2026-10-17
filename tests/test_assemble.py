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
