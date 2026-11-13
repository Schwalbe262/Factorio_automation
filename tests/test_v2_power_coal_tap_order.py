"""Power coal tap: taps toward the boiler feed are routed first (fleet aud-1114-0526 seed 3111291866).

The coal trunk runs east at y -12.5 just south of the boiler feed (west at y -15.5). Taps were tried
in trunk order with the right (south) side first: the first two taps faced south, away from every
join at y -14.5, so their routes had to wrap the trunk; the 6 native route attempts of a prepare
were spent on them (joins 30-33: ``no_clean_trunk_corridor``) and the north-facing taps two tiles
from the feed were never routed. ``mine_trunk_tap_unavailable`` held the boiler on the old feed.
"""

from __future__ import annotations

import unittest

from factorio_agent.power_coal_tap import join_rank


def tap(x, y, facing, side):
    return {"port": {"position": {"x": x, "y": y}, "facing": facing}, "side": side, "along": x}


def join(x, y, index):
    return {"index": index, "port": {"position": {"x": x, "y": y}, "facing": 0}}


class TapOrderTests(unittest.TestCase):
    def test_a_tap_facing_the_feed_ranks_before_one_facing_away(self):
        joins = [join(45.5 + i, -14.5, 30 + i) for i in range(4)]
        south = [tap(47.5, -10.5, 8, "right"), tap(48.5, -10.5, 8, "right")]
        north = [tap(47.5, -13.5, 0, "left"), tap(52.5, -13.5, 0, "left")]
        ranked = sorted(south + north, key=lambda t: join_rank(t, joins))
        self.assertEqual([t["side"] for t in ranked], ["left", "left", "right", "right"])
        self.assertEqual(ranked[0]["port"]["position"], {"x": 47.5, "y": -13.5})

    def test_without_joins_the_order_is_kept(self):
        taps = [tap(1.5, 0.5, 8, "right"), tap(2.5, 0.5, 0, "left")]
        self.assertEqual(sorted(taps, key=lambda t: join_rank(t, [])), taps)


if __name__ == "__main__":
    unittest.main()
