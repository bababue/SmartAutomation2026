"""Text-layer confirmation of layer boundaries."""

from pipeline.scoring import text_check
from schema.borehole import Borehole, Layer, SourceRef


def test_depth_scale_labels_do_not_confirm_whole_metre_boundaries():
    src = SourceRef(file="demo.pdf", page=1)
    bh = Borehole(borehole_id="demo:BH1", borehole_name="BH1", layers=[
        Layer(top_m=0, bottom_m=2.0, description_original="Fill", primary="fill", source=src),
        Layer(top_m=2.0, bottom_m=4.8, description_original="Sand", primary="sand", source=src),
    ])
    scale = " ".join(str(m) for m in range(11))
    text = "Borehole log " + "x" * 300 + " " + scale + " 2.30 4.80"
    found, total, _, _ = text_check(bh, {("demo.pdf", 1): text})
    assert (found, total) == (1, 3)  # 4.80 is printed; 2.0 only as a scale label


def test_position_without_a_place_does_not_claim_a_place_mismatch():
    from pipeline.scoring import score
    from schema.borehole import GeorefMethod, Location

    bh = Borehole(borehole_id="demo:BH1", borehole_name="BH1", location=Location(
        lat=48.0, lon=7.85, method=GeorefMethod.stated_coordinates, confidence=0.5))
    bh.issues = ["no place to cross-check coordinates"]
    score(bh, {}, 0)
    texts = [f.text for f in bh.score_factors]
    assert "No named place to check the printed coordinates against" in texts
    assert not any("named place" in t and "match" in t for t in texts)
