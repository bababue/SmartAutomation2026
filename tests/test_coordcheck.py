"""Second reading of printed coordinates, and one retry for a log page that comes back short (no model calls)."""

import json

import pymupdf
import pytest

from pipeline import config, coordcheck, llm, scoring
from pipeline.extraction_models import CoordinateReading, PageExtraction
from pipeline.georef import georeference
from pipeline.ingest import PageImage
from schema.borehole import Borehole, GeorefMethod, SourceRef
from tests.test_assemble import _log

# Invented coordinates beside the Brandenburg Gate, Berlin
COORDS, CRS = "E 389940 N 5819670", "ETRS89 / UTM zone 33N"


def record(page=3):
    loc = georeference(COORDS, CRS, "DE", 52.5163, 13.3777, 5).location
    loc.source = SourceRef(file="r.pdf", page=page, bbox=(0.6, 0.05, 0.9, 0.1))
    return Borehole(borehole_id="r:BH1", borehole_name="BH1", location=loc)


def second(coords, crs=None, page=3, hole="BH1", source="log"):
    return {("r", page, coordcheck.norm_name(hole), source): CoordinateReading(coords_raw=coords, crs_raw=crs)}


@pytest.mark.parametrize("coords", [COORDS, "R 389940 / H 5819670", "389940.0 E, 5819670.0 N"])
def test_same_point_in_another_notation_confirms(coords):
    finding = coordcheck.check(record(), second(coords, CRS))
    assert finding == coordcheck.CONFIRMED and scoring.finding_severity(finding) == "info"


def test_a_different_digit_blocks():
    finding = coordcheck.check(record(), second("E 389940 N 5819970", CRS))
    assert finding.startswith(coordcheck.DIFFERENT) and "300 m" in finding
    assert scoring.finding_severity(finding) == "blocking"


def test_a_different_coordinate_system_blocks():
    finding = coordcheck.check(record(), second("R 4589940 H 5819670", "Gauss-Krüger EPSG:31468"))
    assert finding.startswith(coordcheck.DIFFERENT_CRS) and scoring.finding_severity(finding) == "blocking"


def test_nothing_legible_on_the_second_look_needs_a_look():
    finding = coordcheck.check(record(), second(None))
    assert finding == coordcheck.NOT_FOUND and scoring.finding_severity(finding) == "needs_look"


def test_a_digit_misread_under_an_equivalent_crs_is_not_called_a_crs_difference():
    finding = coordcheck.check(record(), second("E 389940 N 5819970", "WGS 84 UTM 33N EPSG:25833"))
    assert finding.startswith(coordcheck.DIFFERENT)


def test_the_crs_finding_is_settled_by_a_reviewer_position_fix():
    from pipeline.corrections import GEOREF_ISSUES
    assert coordcheck.DIFFERENT_CRS.startswith(GEOREF_ISSUES) and coordcheck.DIFFERENT.startswith(GEOREF_ISSUES)


def test_without_a_matching_second_reading_nothing_is_added():
    assert coordcheck.check(record(), {}) is None
    assert coordcheck.check(record(), second(COORDS, CRS, page=4)) is None
    assert coordcheck.check(record(), second("E 1 N 2", CRS, hole="BH9")) is None  # another hole on the page
    assert coordcheck.check(record(), second(COORDS, CRS, source="table")) is None
    assert coordcheck.check(record(), {("r", 3, "BH1", "log"): None}) is None  # two holes of that name
    place_only = Borehole(borehole_id="r:BH2", borehole_name="BH2",
                          location=georeference(None, None, "DE", 52.5163, 13.3777, 5).location)
    assert coordcheck.check(place_only, second(COORDS, CRS)) is None


def test_a_hole_positioned_from_a_coordinate_table_is_checked_against_its_row():
    bh = record(page=7)
    bh.location.method = GeorefMethod.linked_table
    assert coordcheck.check(bh, second(COORDS, CRS, page=2, source="table")) == coordcheck.CONFIRMED
    assert coordcheck.check(bh, second(COORDS, CRS, page=7)) is None  # the log page's reading is not the table's


def test_targets_read_the_box_and_whole_pages_for_table_rows():
    page = PageImage("r", 3, "r/0003.jpg", "", "", 100, 100)
    log = _log("BH1", COORDS, 30, [(0, 5)]).model_copy(update={"coords_bbox": [0.6, 0.05, 0.9, 0.1]})
    boxless = _log("BH2", COORDS, 30, [(0, 5)])
    bare = _log("BH3", None, 30, [(0, 5)]).model_copy(update={"coords_raw": None})
    rows = [{"name": n, "coords_raw": COORDS, "crs_raw": None, "elevation_raw": None, "is_planned": None}
            for n in ("BH1", "BH3")]
    x = PageExtraction.model_validate({"logs": [log.model_dump(), boxless.model_dump(), bare.model_dump()],
                                       "coordinate_rows": rows})
    # Only BH3 prints no coordinates on its log, so only its table row is read.
    assert [(b, h, s) for _, b, h, s in coordcheck.targets([page], {("r", 3): x})] == [
        ((0.6, 0.05, 0.9, 0.1), "BH1", "log"), (None, "BH2", "log"), (None, "BH3", "table")]


@pytest.fixture()
def pages(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    image = tmp_path / "pages" / "r" / "0003.jpg"
    image.parent.mkdir(parents=True)
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 600), False).save(image)
    return PageImage("r", 3, "r/0003.jpg", "", "", 400, 600)


def test_the_coordinate_box_is_cut_out_with_a_margin_and_enlarged(pages):
    assert llm.coordinate_image(pages, None) == config.PAGES_DIR / pages.image
    crop = llm.coordinate_image(pages, (0.6, 0.05, 0.9, 0.1))
    with pymupdf.open(config.PAGES_DIR / pages.image) as doc:
        page_width = doc[0].rect.width
    with pymupdf.open(crop) as doc:
        width, height = doc[0].rect.width, doc[0].rect.height
    assert width > 0.38 * page_width * 2  # 0.04 margin on each side, enlarged
    assert width / height == pytest.approx((0.38 * 400) / (0.13 * 600), rel=0.03)


def answers(monkeypatch, *logs_per_call):
    calls = iter(logs_per_call)
    made = []

    def produce(*args, **kwargs):
        def call():
            made.append(1)
            names = next(calls)
            return PageExtraction(logs=[_log(n, COORDS, 30, [(0, 5)]) for n in names], coordinate_rows=[])
        return call

    monkeypatch.setattr(llm, "_produce", produce)
    return made


def test_a_page_that_comes_back_short_is_read_once_more(pages, monkeypatch):
    made = answers(monkeypatch, [], ["BH1"])
    assert [log.name for log in llm.extract_page(pages, "borehole_log", expected=1).logs] == ["BH1"]
    assert len(llm.extract_page(pages, "borehole_log", expected=1).logs) == 1 and len(made) == 2  # both cached
    assert json.loads(llm.cache_path("extract", pages).read_text())["result"]["logs"] == []  # first answer kept
    assert len(llm.cached_extraction(pages, "borehole_log", "", 1).logs) == 1
    assert len(llm.extract_page(pages, "borehole_log", expected=1, retry=False).logs) == 1 and len(made) == 2


def test_no_retry_when_the_count_is_met_or_the_retry_is_no_better(pages, monkeypatch):
    made = answers(monkeypatch, ["BH1"])
    llm.extract_page(pages, "borehole_log", expected=1)
    assert len(made) == 1 and not llm.cache_path("extract-retry", pages).exists()
    llm.cache_path("extract", pages).unlink()
    made = answers(monkeypatch, [], [])
    assert llm.extract_page(pages, "borehole_log", expected=1).logs == [] and len(made) == 2
