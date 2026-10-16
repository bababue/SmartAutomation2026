"""Linking lab sheet rows to boreholes (synthetic data)."""

import pytest

from pipeline.assemble import PageResult, link_lab_results
from pipeline.extraction_models import PageLabResults, XLabRow
from schema.borehole import Borehole, Layer, Location, Sample, SourceRef


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
    linked, unmatched = link_lab_results([a, b], [page])
    assert (linked, unmatched) == (3, 2)
    assert [(r.parameter.value, r.value, r.top_m) for r in a.lab_results] == [("ucs", 12.5, 2.0)]
    k = next(r for r in b.lab_results if r.parameter.value == "hydraulic_conductivity")
    assert k.value == pytest.approx(3e-7) and k.source.page == 9


def test_single_hole_document_takes_rows_without_name():
    a = hole("TP1")
    linked, unmatched = link_lab_results([a], [lab_page(row(None, None, "ucs", "40", "MPa", top=1.0))] * 2)
    assert (linked, unmatched) == (2, 0) and len(a.lab_results) == 1  # same value read twice: kept once


def test_value_on_log_and_lab_sheet_is_kept_once():
    from schema.borehole import LabResult
    a = hole("BH17")
    a.lab_results = [LabResult(parameter="ucs", value=28.9, value_raw="28.9 MPa", top_m=2.1)]  # from the log page
    link_lab_results([a], [lab_page(row("BH17", "019/13", "ucs", "28.9", "MPa", top=2.1))])
    assert len(a.lab_results) == 1 and a.lab_results[0].sample_name == "019/13"
