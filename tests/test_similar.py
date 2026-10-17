"""Similar ground: hand-made layer lists, no database."""

import pytest

from api.similar import distance, profile, reason


def test_clay_over_rock_is_closer_to_clay_over_rock_than_to_sand():
    clay_on_rock = profile([(0, 0.5, "topsoil"), (0.5, 8, "clay"), (8, 12, "limestone")],
                           {"rock_head_m": 8, "groundwater_m": 2})
    silty_clay_on_rock = profile([(0, 7, "boulder_clay"), (7, 15, "sandstone"), (15, 16, "no_recovery")],
                                 {"rock_head_m": 7, "groundwater_m": 3})
    sand = profile([(0, 12, "medium_sand")], {"rock_head_m": None, "groundwater_m": 2})

    assert distance(clay_on_rock, silty_clay_on_rock) < distance(clay_on_rock, sand)
    assert distance(clay_on_rock, clay_on_rock) == 0
    assert distance(clay_on_rock, sand) == pytest.approx(0.85)  # nothing shared in bands 0-5 and 5-10, same water level
    assert reason(clay_on_rock) == "rock from 8 m, mostly clay above; water at 2 m"


def test_holes_without_a_common_logged_band_are_not_compared():
    shallow = profile([(0, 3, "fill")])  # 3 of 5 m logged: band 0-5 counts
    deep_only = profile([(12, 30, "plutonic_rock")])
    assert shallow.bands[0] == {"made ground": 1.0}
    assert distance(shallow, deep_only) is None


def test_a_shallow_hole_matches_a_deep_one_only_on_the_bands_it_logs():
    deep, shallow = profile([(0, 20, "clay")]), profile([(0, 5, "clay")])
    assert distance(deep, shallow) == pytest.approx((0 + 0.5 + 0.5) / 3)  # bands 5-10 and 10-20 not logged
    assert distance(shallow, deep) == 0


def test_similar_holes_leave_out_the_same_document_rejected_records_and_holes_without_layers(con):
    from api.similar import similar
    from pipeline import store
    from schema.borehole import Borehole, GroundwaterObservation, Layer, Location, ReviewStatus, SourceRef

    def hole(bid, layers, water=None, status="auto_approved"):
        src = SourceRef(file="logs.pdf", page=1)
        return Borehole(borehole_id=bid, borehole_name=bid.split(":")[1], review_status=status, sources=[src],
                        layers=[Layer(top_m=t, bottom_m=b, description_original=c, primary=c, source=src)
                                for t, b, c in layers],
                        groundwater=[GroundwaterObservation(depth_m=water, kind="standing")] if water else [],
                        location=Location(lat=51.5007, lon=-0.1246))  # Big Ben

    for bh in [hole("A:T", [(0, 6, "clay"), (6, 15, "limestone")], water=2.0),
               hole("A:SAME", [(0, 6, "clay"), (6, 15, "limestone")], water=2.0),
               hole("B:CLOSE", [(0, 7, "boulder_clay"), (7, 15, "sandstone")], water=3.0),
               hole("B:SAND", [(0, 25, "medium_sand")]),
               hole("C:REJECTED", [(0, 6, "clay"), (6, 15, "limestone")], status=ReviewStatus.rejected),
               hole("C:EMPTY", []),
               hole("D:DEEP", [(0, 45, "clay"), (45, 50, "limestone")])]:
        store.save_borehole(con, bh)

    found = similar(con, "A:T", limit=5)
    assert found["ground"] == "rock from 6 m, mostly clay above; water at 2 m"
    assert [(s["id"], s["similarity"], s["reason"]) for s in found["similar"]] == [
        ("B:CLOSE", 0.91, "rock from 7 m, mostly clay above; water at 3 m"),
        ("D:DEEP", 0.4, "rock from 45 m, mostly clay above"),
        ("B:SAND", 0.0, "mostly sand, no rock reached")]
    assert similar(con, "missing") is None
