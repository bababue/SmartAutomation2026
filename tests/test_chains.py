"""Extraction order across continuation sheets and the previous-sheet context (synthetic data)."""

from pipeline.__main__ import _chains, _walk
from pipeline.extraction_models import LogRef, PageClassification, PageExtraction, PageKind, XLayer, XLog
from pipeline.ingest import PageImage
from pipeline.llm import sheet_context


def page(doc: str, n: int) -> PageImage:
    return PageImage(doc_id=doc, page=n, image="", thumb="", text="", width=1, height=1)


def cls(kind: str, *names: str) -> PageClassification:
    return PageClassification(kind=PageKind(kind), logs=[LogRef(name=n, sheet=None) for n in names], language="en",
                              has_coordinates=False, crs_text=None)


def log(name: str, layers: list[XLayer] | None = None) -> XLog:
    empty = {k: None for k in XLog.model_fields}
    return XLog.model_validate({**empty, "name": name, "layers": layers or [], "spt": [], "core_runs": [],
                                "samples": [], "groundwater": [], "lab_results": [], "unreadable": []})


def layer(top, bottom, text, primary="sand") -> XLayer:
    return XLayer.model_validate({**{k: None for k in XLayer.model_fields}, "top_m": top, "bottom_m": bottom,
                                  "description_original": text, "primary": primary, "secondary": []})


def test_continuation_sheets_form_one_chain():
    pages = [page("d", 1), page("d", 2), page("d", 3), page("d", 4), page("d", 6), page("e", 1)]
    classes = {
        ("d", 1): cls("borehole_log", "BH-1"),
        ("d", 2): cls("borehole_log", "BH-01"),  # same hole, name written differently
        ("d", 3): cls("borehole_log", "BH-2"),
        ("d", 4): cls("coordinate_table", "BH-1", "BH-2"),
        ("d", 6): cls("borehole_log", "BH-2"),  # after a table page, within the gap
        ("e", 1): cls("borehole_log", "BH-2"),  # other document
    }
    chains = [[(p.doc_id, p.page) for p in ch] for ch in _chains(pages, classes)]
    assert sorted(chains) == sorted([[("d", 1), ("d", 2)], [("d", 3), ("d", 6)], [("d", 4)], [("e", 1)]])


def test_walk_passes_previous_sheet_and_survives_failures():
    pages = [page("d", 1), page("d", 2), page("d", 3)]
    classes = {k: cls("borehole_log", "BH-1") for k in [("d", 1), ("d", 2), ("d", 3)]}
    seen = []

    def extract(p, kind, context, expected):
        seen.append(context)
        if p.page == 2:
            raise RuntimeError("boom")
        return PageExtraction(logs=[log("BH-1", [layer(0, 5, "Clayey SAND")])], coordinate_rows=[])

    results, failed = _walk(pages, classes, extract)
    assert set(results) == {("d", 1), ("d", 3)} and len(failed) == 1
    assert seen[0] == "" and "Clayey SAND" in seen[1] and seen[2] == ""  # no context after a failed sheet


def test_sheet_context_is_compact():
    x = PageExtraction(logs=[log("BH-1", [layer(0, 2, "a"), layer(2, 5, "b"), layer(5, 9, "granite", "plutonic_rock")])],
                       coordinate_rows=[])
    text = sheet_context(x)
    assert "BH-1" in text and "5-9 m, plutonic_rock: granite" in text and "0-2 m" not in text
    assert sheet_context(None) == ""
