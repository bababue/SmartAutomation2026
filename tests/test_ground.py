"""Rock head and groundwater level derived from a log (synthetic data)."""

from pipeline.ground import summarize
from schema.borehole import Borehole, GroundwaterObservation, Layer, Location


def hole(*layers, groundwater=()) -> Borehole:
    """layers: (top, bottom, primary) or (top, bottom, primary, weathering)."""
    return Borehole(
        borehole_id="t:BH1", borehole_name="BH1", location=Location(),
        layers=[Layer(top_m=t, bottom_m=b, description_original="x", primary=p, weathering=w[0] if w else None)
                for t, b, p, *w in layers],
        groundwater=list(groundwater),
    )


def test_rock_head_is_the_top_of_rock_reaching_the_bottom():
    g = summarize(hole((0, 2, "fill"), (2, 6, "plutonic_rock"), (6, 7, "clay"), (7, 9, "sandstone"),
                       (9, 9.5, "no_recovery"), (9.5, 12, "sandstone")))
    # a boulder or rock band above soil is not the rock head; core loss inside the rock does not break it
    assert (g.rock_head_m, g.weathered_rock_top_m, g.final_depth_m) == (7, None, 12)


def test_decomposed_rock_above_the_rock_head():
    g = summarize(hole((0, 3, "clay"), (3, 5, "plutonic_rock", "completely weathered (W5)"),
                       (5, 8, "plutonic_rock", "slightly weathered")))
    assert (g.rock_head_m, g.weathered_rock_top_m) == (5, 3)


def test_rock_not_reached():
    assert summarize(hole((0, 4, "sand"), (4, 6, "clay"))).rock_head_m is None
    only_decomposed = summarize(hole((0, 3, "clay"), (3, 5, "plutonic_rock", "residual soil, grade VI")))
    assert (only_decomposed.rock_head_m, only_decomposed.weathered_rock_top_m) == (None, None)


def test_roman_grade_needs_capitals():
    # 'v' in ordinary text is not weathering grade V
    assert summarize(hole((0, 2, "sand"), (2, 5, "limestone", "slightly weathered, v. strong"))).rock_head_m == 2


def test_groundwater_below_ground_only():
    g = summarize(hole((0, 5, "sand"), groundwater=[
        GroundwaterObservation(depth_m=1.0, reference="casing"),
        GroundwaterObservation(depth_m=None, dry=True),
        GroundwaterObservation(depth_m=2.4, kind="struck"),
        GroundwaterObservation(depth_m=3.1, kind="standing", reference="ground"),
    ]))
    assert g.groundwater_m == 2.4
