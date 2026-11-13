"""``produce`` generalised: belt output mode and the lane-merge adapter for three belt-fed inputs."""

import copy
import json
import unittest

from factorio_agent.blocks.belts import belt_graph, belt_tiles
from factorio_agent.blocks.geometry import step, xy
from factorio_agent.board import entity
from factorio_agent.layout_variants import Genome, candidates, genomes
from factorio_agent.machine_stock import MachineStock
from factorio_agent.production_layout import (
    LANE_MERGE,
    adapter_intrusions,
    audit_line,
    design_line,
    extend_line,
    input_demand,
    is_belt_output,
)
from factorio_agent.production_rows import BLOCK_PREFIX, validate_parameters
from factorio_agent.production_sources import free_board_ports
from factorio_agent.site.transform import Transform, apply
from test_v2_production_layout import PLATES, catalog
import test_v2_production_rows as rows_tests

LDS = "low-density-structure"
SOURCED = PLATES | {"plastic-bar"}


def lds_line(c, output="stock_chest", rate=2):
    return design_line(
        c,
        LDS,
        rate,
        inserter="inserter",
        enabled=sorted(c.recipes),
        sourced=SOURCED.__contains__,
        chest="iron-chest",
        output=output,
    )


class LaneMergeTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()

    def test_three_belt_fed_items_get_two_full_belt_ports_and_one_merged_far_belt(self):
        line = lds_line(self.catalog)
        self.assertEqual(line["spec"]["merge"], LANE_MERGE)
        self.assertEqual(audit_line(self.catalog, line), [])
        self.assertEqual(sorted(p["item"] for p in line["inputs"]), ["copper-plate", "plastic-bar", "steel-plate"])
        # Every external port is a plain full belt a route can lay (no lane key), facing the flow.
        self.assertTrue(all("lane" not in p and p["facing"] == 4 for p in line["inputs"]))
        far = {p["lane"]: p["item"] for p in line["adapter"]["replaces"]}
        self.assertEqual(far, {"left": "steel-plate", "right": "plastic-bar"})  # copper (heaviest) alone, near
        near = next(p for p in line["inputs"] if p["item"] == "copper-plate")
        self.assertEqual(near.get("belt_index"), 0)
        self.assertGreater(input_demand(self.catalog, line, "steel-plate"), 0)

    def test_every_rotation_audits_clean_and_extension_keeps_the_ports(self):
        line = lds_line(self.catalog)
        for rotation in (0, 4, 8, 12):
            with self.subTest(rotation=rotation):
                turned = apply(Transform(rotation, None, (0.5, 0.5)), line)
                self.assertEqual(audit_line(self.catalog, turned), [])
                self.assertEqual(adapter_intrusions(self.catalog, turned, turned["entities"]), [])
        grown = extend_line(self.catalog, line, 1)
        self.assertEqual((grown["inputs"], grown["output"]), (line["inputs"], line["output"]))
        self.assertEqual(audit_line(self.catalog, grown), [])

    def test_swapped_ports_or_a_back_feed_are_caught(self):
        line = lds_line(self.catalog)
        swapped = copy.deepcopy(line)
        a, b = swapped["adapter"]["inputs"]
        a["item"], b["item"] = b["item"], a["item"]
        self.assertTrue(any(f.startswith("adapter:adapter_lane_wrong") for f in audit_line(self.catalog, swapped)))
        # A belt straight behind J turns it into a straight belt: the lanes mix.
        junction = line["adapter"]["entities"][1]
        behind = step(xy(junction), (junction["direction"] + 8) % 16)
        broken = copy.deepcopy(line)
        broken["entities"] = [e for e in broken["entities"] if xy(e) != behind]
        broken["entities"].append(entity("transport-belt", *behind, junction["direction"]))
        self.assertTrue(audit_line(self.catalog, broken))

    def test_a_route_belt_feeding_an_adapter_tile_is_an_intrusion(self):
        line = lds_line(self.catalog)
        curve = line["adapter"]["entities"][0]
        below = step(xy(curve), 8)  # the near belt's side of the turn
        route = entity("transport-belt", *step(below, 12), 4)
        feeder = entity("transport-belt", *below, 0)  # faces north into the curve tile
        found = adapter_intrusions(self.catalog, line, line["entities"] + [route, feeder])
        self.assertTrue(found and found[0].startswith("adapter_fed_by_route"))
        # A route ending straight behind an external port is the normal connection.
        port = line["adapter"]["inputs"][0]
        ok = entity("transport-belt", *step(xy(port), (port["facing"] + 8) % 16), port["facing"])
        self.assertEqual(adapter_intrusions(self.catalog, line, line["entities"] + [ok]), [])

    def test_the_merged_lanes_reach_the_inserters_of_the_far_belt(self):
        line = lds_line(self.catalog)
        graph = belt_graph(belt_tiles(self.catalog, line["entities"]))
        start = xy(line["adapter"]["inputs"][0])
        seen = [start]
        while seen[-1] in graph and len(seen) < 64:
            seen.append(graph[seen[-1]][0])
        self.assertIn(xy(line["adapter"]["replaces"][0]), seen)

    def test_genomes_carry_the_merge_and_rebuild_it(self):
        found = genomes(self.catalog, product=LDS, enabled=sorted(self.catalog.recipes), sourced=SOURCED.__contains__)
        rows = [g for g in found if g.form == "row"]
        self.assertTrue(rows and all(g.merge == LANE_MERGE for g in rows))
        again = Genome.from_dict(rows[0].to_dict())
        self.assertEqual(again, rows[0])
        self.assertIn(LANE_MERGE, again.key())
        request = dict(
            product=LDS,
            rate=2,
            inserter="inserter",
            enabled=sorted(self.catalog.recipes),
            sourced=SOURCED.__contains__,
            chest="iron-chest",
        )
        result = candidates(self.catalog, request, library=False)
        self.assertTrue(result["candidates"])
        with self.assertRaisesRegex(ValueError, "genome merge"):
            design_line(
                self.catalog, LDS, 2, genome=Genome("row", "assembling-machine-1").to_dict(), **_args(self.catalog)
            )


def _args(c):
    return dict(inserter="inserter", enabled=sorted(c.recipes), sourced=SOURCED.__contains__, chest="iron-chest")


class BeltOutputTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()

    def test_belt_output_ends_in_a_free_belt_port_without_a_chest(self):
        line = design_line(self.catalog, "pipe", 30, output="belt", **_args(self.catalog))
        self.assertTrue(is_belt_output(line))
        self.assertEqual(audit_line(self.catalog, line), [])
        self.assertNotIn("name", line["output"])
        self.assertEqual(line["output"]["facing"], 4)
        self.assertFalse(any(e["name"] == "iron-chest" for e in line["entities"]))
        board = {"blocks": {"production_row_pipe": {"ports": line["inputs"] + [line["output"]]}}, "connections": []}
        self.assertEqual([p["block"] for p in free_board_ports(board, "pipe")], ["production_row_pipe"])
        for rotation in (4, 8, 12):
            self.assertEqual(audit_line(self.catalog, apply(Transform(rotation, None, (0.5, 0.5)), line)), [])
        grown = extend_line(self.catalog, line, 1)
        self.assertEqual(grown["output"], line["output"])
        self.assertGreater(grown["stock_rate_per_minute"], line["stock_rate_per_minute"])

    def test_a_stock_chest_port_is_never_a_belt_source(self):
        line = design_line(self.catalog, "pipe", 30, **_args(self.catalog))
        board = {"blocks": {"production_row_pipe": {"ports": [line["output"]]}}, "connections": []}
        self.assertEqual(free_board_ports(board, "pipe"), [])

    def test_cluster_forms_refuse_a_belt_output(self):
        with self.assertRaisesRegex(ValueError, "cluster"):
            design_line(self.catalog, "inserter", 10, output="belt", **_args(self.catalog))

    def test_validate_takes_the_output_mode(self):
        validate_parameters({"item": "pipe", "rate_per_minute": 3, "output": "belt"})
        validate_parameters({"item": "pipe", "rate_per_minute": 3, "output": "stock_chest"})
        for bad in (
            {"item": "pipe", "rate_per_minute": 3, "output": "pipe"},
            {"item": "pipe", "rate_per_minute": 3, "x": 1},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_parameters(bad)


class BeltOutputRowTests(unittest.TestCase):
    """The controller in belt mode (fixtures from ``test_v2_production_rows``)."""

    setUp = rows_tests.ProductionRowsTests.setUp
    tearDown = rows_tests.ProductionRowsTests.tearDown
    board = rows_tests.ProductionRowsTests.board
    obs = rows_tests.ProductionRowsTests.obs
    live = rows_tests.ProductionRowsTests.live

    def test_belt_row_publishes_its_output_port_proves_machines_and_holds_no_stock(self):
        parameters = {"item": "pipe", "rate_per_minute": 30, "output": "belt"}
        state = self.rows.prepare(self.obs(), parameters)
        self.assertTrue(state["ok"], state)
        self.assertTrue(self.site.allocated[0][4])  # returns_to_bus: the product leaves on the bus edge
        self.rows.save("pipe__belt", dict(state, key="pipe__belt"))
        waiting = self.rows.step(self.obs(), parameters)
        self.assertEqual(waiting["reason"], "waiting for machine-made pipe on the output belt")
        self.game.query.assert_not_called()  # no chest bar
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        ports = board["blocks"][BLOCK_PREFIX + "pipe__belt"]["ports"]
        self.assertIn(state["line"]["output"], ports)
        self.assertEqual([p["block"] for p in free_board_ports(board, "pipe")], [BLOCK_PREFIX + "pipe__belt"])
        done = self.rows.step(self.obs(self.live(state["plan"], "pipe")), parameters)
        self.assertEqual(done["status"], "succeeded", done)
        self.assertIn("output_port", done["evidence"])
        self.assertIsNone(MachineStock(self.rows)(self.obs(), "pipe"))
        # A stock row of the same item is a separate instance (production_rows.row_key).
        self.assertFalse(self.rows.path("pipe").exists())
        self.assertTrue(self.rows.path("pipe__belt").exists())
        self.assertIn(BLOCK_PREFIX + "pipe__belt", board["blocks"])


if __name__ == "__main__":
    unittest.main()
