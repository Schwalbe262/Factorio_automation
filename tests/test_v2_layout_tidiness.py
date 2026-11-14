"""layout_tidiness: belts against their reservations; the dashboard's built-belt layer."""

import unittest

from factorio_agent.layout_tidiness import belt_cells, board_tidiness, connection_metrics
from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.masterplan import plan_site
from factorio_agent.site.planrender import render_dashboard_svg


def belt(x, y, direction=0, name="transport-belt", **extra):
    return dict(name=name, position=dict(x=x, y=y), direction=direction, **extra)


def board():
    """Block ``a`` reserves [0,0]-[4,4]: 5 belt-likes inside, 2 outside, two undergrounds, one splitter.

    The belt at (1.5, 2.5) faces north and is fed only from the west by (0.5, 2.5) facing east: a curve."""
    a = [
        belt(0.5, 2.5, 4),  # east, feeds (1.5, 2.5) from its side
        belt(1.5, 2.5, 0),  # north: a curve
        belt(1.5, 1.5, 0),  # north, fed from behind: straight
        belt(6.5, 1.5, 4),  # outside
        belt(7.5, 1.5, 4, name="underground-belt", belt_to_ground_type="input"),  # outside
        belt(3.5, 3.5, 0, name="underground-belt", belt_to_ground_type="output"),
        belt(2, 0.5, 0, name="splitter"),
        dict(name="assembling-machine-1", position=dict(x=2.5, y=2.5)),  # not a belt
    ]
    return {
        "world_id": "w",
        "blocks": {
            "a": {"reserved_area": [[0, 0], [4, 4]], "entities": a, "growth_entities": [belt(9.5, 9.5)]},
            "lane": {"entities": [belt(20.5, 0.5), belt(20.5, 1.5)]},  # no reservation
            "broken": {"reserved_area": "nonsense", "entities": "nonsense"},
            "empty": {"reserved_area": [[0, 0], [1, 1]], "entities": []},
        },
        "shared_main_bus": {
            "entities": [belt(30.5, 0.5), belt(40.5, 0.5)],
            "reservations": [{"area": [[30, 0], [31, 10]], "item": "iron-plate"}],
        },
        "connections": [
            {"source": {"x": 0, "y": 0}, "destination": {"x": 10, "y": 5}, "item": "stone", "owner": "a"},
            {"source": {"x": 0, "y": 0}, "destination": {"x": 1, "y": 1}, "item": "coal", "owner": "b"},
            {"source": "bad"},
        ],
    }


class TidinessTests(unittest.TestCase):
    def test_blocks_and_totals(self):
        metrics = board_tidiness(board())
        rows = {b["block"]: b for b in metrics["blocks"]}
        self.assertEqual(set(rows), {"a", "lane", "shared_main_bus"})  # blocks without belts are left out
        a = rows["a"]
        self.assertEqual((a["belts"], a["outside"], a["undergrounds"], a["splitters"]), (7, 2, 2, 1))
        self.assertEqual(a["outside_share"], round(2 / 7, 3))
        self.assertEqual(a["turns"], 1)
        self.assertIsNone(rows["lane"]["outside"])
        self.assertEqual(rows["shared_main_bus"]["outside"], 1)
        totals = metrics["totals"]
        self.assertEqual((totals["belts"], totals["outside"], totals["unreserved_belts"]), (11, 3, 2))
        self.assertEqual(totals["outside_share"], round(3 / 9, 3))
        self.assertEqual(totals["undergrounds"], 2)
        # without a plan, outside_plan = outside every reservation of the board (the lane's 2 belts too)
        self.assertEqual(totals["outside_plan"], 5)
        self.assertEqual(metrics["blocks"][0]["block"], "a")  # most belts outside first

    def test_site_plan_areas_account_for_belts(self):
        plan = {
            "world_id": "w",
            "plan": {
                "zones": [{"kind": "bus", "rect": [40, 0, 42, 10]}],
                "corridors": [{"segments": [{"rect": [5, 0, 9, 3]}]}],
                "allocations": [{"rect": [100, 100, 101, 101], "tap_rect": [19, 0, 22, 3]}],
            },
        }
        totals = board_tidiness(board(), plan)["totals"]
        self.assertEqual(totals["outside_plan"], 0)
        self.assertEqual(totals["outside"], 3)  # the per-block measure ignores the plan

    def test_connections_and_bad_input(self):
        self.assertEqual(
            connection_metrics(board()["connections"]),
            dict(count=2, longest=15.0, longest_item="stone", longest_owner="a"),
        )
        empty = board_tidiness(None)
        self.assertEqual(empty["blocks"], [])
        self.assertIsNone(empty["totals"]["outside_share"])
        self.assertEqual(empty["connections"]["count"], 0)

    def test_belt_cells_cover_splitter_tiles(self):
        cells = belt_cells(board())
        splitter = next(c for c in cells if c["kind"] == "splitter")
        self.assertEqual(splitter["tiles"], [(1, 0), (2, 0)])
        self.assertEqual({c["inside"] for c in cells if c["block"] == "lane"}, {None})


class BeltLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = make_world(7, size=384)
        cls.plan = plan_site(cls.world)

    def test_layer_has_one_path_per_kind_and_state(self):
        data = board()
        svg = render_dashboard_svg(self.world, self.plan, data["blocks"], shared_bus=data["shared_main_bus"])
        self.assertIn('data-kind="belts" data-state="inside" data-count="5"', svg)  # 3 belts + splitter of a, 1 bus
        self.assertIn('data-kind="belts" data-state="outside" data-count="2"', svg)
        self.assertIn('data-kind="belts" data-state="unreserved" data-count="2"', svg)
        self.assertIn('data-kind="undergrounds" data-state="outside" data-count="1"', svg)
        self.assertIn('data-kind="undergrounds" data-state="inside" data-count="1"', svg)
        self.assertEqual(svg.count("<path data-kind="), 5)
        self.assertIn('data-kind="board" data-name="a"', svg)

    def test_many_belts_stay_few_elements(self):
        entities = [belt(x + 0.5, y + 0.5) for x in range(60) for y in range(50)]
        blocks = {"big": {"reserved_area": [[0, 0], [30, 50]], "entities": entities}}
        svg = render_dashboard_svg(self.world, self.plan, blocks)
        self.assertEqual(svg.count("<path data-kind="), 2)
        self.assertIn('data-state="inside" data-count="1500"', svg)
        self.assertIn('data-state="outside" data-count="1500"', svg)
        overlay = svg.split('<g id="dashboard-overlay">')[1]
        self.assertLess(len(overlay), 80_000)  # ~20 bytes per belt cell

    def test_no_board_draws_no_belts(self):
        svg = render_dashboard_svg(self.world, self.plan, None)
        self.assertNotIn('data-kind="belts"', svg)


if __name__ == "__main__":
    unittest.main()
