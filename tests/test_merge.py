"""One record per hole across documents (synthetic data, public landmarks)."""

from datetime import date

from pipeline.assemble import dedupe
from pipeline.scoring import finding_severity
from schema.borehole import Borehole, GeorefMethod, Layer, Location, ReviewStatus, SourceRef

# Freiburg Minster; the second point is about 10 m east of it.
MINSTER = (47.99552, 7.85291)
NEAR_MINSTER = (47.99552, 7.85304)


def record(doc: str, layers: list[tuple[float, float]], position=None, **header) -> Borehole:
    src = SourceRef(file=f"{doc}.pdf", page=1)
    location = Location(lat=position[0], lon=position[1], method=GeorefMethod.stated_coordinates, confidence=0.95) \
        if position else Location()
    bh = Borehole(borehole_id=f"{doc}:B2", borehole_name=header.pop("name", "B 2"), location=location, sources=[src],
                  layers=[Layer(top_m=t, bottom_m=b, description_original="Clay", primary="clay", source=src)
                          for t, b in layers], **header)
    bh.issues = bh.validate_log()
    return bh


def merge(*records: Borehole) -> list[Borehole]:
    return dedupe(list(records))


def blocking(bh: Borehole) -> list[str]:
    return [i for i in bh.issues if finding_severity(i) == "blocking"]


def test_same_position_combines_and_fills_the_header():
    log = record("report", [(0, 1.2), (1.2, 9.0)], MINSTER, depth_m=9.0)
    drawing = record("drawing", [(0, 1.2), (1.2, 9.0)], NEAR_MINSTER, surface_elevation_m=278.4,
                     surface_elevation_raw="278,4 m a.s.l.", contractor="Example Drilling Ltd")
    log.confidence, drawing.confidence = 0.9, 0.6
    kept, = merge(log, drawing)
    assert kept.borehole_id == "report:B2" and kept.depth_m == 9.0
    assert kept.surface_elevation_m == 278.4 and kept.contractor == "Example Drilling Ltd"
    assert kept.field_sources["surface_elevation_m"].file == "drawing.pdf"
    assert {s.file for s in kept.sources} == {"report.pdf", "drawing.pdf"}
    assert any(i.startswith("combined with the record from drawing.pdf") for i in kept.issues)
    assert not blocking(kept)


def test_header_match_without_coordinates_goes_to_review():
    # A driller's log with few layers and a geologist's profile of the same hole with more units.
    header = dict(client="District Office", contractor="Example Drilling Ltd")
    log = record("log", [(0, 1), (1, 27), (27, 64.4), (64.4, 77.9)], depth_m=77.9, start_date=date(1978, 6, 6),
                 **header)
    profile = record("profile", [(0, 1), (1, 14.5), (14.5, 15), (15, 42), (42, 55), (55, 77.5)], depth_m=77.5,
                     surface_elevation_m=307.4, start_date=date(1989, 6, 6), **header)
    kept, = merge(log, profile)
    assert kept.borehole_id == "profile:B2" and len(kept.layers) == 6  # the more detailed sequence
    assert kept.surface_elevation_m == 307.4
    assert any(i.startswith("same hole assumed in log.pdf") for i in blocking(kept))
    assert any(i.startswith("sources disagree on the layer sequence") for i in blocking(kept))
    assert any(i.startswith("sources disagree on final depth: 77.5 m") for i in blocking(kept))
    assert any(i.startswith("sources differ on drilling start date") for i in kept.issues)
    assert kept.review_status == ReviewStatus.pending_review


def test_checks_of_the_other_record_are_rerun_on_the_combined_one():
    from schema.borehole import Sample
    log = record("report", [(0, 1.2), (1.2, 9.0)], MINSTER, depth_m=9.0)
    export = record("export", [(0, 1.2), (1.2, 9.0)], NEAR_MINSTER)
    export.samples = [Sample(name="U1", top_m=8.0, bottom_m=8.45)]
    export.depth_m = 5.0  # misread on the export: its own check flags the sample
    export.issues = export.validate_log()
    assert any(i.startswith("sample") for i in export.issues)
    log.confidence, export.confidence = 0.9, 0.5
    kept, = merge(log, export)
    assert len(kept.samples) == 1 and not any(i.startswith("sample") for i in kept.issues)


def test_file_names_in_merge_findings_do_not_block():
    log = record("report", [(0, 1.2), (1.2, 9.0)], MINSTER, depth_m=9.0)
    export = record("Singapore_export", [(0, 1.2), (1.2, 9.0)], NEAR_MINSTER)
    log.confidence, export.confidence = 0.9, 0.5
    kept, = merge(log, export)
    assert any("Singapore" in i for i in kept.issues) and not blocking(kept)


def test_reviewer_value_settles_a_disagreement():
    from pipeline import corrections
    header = dict(client="District Office", contractor="Example Drilling Ltd")
    kept, = merge(record("log", [(0, 9.0)], depth_m=9.0, **header),
                  record("profile", [(0, 9.0)], depth_m=9.5, **header))
    assert any(i.startswith("sources disagree on final depth") for i in kept.issues)
    corrections.apply(kept, [{"kind": "field", "payload": {"field": "depth_m", "value": 9.0}, "reviewer": "r"}])
    assert not any(i.startswith("sources disagree on final depth") for i in kept.issues)
    assert any(i.startswith("same hole assumed") for i in kept.issues)  # the match itself still needs a decision


def test_contradicting_evidence_keeps_both_and_flags_them():
    near = record("phase1", [(0, 5)], MINSTER, project_id="J-100")
    far = record("phase2", [(0, 5)], (48.00, 7.92), project_id="J-100")  # about 5 km away
    a, b = merge(near, far)
    for bh in (a, b):
        assert any(i.startswith("possibly the same hole as B 2") for i in bh.issues)
        assert bh.review_status == ReviewStatus.pending_review
    # Same client and contractor, but another project and no position: a wrong combination could not be undone.
    header = dict(client="District Office", contractor="Example Drilling Ltd")
    a, b = merge(record("job1", [(0, 5)], project_name="Bridge North", **header),
                 record("job2", [(0, 5)], project_name="Harbour Quay", **header))
    assert all(any(i.endswith("project name differs") for i in bh.issues) for bh in (a, b))


def test_a_shared_name_alone_is_no_evidence():
    a, b = merge(record("site-a", [(0, 5)], client="Client A"), record("site-b", [(0, 6)], client="Client B"))
    assert a.issues == a.validate_log() and b.issues == b.validate_log()


def test_holes_of_one_document_are_never_combined():
    first = record("report", [(0, 5)], MINSTER)
    second = record("report", [(0, 5)], NEAR_MINSTER)
    second.borehole_id = "report:B2-p3"
    assert len(merge(first, second)) == 2
