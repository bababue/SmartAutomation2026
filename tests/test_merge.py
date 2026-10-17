"""One record per hole across documents (synthetic data, public landmarks)."""

from datetime import date

from pipeline.assemble import unstack_place_positions
from pipeline.dedupe import dedupe
from pipeline.scoring import finding_severity
from schema.borehole import SPT, Borehole, GeorefMethod, Layer, Lithology, Location, ReviewStatus, SourceRef

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


def test_spt_of_both_documents_is_kept_once():
    log = record("report", [(0, 9.0)], MINSTER)
    drawing = record("drawing", [(0, 9.0)], NEAR_MINSTER)
    log.spt = [SPT(depth_m=1.5, blows=[3, 4, 5], n_value=9)]
    drawing.spt = [SPT(depth_m=1.5, blows=[3, 4, 5], n_value=9), SPT(depth_m=3.0, blows=[6, 7, 8], n_value=15)]
    kept, = merge(log, drawing)
    assert [(s.depth_m, s.n_value) for s in kept.spt] == [(1.5, 9), (3.0, 15)]


def test_spt_differing_in_refusal_or_increment_is_kept_from_both():
    log = record("report", [(0, 9.0)], MINSTER)
    drawing = record("drawing", [(0, 9.0)], NEAR_MINSTER)
    log.spt = [SPT(depth_m=1.5, blows=[], n_value=None), SPT(depth_m=3.0, blows=[5, 6, 7], increment_mm=150)]
    drawing.spt = [SPT(depth_m=1.5, blows=[], n_value=None, refusal=True, raw="R"),
                   SPT(depth_m=3.0, blows=[5, 6, 7], increment_mm=75)]
    kept, = merge(log, drawing)
    assert any(s.refusal for s in kept.spt if s.depth_m == 1.5)
    assert sorted(s.increment_mm for s in kept.spt if s.depth_m == 3.0) == [75, 150]


def test_same_boundaries_with_another_lithology_block():
    log = record("report", [(0, 2.0), (2.0, 9.0)], MINSTER, depth_m=9.0)
    drawing = record("drawing", [(0, 2.0), (2.0, 9.0)], NEAR_MINSTER, depth_m=9.0)
    drawing.layers[1].primary = Lithology.sand
    kept, = merge(log, drawing)
    disagree, = [i for i in blocking(kept) if i.startswith("sources disagree on the lithology")]
    assert disagree.startswith("sources disagree on the lithology of layer 2–9 m:")
    assert "clay" in disagree and "sand" in disagree


def test_a_corrected_position_is_evidence_against_a_merge():
    log = record("report", [(0, 9.0)], MINSTER, project_id="R-17")
    drawing = record("drawing", [(0, 9.0)], NEAR_MINSTER, project_id="R-17")
    moved = drawing.model_copy(deep=True)  # a reviewer put it 1 km north
    moved.location.lat += 0.009
    records = dedupe([log, drawing], judged={log.borehole_id: log, drawing.borehole_id: moved})
    assert len(records) == 2
    assert all(any(i.startswith("possibly the same hole as") for i in r.issues) for r in records)


def test_a_placeholder_never_joins_a_real_hole():
    from pipeline.assemble import placeholder
    failed = placeholder("site__a", "site__a.pdf", 1, "log not assembled: BH1 on page 1 (ValueError)", "BH1")
    real = record("site__b", [(0, 9.0)], MINSTER, name="P1")
    for protected in ({}, {failed.borehole_id: {"uid-a", failed.borehole_id}}):
        records = dedupe([failed.model_copy(deep=True), real.model_copy(deep=True)], protected)
        kept, = [r for r in records if r.borehole_id == real.borehole_id]
        assert len(records) == 2 and kept.layers and not blocking(kept)


def test_same_hole_number_in_one_upload_batch_is_one_hole():
    drawing, copy = record("upload-x__drawing", [(0, 2), (2, 6)]), record("upload-x__copy", [(0, 2), (2, 6)])
    elsewhere = record("other__drawing", [(0, 2), (2, 6)])  # another folder: the name alone is no evidence
    records = merge(drawing, copy, elsewhere)
    assert len(records) == 2
    kept, = [r for r in records if r.borehole_id.startswith("upload-x")]
    assert any(i.startswith("same hole assumed") for i in blocking(kept))


def test_longer_name_ending_in_the_hole_name_joins_it_in_one_batch():
    log = record("upload-x__log", [(0, 2), (2, 6)], MINSTER, name="Road X 12/14 B 2")
    drawing = record("upload-x__drawing", [(0, 2), (2, 6)])
    other_series = record("upload-x__plan", [(0, 2), (2, 6)], name="AB 2")  # whole tokens only
    records = merge(log, drawing, other_series)
    assert sorted(r.borehole_name for r in records) == ["AB 2", "B 2"]
    kept, = [r for r in records if r.borehole_name == "B 2"]
    assert (kept.location.lat, kept.location.lon) == MINSTER  # the printed position wins over none


def test_holes_sharing_a_place_name_position_leave_the_map():
    def placed(doc: str, name: str) -> Borehole:
        bh = record(doc, [(0, 2)], name=name)
        bh.borehole_id = f"{doc}:{name.replace(' ', '')}"
        bh.location = Location(lat=MINSTER[0], lon=MINSTER[1], method=GeorefMethod.geocoded_place,
                               accuracy_m=5000, confidence=0.3)
        bh.issues.append("position from place name only")
        return bh

    first, second, reviewed = (placed("upload-x__plan", n) for n in ("B 1", "B 2", "B 3"))
    alone = placed("upload-y__plan", "B 1")  # the only hole of its batch at that place
    unstack_place_positions([first, second, reviewed, alone], keep={reviewed.borehole_id})
    for bh in (first, second):
        assert bh.location.lat is None and bh.location.method is None
        assert any(i.startswith("not georeferenced: 3 holes") for i in bh.issues)
        assert "position from place name only" not in bh.issues
        assert bh.review_status != ReviewStatus.auto_approved
    assert reviewed.location.lat == alone.location.lat == MINSTER[0]
