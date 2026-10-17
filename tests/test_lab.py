"""Linking lab sheet rows to boreholes (synthetic data)."""

import pytest

from pipeline.assemble import PageResult, link_lab_results
from pipeline.extraction_models import PageLabResults, XLabRow
from schema.borehole import Borehole, InvestigationType, Layer, Location, Sample, SourceRef


def hole(name: str, samples=()) -> Borehole:
    src = SourceRef(file="r.pdf", page=1)
    return Borehole(borehole_id=f"r:{name}", borehole_name=name, location=Location(),
                    layers=[Layer(top_m=0, bottom_m=10, description_original="Clay", primary="clay", source=src)],
                    samples=[Sample(name=s, top_m=t, bottom_m=b) for s, t, b in samples])


def row(hole_name, sample, parameter, value, unit, top=None):
    return XLabRow(hole_name=hole_name, sample_name=sample, top_m=top, bottom_m=None, parameter=parameter,
                   parameter_raw=parameter, value_raw=value, unit_raw=unit)


def lab_page(*rows) -> PageResult:
    return PageResult("r", "r.pdf", 9, "lab_results", None, labs=PageLabResults(rows=list(rows), unreadable=[]))


def test_rows_link_by_hole_then_unique_sample():
    a = hole("BH-01", [("U1", 2.0, 2.45)])
    b = hole("BH-2", [("U1", 3.0, 3.45), ("U7", 6.0, 6.45)])
    page = lab_page(
        row("BH 1", "U1", "ucs", "12,5", "MPa"),            # name variant, depth from the log's sample
        row(None, "U7", "water_content", "18", "%"),         # only BH-2 has U7
        row(None, "U1", "water_content", "20", "%"),         # U1 exists in both holes: ambiguous
        row("BH-9", None, "cai", "2.1", None, top=4.0),      # unknown hole
        row("BH-2", "U1", "hydraulic_conductivity", "3x10-5", "cm/s"),
        row("BH-2", "U1", "liquid_limit", "NP", "%"),        # no number
    )
    linked, unmatched, _ = link_lab_results([a, b], [page])
    assert (linked, unmatched) == (3, 2)
    assert [(r.parameter.value, r.value, r.top_m) for r in a.lab_results] == [("ucs", 12.5, 2.0)]
    k = next(r for r in b.lab_results if r.parameter.value == "hydraulic_conductivity")
    assert k.value == pytest.approx(3e-7) and k.source.page == 9


def test_single_hole_document_takes_rows_without_name():
    a = hole("TP1")
    linked, unmatched, _ = link_lab_results([a], [lab_page(row(None, None, "ucs", "40", "MPa", top=1.0))] * 2)
    assert (linked, unmatched) == (2, 0) and len(a.lab_results) == 1  # same value read twice: kept once


def test_value_on_log_and_lab_sheet_is_kept_once():
    from schema.borehole import LabResult
    a = hole("BH17")
    a.lab_results = [LabResult(parameter="ucs", value=28.9, value_raw="28.9 MPa", top_m=2.1)]  # from the log page
    link_lab_results([a], [lab_page(row("BH17", "019/13", "ucs", "28.9", "MPa", top=2.1))])
    assert len(a.lab_results) == 1 and a.lab_results[0].sample_name == "019/13"


def test_units_are_converted_or_the_value_is_dropped():
    from pipeline.assemble import _lab_value, lab_qualifier
    from schema.borehole import LabParameter as P
    assert _lab_value(P.ucs, "1500", "psi") == pytest.approx(10.342, rel=1e-3)
    assert _lab_value(P.ucs, "25", "MPa") == 25 and _lab_value(P.ucs, "25", None) == 25
    assert _lab_value(P.ucs, "25", "bananas") is None  # never publish a number in the wrong unit
    assert _lab_value(P.cai, "2.1", "-") == 2.1
    assert lab_qualifier("<0.01") == "<" and lab_qualifier("5 - 10") == "range" and lab_qualifier("12.5") is None


def test_lab_range_bounds_use_the_same_target_unit():
    from pipeline.assemble import lab_upper
    assert lab_upper("ucs", "5–10", "kPa") == pytest.approx(0.01)
    assert lab_upper("ucs", "5", "kPa") is None
    bh = hole("BH1")
    link_lab_results([bh], [lab_page(row("BH1", None, "ucs", "5000–10000", "kPa", top=2))])
    assert (bh.lab_results[0].value, bh.lab_results[0].value_upper) == (5, 10)


@pytest.mark.parametrize("text", ["2 × 10-6", "2x10^-6", "2e-6", "2,0E-6"])
def test_scientific_exponents_are_exact_values(text):
    from pipeline.assemble import lab_qualifier, lab_upper
    assert lab_qualifier(text) is None
    assert lab_upper("hydraulic_conductivity", text, "m/s") is None



def test_hole_id_in_sample_column_suffixes_and_implausible_values():
    holes = [hole("QX-10"), hole("QX-11"), hole("QX-01")]
    page = lab_page(
        row(None, "QX10", "water_content", "33.0", "%", top=2.5),     # hole id in the sample column
        row("QX 16 - FHT", None, "hydraulic_conductivity", "1e-7", "m/s"),  # unknown hole: stays unmatched
        row("QX10 Contam Land", None, "water_content", "30", "%", top=1.0),  # known hole + word
        row("QX11A", None, "water_content", "25", "%", top=2.0),       # re-drill: another hole
        row("QX-01", None, "ucs", "9100", None, top=3.0),              # 9100 MPa is impossible
    )
    linked, unmatched, implausible = link_lab_results(holes, [page])
    assert (linked, unmatched, implausible) == (2, 2, 1)
    assert len(holes[0].lab_results) == 2 and not holes[1].lab_results and not holes[2].lab_results


def test_number_shared_by_borehole_and_test_pit_takes_the_type_that_fits_the_whole_sheet():
    def typed(name: str, kind: str, depth: float) -> Borehole:
        bh = hole(name)
        bh.investigation_type, bh.layers[0].bottom_m = InvestigationType(kind), depth
        return bh
    bh1, bh2, tp1, tp2 = typed("1", "borehole", 6), typed("2", "borehole", 6), typed("1", "test_pit", 2.5), \
        typed("2", "test_pit", 1.5)
    # The 4.5 m samples fit only the boreholes, so the shallow samples of the same sheet are theirs too.
    rocks = lab_page(*(row(h, None, "ucs", str(10 + top), "MPa", top=top) for h, top in
                       [("1", 1.5), ("1", 3.0), ("1", 4.5), ("2", 0.5), ("2", 4.5)]))
    assert link_lab_results([bh1, bh2, tp1, tp2], [rocks]) == (5, 0, 0)
    assert [r.top_m for r in bh1.lab_results] == [1.5, 3.0, 4.5] and len(bh2.lab_results) == 2
    assert not tp1.lab_results and not tp2.lab_results
    # Shallow samples alone fit both types: they stay unmatched rather than go to a guessed hole.
    assert link_lab_results([bh1, bh2, tp1, tp2], [lab_page(row("1", None, "water_content", "12", "%", top=1.0))]) \
        == (0, 1, 0)
    # A sheet that names the type keeps it for its bare numbers, even where another type would fit deeper rows.
    pits = lab_page(row("TP 1", None, "water_content", "15", "%", top=1.0),
                    row("2", None, "water_content", "9", "%", top=4.0))
    assert link_lab_results([bh1, bh2, tp1, tp2], [pits]) == (1, 1, 0) and len(bh2.lab_results) == 2


def test_number_shared_by_borehole_and_test_pit_goes_to_the_hole_sampled_at_that_depth():
    # A pit table and a borehole table on one page, both read as bare numbers; both holes are deep enough.
    bh1, bh4 = hole("1", [("C2", 1.5, 3.0)]), hole("4", [("2", 1.5, 3.0)])
    tp1, tp4 = hole("1", [(None, 1.8, 1.8), (None, 2.7, 2.7)]), hole("4", [(None, 1.0, 1.0)])
    for bh, kind, depth in ((bh1, "borehole", 6), (bh4, "borehole", 6), (tp1, "test_pit", 2.7), (tp4, "test_pit", 1.6)):
        bh.investigation_type, bh.layers[0].bottom_m = InvestigationType(kind), depth
    pit_row = row("1", None, "other", "8.89", None, top=2.7)
    core_row = row("4", None, "other", "8.61", None, top=1.5)
    core_row.bottom_m = 3.0
    shallow = row("1", None, "other", "8.0", None, top=0.5)  # no hole sampled there: left unmatched
    assert link_lab_results([bh1, bh4, tp1, tp4], [lab_page(pit_row, core_row, shallow)]) == (2, 1, 0)
    assert len(tp1.lab_results) == len(bh4.lab_results) == 1 and not bh1.lab_results and not tp4.lab_results


def test_chemistry_rows_with_the_same_detection_limit_are_kept_apart():
    def chem(name: str, value: str, unit: str = "ug/l") -> XLabRow:
        return XLabRow(hole_name="MW-3", sample_name=None, top_m=None, bottom_m=None, parameter="other",
                       parameter_raw=name, value_raw=value, unit_raw=unit)
    a = hole("MW-3")
    first = lab_page(chem("Benzene", "<1"), chem("Toluene", "<1"), chem("Xylene", "<1e-3"))
    # The same certificate printed again; this copy repeats the unit in the row name.
    second = lab_page(chem("Benzene ug/l", "<1"), chem("Toluene (ug/l)", "<1"), chem("Xylene ug/l", "<1e-3"))
    link_lab_results([a], [first, second])
    assert sorted(r.parameter_raw for r in a.lab_results) == ["Benzene", "Toluene", "Xylene"]
    # Another unit or a detected value instead of the detection limit is another result.
    link_lab_results([a], [lab_page(chem("Benzene", "<1", "mg/l"), chem("Toluene", "1"))])
    assert len(a.lab_results) == 5


def test_duplicate_record_hands_its_lab_values_to_the_kept_one():
    from pipeline.assemble import dedupe
    from schema.borehole import GeorefMethod, LabResult
    def located(doc: str, confidence: float, labs: list[tuple[float, float]]) -> Borehole:
        bh = hole("BH-4")
        bh.borehole_id, bh.confidence = f"{doc}:BH4", confidence
        bh.location = Location(lat=48.0, lon=8.0, method=GeorefMethod.stated_coordinates)
        bh.lab_results = [LabResult(parameter="ucs", value=v, top_m=t) for t, v in labs]
        return bh
    report = located("report", 0.5, [(1.0, 20.0), (2.0, 25.0)])  # only the report has the lab sheet
    image = located("report_png", 0.9, [(2.0, 25.0)])            # the image export of the log page wins
    kept, = dedupe([report, image])
    assert kept.borehole_id == image.borehole_id and sorted((r.top_m, r.value) for r in kept.lab_results) == [(1.0, 20.0), (2.0, 25.0)]
    # A test pit of the same name at the same place is another hole: nothing is merged.
    pit = located("pits", 0.4, [(0.5, 3.0)])
    pit.investigation_type = InvestigationType.test_pit
    assert len(dedupe([pit, located("report_png", 0.9, [])])) == 2


def test_lab_values_from_a_log_in_feet_keep_the_kept_record_in_review():
    from pipeline.assemble import dedupe
    from schema.borehole import GeorefMethod, LabResult, ReviewStatus
    def located(doc: str, confidence: float) -> Borehole:
        bh = hole("BH-4")
        bh.borehole_id, bh.confidence = f"{doc}:BH4", confidence
        bh.location = Location(lat=48.0, lon=8.0, method=GeorefMethod.stated_coordinates)
        return bh
    report, image = located("report", 0.5), located("report_png", 0.95)
    report.issues = ["depths printed in feet (ft); check the conversion to metres"]
    report.lab_results = [LabResult(parameter="ucs", value=20.0, top_m=1.5)]
    image.review_status = ReviewStatus.auto_approved
    kept, = dedupe([report, image])
    assert kept.borehole_id == image.borehole_id and len(kept.lab_results) == 1
    assert kept.review_status == ReviewStatus.pending_review and report.issues[0] in kept.issues
