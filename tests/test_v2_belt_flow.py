"""Belt lane flow model (belt_flow.py): lane rules, splitters, undergrounds and findings."""

import unittest

from factorio_agent import belt_flow as bf

E, S, W, N = 4, 8, 12, 0
LANE = bf.LANE_PER_MINUTE["transport-belt"]


def belt(x, y, d, name="transport-belt"):
    return {"name": name, "position": {"x": x + 0.5, "y": y + 0.5}, "direction": d}


def splitter(cx, cy, d, **settings):
    return {"name": "splitter", "position": {"x": cx, "y": cy}, "direction": d, **settings}


def underground(x, y, d, kind):
    return dict(belt(x, y, d, "underground-belt"), belt_to_ground_type=kind)


def side_load_then_split():
    """(1,0) east with an empty stub behind it, a full belt side-loaded from the north, then a
    splitter on tiles (2,-1) / (2,0) with one belt after each output (the screenshot shape)."""
    es = [belt(0, 0, E), belt(1, 0, E), belt(1, -1, S), splitter(2.5, 0.0, E), belt(3, -1, E), belt(3, 0, E)]
    return es, [bf.full_belt("iron", (1.5, -0.5))]


class LaneRuleTests(unittest.TestCase):
    def test_side_load_fills_only_the_near_lane(self):
        es = [belt(0, 0, E), belt(1, 0, E), belt(2, 0, E), belt(1, -1, S)]
        flow = bf.solve(es, [bf.full_belt("iron", (1.5, -0.5))], sinks=[(2, 0)])
        self.assertEqual(flow.at((2, 0)), {"left": {"iron": LANE}, "right": {}})
        # From the south the same feeder fills the right lane.
        es = [belt(0, 0, E), belt(1, 0, E), belt(2, 0, E), belt(1, 1, N)]
        flow = bf.solve(es, [bf.full_belt("iron", (1.5, 1.5))], sinks=[(2, 0)])
        self.assertEqual(flow.at((2, 0)), {"left": {}, "right": {"iron": LANE}})

    def test_a_belt_fed_only_from_one_side_is_a_curve_and_keeps_both_lanes(self):
        es = [belt(0, 0, E), belt(1, 0, S), belt(1, 1, S)]
        flow = bf.solve(es, [bf.full_belt("iron", (0.5, 0.5), lanes=("left",))], sinks=[(1, 1)])
        self.assertEqual(flow.at((1, 1)), {"left": {"iron": LANE}, "right": {}})
        both = bf.solve(es, [bf.full_belt("iron", (0.5, 0.5))], sinks=[(1, 1)])
        self.assertEqual(both.item_rate((1, 1), "iron"), 2 * LANE)

    def test_head_on_join_keeps_both_lanes(self):
        es = [belt(x, 0, E) for x in range(4)]
        flow = bf.solve(es, [bf.full_belt("iron", (0.5, 0.5))], sinks=[(3, 0)])
        self.assertEqual(flow.at((3, 0)), {"left": {"iron": LANE}, "right": {"iron": LANE}})

    def test_underground_pair_keeps_lanes(self):
        es = [
            belt(0, 0, E),
            underground(1, 0, E, "input"),
            belt(2, 0, N),
            underground(4, 0, E, "output"),
            belt(5, 0, E),
        ]
        flow = bf.solve(es, [bf.full_belt("copper", (0.5, 0.5), lanes=("right",))], sinks=[(5, 0)])
        self.assertEqual(flow.at((5, 0)), {"left": {}, "right": {"copper": LANE}})
        self.assertEqual(flow.item_rate((2, 0), "copper"), 0)  # the belt above the tunnel is another line

    def test_side_load_onto_an_underground_is_not_modelled(self):
        es = [belt(0, -1, S), underground(0, 0, E, "input"), underground(3, 0, E, "output")]
        flow = bf.solve(es, [bf.full_belt("iron", (0.5, -0.5))])
        self.assertIn("unmodelled_join", [f["kind"] for f in flow.findings])

    def test_inserter_drops_on_the_far_lane(self):
        self.assertEqual(bf.drop_lane(E, (0, -1)), "right")  # inserter north of an eastbound belt
        self.assertEqual(bf.drop_lane(E, (0, 1)), "left")
        es = [belt(0, 0, E), belt(1, 0, E)]
        flow = bf.solve(
            es, [{"tile": (0, 0), "item": "gear", "lane": "right", "rate": 30, "mode": "drop"}], sinks=[(1, 0)]
        )
        self.assertEqual(flow.at((1, 0)), {"left": {}, "right": {"gear": 30.0}})


class SplitterTests(unittest.TestCase):
    def test_head_on_then_splitter_gives_half_a_belt_to_each_branch(self):
        es = [belt(0, 0, E), belt(1, 0, E), splitter(2.5, 0.0, E), belt(3, -1, E), belt(3, 0, E)]
        flow = bf.solve(es, [bf.full_belt("iron", (0.5, 0.5))], sinks=[(3, -1), (3, 0)])
        for tile in ((3, -1), (3, 0)):
            self.assertEqual(flow.at(tile), {"left": {"iron": LANE / 2}, "right": {"iron": LANE / 2}})
        self.assertFalse(flow.findings)

    def test_side_load_then_splitter_gives_a_quarter_belt_per_branch(self):
        es, sources = side_load_then_split()
        flow = bf.solve(es, sources, sinks=[(3, -1), (3, 0)])
        for tile in ((3, -1), (3, 0)):
            self.assertEqual(flow.at(tile), {"left": {"iron": LANE / 2}, "right": {}})
        waste = [f for f in flow.findings if f["kind"] == "side_load_before_splitter"]
        self.assertEqual(len(waste), 1)
        self.assertEqual((waste[0]["feeder_per_minute"], waste[0]["per_branch_per_minute"]), (2 * LANE, LANE / 2))

    def test_a_branch_that_takes_less_leaves_the_rest_to_the_other(self):
        es, sources = side_load_then_split()
        flow = bf.solve(
            es, sources, [{"tile": (3, -1), "demand": {"iron": 300}}, {"tile": (3, 0), "demand": {"iron": 100}}]
        )
        self.assertAlmostEqual(flow.item_rate((3, 0), "iron"), 110)  # demand x margin
        self.assertAlmostEqual(flow.item_rate((3, -1), "iron"), 330)  # its own demand x margin; the rest backs up
        self.assertFalse([f for f in flow.findings if f["kind"] in ("single_lane_feed", "capacity_shortfall")])

    def test_two_hungry_branches_after_a_side_load_are_short(self):
        es, sources = side_load_then_split()
        flow = bf.solve(
            es, sources, [{"tile": (3, -1), "demand": {"iron": 300}}, {"tile": (3, 0), "demand": {"iron": 300}}]
        )
        short = [f for f in flow.findings if f["kind"] == "single_lane_feed"]
        self.assertEqual(len(short), 2)
        self.assertEqual(short[0]["delivered_per_minute"], LANE / 2)
        steps = [s["step"] for s in short[0]["trace"]]
        self.assertEqual(steps, ["splitter", "side_load"])

    def test_priority_output_fills_first_and_a_bounded_branch_passes_the_rest_on(self):
        es = [belt(0, 0, S), splitter(0.0, 1.5, S, splitter_output_priority="left"), belt(-1, 2, S), belt(0, 2, S)]
        # Facing south the left output is the east tile (0, 2).
        flow = bf.solve(es, [bf.full_belt("iron", (0.5, 0.5))], sinks=[(0, 2), (-1, 2)])
        self.assertEqual(flow.item_rate((0, 2), "iron"), 2 * LANE)
        self.assertEqual(flow.item_rate((-1, 2), "iron"), 0)
        flow = bf.solve(
            es, [bf.full_belt("iron", (0.5, 0.5))], [{"tile": (0, 2), "demand": {"iron": 100}}], sinks=[(-1, 2)]
        )
        self.assertAlmostEqual(flow.item_rate((0, 2), "iron"), 110)
        self.assertAlmostEqual(flow.item_rate((-1, 2), "iron"), 2 * LANE - 110)

    def test_filter_sends_its_item_to_the_priority_side(self):
        es = [belt(0, 0, S), splitter(0.0, 1.5, S, splitter_output_priority="left", splitter_filter="coal")]
        es += [belt(-1, 2, S), belt(0, 2, S)]
        sources = [
            bf.full_belt("coal", (0.5, 0.5), lanes=("left",)),
            bf.full_belt("stone", (0.5, 0.5), lanes=("right",)),
        ]
        flow = bf.solve(es, sources, sinks=[(0, 2), (-1, 2)])
        self.assertEqual(flow.at((0, 2)), {"left": {"coal": LANE}, "right": {}})
        self.assertEqual(flow.at((-1, 2)), {"left": {}, "right": {"stone": LANE}})

    def test_an_output_without_a_belt_backs_up_and_the_other_takes_everything(self):
        es = [belt(0, 0, E), belt(1, 0, E), splitter(2.5, 0.0, E), belt(3, 0, E)]
        flow = bf.solve(es, [bf.full_belt("iron", (0.5, 0.5))], sinks=[(3, 0)])
        self.assertEqual(flow.item_rate((3, 0), "iron"), 2 * LANE)


class FindingTests(unittest.TestCase):
    def test_a_sufficient_single_lane_side_load_is_not_a_finding(self):
        es = [belt(0, 0, E), belt(1, 0, E), belt(2, 0, E), belt(1, -1, S)]
        flow = bf.solve(es, [bf.full_belt("stone", (1.5, -0.5))], [{"tile": (2, 0), "demand": {"stone": 200}}])
        self.assertEqual(flow.findings, [])
        flow = bf.solve(es, [bf.full_belt("stone", (1.5, -0.5))], [{"tile": (2, 0), "demand": {"stone": 600}}])
        self.assertEqual([f["kind"] for f in flow.findings], ["single_lane_feed"])

    def test_a_consumer_its_item_never_reaches_is_starved(self):
        es = [belt(0, 0, E), belt(1, 0, E)]
        flow = bf.solve(es, [bf.full_belt("iron", (0.5, 0.5))], [{"tile": (1, 0), "demand": {"copper": 10}}])
        self.assertEqual([f["kind"] for f in flow.findings], ["lane_starvation"])

    def test_one_shortfall_per_line(self):
        es = [belt(x, 0, E) for x in range(4)] + [belt(0, -1, S)]
        es[0] = belt(-1, 0, E)  # empty stub west of the side-load tile (0, 0)
        es.append(belt(0, 0, E))
        consumers = [{"tile": (x, 0), "demand": {"ore": 200}} for x in (1, 2, 3)]
        flow = bf.solve(es, [bf.full_belt("ore", (0.5, -0.5))], consumers)
        short = [f for f in flow.findings if f["kind"] == "single_lane_feed"]
        self.assertEqual([tuple(f["tile"]) for f in short], [(1, 0)])
        self.assertEqual(short[0]["demand_per_minute"], 600)

    def test_uneven_parallel_belts_are_reported(self):
        es = [belt(0, 0, E), belt(1, 0, E), belt(0, 1, E), belt(1, 1, E)]
        sources = [bf.full_belt("ore", (0.5, 0.5)), bf.full_belt("ore", (0.5, 1.5), lanes=("left",))]
        flow = bf.solve(es, sources, sinks=[(1, 0), (1, 1)], groups=[{"name": "trunk", "tiles": [(1, 0), (1, 1)]}])
        self.assertEqual([f["kind"] for f in flow.findings], ["unbalanced_lanes"])

    def test_ports_report_capacity_and_lane_fill(self):
        es, sources = side_load_then_split()
        flow = bf.solve(
            es, sources, sinks=[(3, -1), (3, 0)], ports=[{"name": "upper", "tile": (3, -1), "item": "iron"}]
        )
        port = flow.ports[0]
        self.assertEqual(
            (port["per_minute"], port["lanes"], port["fill"]),
            (225.0, {"left": 225.0, "right": 0.0}, {"left": 0.5, "right": 0.0}),
        )


if __name__ == "__main__":
    unittest.main()
