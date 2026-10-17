"""Joining sheets into holes (invented page results)."""

import pytest
from pyproj import Transformer

from pipeline.assemble import PageResult, _layers, assemble
from pipeline.extraction_models import PageExtraction, XCoordinateRow, XLayer, XLog
from pipeline.georef import haversine_km
from pipeline.scoring import finding_severity
from schema.borehole import Borehole, GeorefMethod, SourceRef


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


def test_holes_of_different_investigations_with_one_name_are_not_joined():
    from pipeline.extraction_models import PageLabResults, XCoordinateRow, XLabRow

    def sheet(n: int, layers, report: str | None = "S.I. No. 101", drilled: str | None = "1990-03-01",
              sheet: str = "1 of 1", name: str = "Borehole No. 2", **header) -> PageResult:
        log = _log(name, None, None, layers).model_copy(update={
            "crs_raw": None, "project_id": report, "start_date": drilled, "sheet": sheet,
            "project_name": "Harbour wall", "client": "Port authority", **header})
        return PageResult("c", "c.pdf", n, "borehole_log", PageExtraction(logs=[log], coordinate_rows=[]))

    def overlaps(bh) -> int:
        return sum(i.startswith("overlap") for i in bh.issues)

    # A compilation of older reports, each with its own 'Borehole No. 2'; the first sheet is copied twice.
    # A coordinate table and a lab sheet that only give the name cannot tell which hole they mean.
    table = XCoordinateRow(name="BH 2", coords_raw="E 412000 N 5320000", crs_raw="UTM 32N", elevation_raw=None,
                           is_planned=False)
    lab = XLabRow(hole_name="2", sample_name=None, top_m=1.0, bottom_m=None, parameter="water_content",
                  parameter_raw="w %", value_raw="21", unit_raw="%")
    records = assemble([
        sheet(1, [(0, 2), (2, 5)], sheet="1 of 2"), sheet(2, [(0, 2), (2, 5)], sheet="1 of 2"),
        sheet(3, [(5, 8)], sheet="2 of 2"),
        sheet(4, [(0, 4)], report="S.I. No. 202", drilled="1994-06-10", project_name="Ring road"),
        sheet(5, [(0, 1), (1, 6)], report="AQ 7", drilled="1985-11-19", sheet="1 of 3"),
        PageResult("c", "c.pdf", 6, "coordinate_table", PageExtraction(logs=[], coordinate_rows=[table])),
        PageResult("c", "c.pdf", 7, "lab_results", None, labs=PageLabResults(rows=[lab], unreadable=[])),
    ])
    assert len(records) == len({b.borehole_id for b in records}) == len({b.borehole_name for b in records}) == 3
    assert all("2" in bh.borehole_name and bh.layers[0].top_m == 0 and not overlaps(bh) for bh in records)
    assert all(_disputed(bh) and bh.review_status.value != "auto_approved" for bh in records)
    assert all(bh.location.method != "linked_table" and not bh.lab_results for bh in records)
    [first] = [bh for bh in records if bh.project_id == "S.I. No. 101"]
    assert first.layers[-1].bottom_m == 8 and {s.page for s in first.sources} == {1, 2, 3}

    # Handwritten logs of one project: the same short name, drilled on other days.
    records = assemble([sheet(1, [(0, 1.5)], report=None, drilled="2013-07-14"),
                        sheet(2, [(0, 2.5)], report=None, drilled="2013-08-16")])
    assert len(records) == 2 and all(_disputed(bh) and not overlaps(bh) for bh in records)

    # The same report number written differently, or a single header text read differently, is no evidence.
    for second in (sheet(2, [(0, 4)], report="SI 101"), sheet(2, [(0, 4)], location_text="Pier 4")):
        [bh] = assemble([sheet(1, [(0, 5)], location_text="North quay"), second])
        assert not _disputed(bh)

    # Depths that continue keep the sheets together: another report number needs a look, another date does not
    # (a continuation sheet may carry the day it was drilled).
    [bh] = assemble([sheet(1, [(0, 5)]), sheet(2, [(5, 9)], report="S.I. No. 202")])
    assert bh.layers[-1].bottom_m == 9 and _disputed(bh)
    [bh] = assemble([sheet(1, [(0, 5)]), sheet(2, [(5, 9)], drilled="1990-03-02")])
    assert bh.layers[-1].bottom_m == 9 and not _disputed(bh)


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


def test_sheets_of_a_hole_in_separate_image_files_are_joined():
    here = "E 412000 N 5320000"

    def image(n: int, sheet: str, layers, elevation: float = 300.0, name: str = "BH1") -> PageResult:
        log = _log(name, here, elevation, layers).model_copy(update={"sheet": sheet})
        return PageResult(f"f__scan_{n}", f"f/scan_{n}.png", 1, "borehole_log",
                          PageExtraction(logs=[log], coordinate_rows=[]))

    def missing(bh) -> bool:
        return any(i.startswith("missing sheets") for i in bh.issues)

    # Files in numeric order (9 before 10); the hole keeps the id of its first sheet's file.
    [bh] = assemble([image(10, "2 of 2", [(10.3, 14)]), image(9, "1 of 2", [(0, 4), (4, 10)])])
    assert bh.borehole_id == "f__scan_9:BH1" and not missing(bh)
    assert [(l.top_m, l.bottom_m) for l in bh.layers][-1] == (10.3, 14)
    assert {s.file for s in bh.sources} == {"f/scan_9.png", "f/scan_10.png"}

    # A restated unit (the next sheet repeats the last unit from its top) still continues the hole.
    [bh] = assemble([image(1, "1 of 2", [(0, 4), (4, 10)]), image(2, "2 of 2", [(4, 14)])])
    assert not missing(bh) and bh.layers[-1].bottom_m == 14

    # Depths that restart, another ground level, numbering that does not follow or another name: not joined,
    # whatever is kept is still missing a sheet.
    for second in (image(2, "2 of 2", [(0, 5)]), image(2, "2 of 2", [(10, 14)], elevation=303.0),
                   image(2, "2 of 3", [(10, 14)]), image(2, "2 of 2", [(10, 14)], name="BH2")):
        records = assemble([image(1, "1 of 2", [(0, 4), (4, 10)]), second])
        assert all(missing(bh) and (bh.layers[-1].bottom_m <= 10 or bh.layers[0].top_m > 0) for bh in records)


def test_complete_copy_of_a_hole_wins_over_a_fragment():
    here = "E 412000 N 5320000"
    pdf = [PageResult("f__report", "f/report.pdf", n, "borehole_log", PageExtraction(
        logs=[_log("BH1", here, 300.0, layers).model_copy(update={"sheet": f"{n} of 2"})], coordinate_rows=[]))
        for n, layers in ((1, [(0, 4), (4, 10)]), (2, [(10, 14)]))]
    fragment = PageResult("f__scan_2", "f/scan_2.png", 1, "borehole_log", PageExtraction(
        logs=[_log("BH1", here, 300.0, [(10, 14)]).model_copy(update={"sheet": "2 of 2"})], coordinate_rows=[]))
    [bh] = assemble(pdf + [fragment])
    assert bh.borehole_id == "f__report:BH1" and bh.layers[0].top_m == 0


def _sheet_layers(*sheets: list[tuple[float, float, str, str]]):
    """Layers of one hole joined from consecutive sheets of (top, bottom, description, primary)."""
    logs = [(_log("BH1", "E 412000 N 5320000", 300.0, []).model_copy(update={"layers": [
        XLayer.model_validate({**{k: None for k in XLayer.model_fields}, "top_m": t, "bottom_m": b,
                               "description_original": d, "primary": p, "secondary": []}) for t, b, d, p in sheet]}),
             SourceRef(file="r.pdf", page=n)) for n, sheet in enumerate(sheets, start=1)]
    notes: list[str] = []
    layers = _layers(logs, None, notes)
    return [(l.top_m, l.bottom_m, l.primary.value) for l in layers], layers, notes


def _blocking(layers) -> list[str]:
    bh = Borehole(borehole_id="r:BH1", borehole_name="BH1", layers=layers)
    return [i for i in bh.validate_log() if finding_severity(i) == "blocking"]


def test_unit_run_past_the_sheet_end_is_one_layer():
    # The last unit is drawn to the end of the last sample (10.45 m on a 10 m sheet); the next sheet restarts it.
    depths, layers, notes = _sheet_layers(
        [(0, 8.5, "Dense SAND", "sand"), (8.5, 10.45, "Stiff sandy CLAY", "clay")],
        [(10, 12.5, "Strata as previous page ... becoming silty from 11 m", "clay"), (12.5, 15, "Dense SAND", "sand")])
    assert depths == [(0, 8.5, "sand"), (8.5, 12.5, "clay"), (12.5, 15, "sand")]
    assert layers[1].description_original.startswith("Stiff sandy CLAY / Strata as previous page")
    assert len(notes) == 1 and not _blocking(layers)

    # Without a marker the same lithology overrunning the sheet end is the same unit too.
    depths, _, _ = _sheet_layers([(8.5, 10.45, "Stiff sandy CLAY", "clay")], [(10, 12.5, "Firm CLAY", "clay")])
    assert depths == [(8.5, 12.5, "clay")]


def test_back_references_join_across_several_sheets():
    depths, layers, _ = _sheet_layers(
        [(18.5, 20, "Weak SANDSTONE", "sandstone")], [(20, 30, "strata as previous page", "sandstone")],
        [(30, 39.95, "— Ditto —", "unknown")], [(40, 40.6, "(continued from previous sheet: Weak SANDSTONE)", "sandstone")],
        [(40.6, 40.6, "fragments of the sandstone as gravel", "sandstone"), (40.6, 42, "Stiff CLAY", "clay")])
    assert depths == [(18.5, 40.6, "sandstone"), (40.6, 42, "clay")]
    assert layers[0].description_original == "Weak SANDSTONE / fragments of the sandstone as gravel"


def test_continued_unit_with_its_own_description_starts_at_the_foot_of_the_previous_sheet():
    # The unit printed as continued began in the rows below the last description read on the previous sheet.
    depths, _, notes = _sheet_layers([(26.8, 29.87, "fine to coarse SAND", "sand")],
                                     [(30, 40, "medium to fine SAND, silty layers. (continued)", "sand")])
    assert depths == [(26.8, 29.87, "sand"), (29.87, 40, "sand")]
    assert len(notes) == 1


def test_sheet_break_keeps_what_it_cannot_resolve():
    # Another material under a back-reference contradicts it: the overlap stays and blocks.
    depths, layers, _ = _sheet_layers([(8.5, 10.45, "Stiff CLAY", "clay")], [(10, 12, "As above", "sand")])
    assert depths == [(8.5, 10.45, "clay"), (10, 12, "sand")] and any("overlap" in i for i in _blocking(layers))
    # A new description at the sheet top is a boundary; an undescribed stretch or a large overlap stays visible.
    for second in ([(10, 12, "Soft silty CLAY", "clay")], [(10.3, 12, "Soft silty CLAY", "clay")],
                   [(9, 12, "Soft silty CLAY", "clay")]):
        depths, _, _ = _sheet_layers([(8.5, 10, "Stiff CLAY", "clay")], second)
        assert len(depths) == 2


def test_only_unreadable_items_outside_the_header_and_tests_lower_the_extraction_confidence():
    def confidence(*unreadable: str) -> float:
        log = _log("BH1", "E 412000 N 5320000", 300.0, [(0, 2), (2, 6)]).model_copy(update={"unreadable": list(unreadable)})
        [bh] = assemble([PageResult("r", "r.pdf", 1, "borehole_log", PageExtraction(logs=[log], coordinate_rows=[]))])
        return bh.confidence

    # Several items, so the penalty goes below the cap for logs nothing confirms independently.
    clean = confidence()
    assert confidence(*["header: drilling method cut off by a punch hole", "tests: SPT blows at 4.5 m faint"] * 2) == clean
    assert confidence(*[f"depth: boundary at {d} m faint" for d in (1, 2, 3, 4)]) < clean
    assert confidence(*[f"boundary at {d} m faint" for d in (1, 2, 3, 4)]) < clean  # without a field name it counts
    # Doubts about the position or the ground level count under any field name.
    assert confidence(*["header: datum illegible", "header: coordinate uncertain", "header: ground level faint",
                        "tests: UTM zone cut off"]) < clean


@pytest.mark.parametrize("header_logs", [False, True])
def test_a_coordinate_table_with_or_without_header_logs_gives_the_same_position(header_logs):
    # The model returns a table page sometimes as rows only, sometimes also as a header-only log per hole; the
    # hole's own sheet prints UTM, the table Oslo's local grid. Holmenkollen ski jump stands in for the hole.
    lat, lon = 59.9636, 10.6676
    e, n = Transformer.from_crs(4326, 32632, always_xy=True).transform(lon, lat)
    ly, lx = Transformer.from_crs(4326, "ESRI:102138", always_xy=True).transform(lon, lat)
    table = f"X-koordinat {lx:.1f} Y-koordinat {ly:.1f}".replace(".", ",")
    place = {"place_lat": 59.91, "place_lon": 10.75, "place_precision_km": 10.0, "country": "NO"}
    sheet = _log("BH1", f"UTM Sone: 32 ØV-koordinat: {e:.0f} NS-koordinat: {n:.0f}", 10.0, [(0, 4), (4, 9)])
    sheet = sheet.model_copy(update={"crs_raw": "Kartdatum WGS 84, UTM Sone 32", **place})
    header = _log("BH1", table, 10.0, []).model_copy(update={"crs_raw": "Oslo lokal", **place})
    row = XCoordinateRow(name="BH1", coords_raw=table, crs_raw="Oslo lokal", elevation_raw=None, is_planned=False)
    [bh] = assemble([PageResult("t", "t.pdf", 1, "coordinate_table",
                                PageExtraction(logs=[header] if header_logs else [], coordinate_rows=[row])),
                     PageResult("t", "t.pdf", 2, "borehole_log", PageExtraction(logs=[sheet], coordinate_rows=[]))])
    assert bh.location.method != GeorefMethod.geocoded_place and bh.location.confidence >= 0.9
    assert haversine_km(bh.location.lat, bh.location.lon, lat, lon) < 0.005


@pytest.mark.parametrize("notes, blocked", [
    ("Depths converted from feet to metres.", True),
    ("Tiefen von Fuß in Meter umgerechnet", True),
    ("depths 0-12' converted", True),
    ("soft clay, left bank; FTIR sample at the foot of the slope", False),
    ("coordinates 48°12'30\" N", False),
])
def test_a_conversion_from_feet_stated_only_in_the_notes_goes_to_a_person(notes, blocked):
    page = _page(1, "E 412000 N 5320000", 300.0, [(0, 2), (2, 6)])
    page.extraction.logs[0].depth_unit_raw = "m"
    page.extraction.logs[0].notes = notes
    [bh] = assemble([page])
    feet = [i for i in bh.issues if i.startswith("depths printed in feet")]
    assert bool(feet) == blocked
    assert all(finding_severity(i) == "blocking" for i in feet)
