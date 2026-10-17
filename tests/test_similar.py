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
