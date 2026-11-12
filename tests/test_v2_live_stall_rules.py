"""Rules added after stalls on live maps; each test pins one of them."""

import unittest

from factorio_agent.construction_supply import FUEL_SKIP_STOCK
from factorio_agent.core.deterministic_builder import COAL_RICHNESS_TIERS, FactoryBuilder
from factorio_agent.jobs import RESEARCH_CAPABILITIES, _owns_research
from factorio_agent.mining import StarterCapacity


def furnace_cell(x, *, fuel=0, burning=False, plates=0, finished=0):
    drill = {"name": "burner-mining-drill", "position": {"x": x, "y": 2}}
    furnace = {
        "name": "stone-furnace",
        "position": {"x": x, "y": 0},
        "output_inventory": {"copper-plate": plates} if plates else {},
        "products_finished": finished,
    }
    cell = {"drill": drill, "receiver": {"name": "stone-furnace", "position": {"x": x, "y": 0}}, "fuel": fuel}
    cell["burning"] = burning
    return cell, furnace


class StockedCellProofTests(unittest.TestCase):
    def capacity(self, cells):
        controller = StarterCapacity.__new__(StarterCapacity)
        controller.cells = lambda resource: cells
        return controller

    def test_unfuelled_cell_with_enough_finished_output_counts_as_productive(self):
        running, furnace_a = furnace_cell(0, fuel=2, finished=30)
        idle, furnace_b = furnace_cell(4, plates=FUEL_SKIP_STOCK, finished=80)
        obs = {"tick": 1, "entities": [furnace_a, furnace_b]}
        proof = self.capacity([running, idle]).proof(obs, {"resource": "copper-ore", "cells": 2})
        self.assertEqual(proof["productive_cells"], 2)

    def test_unfuelled_cell_without_stock_still_does_not_count(self):
        running, furnace_a = furnace_cell(0, fuel=2, finished=30)
        empty, furnace_b = furnace_cell(4, plates=3, finished=3)
        obs = {"tick": 1, "entities": [furnace_a, furnace_b]}
        self.assertIsNone(self.capacity([running, empty]).proof(obs, {"resource": "copper-ore", "cells": 2}))


class CoalRichnessOrderTests(unittest.TestCase):
    def test_richer_sites_come_first_and_thin_ones_remain_as_last_resort(self):
        amounts = {(0, 0): 300, (5, 0): 5000, (9, 0): 1200, (20, 0): 2500}
        builder = FactoryBuilder.__new__(FactoryBuilder)
        builder.coal_sites = lambda minimum=0: [
            {"x": x, "y": y} for (x, y), amount in amounts.items() if amount >= minimum
        ]
        order = [(site["x"], site["y"]) for site in builder.coal_sites_richest_first()]
        self.assertEqual(order, [(5, 0), (20, 0), (9, 0), (0, 0)])
        self.assertEqual(COAL_RICHNESS_TIERS[-1], 0)


class ResearchOwnershipTests(unittest.TestCase):
    def test_idle_lane_and_admission_agree_on_who_owns_research(self):
        for capability in RESEARCH_CAPABILITIES:
            self.assertTrue(_owns_research(capability), capability)
        self.assertFalse(_owns_research("power"))


class PowerBridgeAvoidsOtherBlocksTests(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        board = {
            "blocks": {
                "startup_science": {
                    "reserved_area": [[32, 4], [48, 20]],
                    "entities": [{"name": "inserter", "position": {"x": 40.5, "y": 14.5}}],
                },
                "mining_bank": {
                    "reserved_area": [[60, 0], [80, 12]],
                    "entities": [{"name": "small-electric-pole", "position": {"x": 64.5, "y": 6.5}}],
                },
                "loose_hardware": {"entities": [{"name": "transport-belt", "position": {"x": 52.5, "y": 30.5}}]},
            }
        }
        (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
        self.builder = SimpleNamespace(
            _occupied_by_plan=lambda entities: {(e["position"]["x"], e["position"]["y"]) for e in entities}
        )

    def test_points_in_other_blocks_are_refused_but_the_callers_own_block_is_not(self):
        from factorio_agent.power_grid import _foreign_block_points

        goal = (64.5, 6.5)
        points = {goal, (40.5, 14.5), (44.5, 10.5), (52.5, 30.5), (56.5, 6.5), (68.5, 6.5)}
        foreign = _foreign_block_points(self.game, self.builder, goal, points)
        self.assertEqual(foreign, {(40.5, 14.5), (44.5, 10.5), (52.5, 30.5)})

    def test_missing_board_means_no_restriction(self):
        from types import SimpleNamespace

        from factorio_agent.power_grid import _foreign_block_points

        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.temp.name + "/nowhere"))
        self.assertEqual(_foreign_block_points(game, self.builder, (0.5, 0.5), {(1.5, 1.5)}), set())
        self.assertEqual(_foreign_block_points(SimpleNamespace(), self.builder, (0.5, 0.5), {(1.5, 1.5)}), set())


class PowerBridgeLeavesDeepBlockPolesTests(unittest.TestCase):
    """Live seed 1400895309: red science sites failed with no_bounded_pole_hop_connection.

    The nearest supplied pole was a smelting furnace pole more than one hop
    from the district edge; every hop out of it lay on the district's
    reserved land, so the search box around it had no way out.
    """

    def test_search_falls_back_to_the_nearest_supplied_pole_outside_other_blocks(self):
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from factorio_agent.power_grid import plan_connection

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        board = {"world_id": "w1", "blocks": {"smelting_district": {"reserved_area": [[0, -20], [40, 20]]}}}
        (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")

        def query(lua):
            payload = lua.split("helpers.json_to_table(", 1)[1].split(");local clear", 1)[0]
            points = json.loads(json.loads(payload))["points"]
            return {"ok": True, "clear": list(range(1, len(points) + 1))}

        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=query)
        builder = SimpleNamespace(
            _occupied_by_plan=lambda entities: {(e["position"]["x"], e["position"]["y"]) for e in entities}
        )

        def pole(x, y, unit):
            return {
                "name": "small-electric-pole",
                "position": {"x": x, "y": y},
                "electric_network_id": 1,
                "unit_number": unit,
            }

        engine = {"name": "steam-engine", "position": {"x": 0, "y": 90}, "electric_network_id": 1, "energy": 1}
        deep = pole(30.5, 0.5, 1)  # nearest to the goal, 9.5 tiles inside the district edge
        outside = pole(30.5, 40.5, 2)
        obs = {"world_id": "w1", "entities": [engine, deep]}
        self.assertEqual(
            plan_connection(game, builder, obs, {"x": 60.5, "y": 0.5})["reason"], "no_bounded_pole_hop_connection"
        )
        obs["entities"].append(outside)
        result = plan_connection(game, builder, obs, {"x": 60.5, "y": 0.5})
        self.assertTrue(result["ok"], result.get("reason"))
        self.assertEqual(result["source_unit_number"], 2)
        for e in result["entities"]:
            self.assertFalse(0 <= e["position"]["x"] <= 40 and -20 <= e["position"]["y"] <= 20, e)


class SharedErrorClassTests(unittest.TestCase):
    def test_supervisor_run_as_a_script_catches_the_bridge_error(self):
        """A refused submit raised by the in-process bridge must be the class the supervisor catches.

        Running ``python -m factorio_agent.supervisor`` loads supervisor.py a
        second time under another module name; a class defined inside it would
        then differ from the one the bridge raises, and every deferred dispatch
        became a controller error with up to 300 s of backoff.
        """
        import runpy

        from factorio_agent import errors
        from factorio_agent.kernel import session

        as_script = runpy.run_module("factorio_agent.supervisor", run_name="supervisor_as_script")
        self.assertIs(as_script["CLIError"], errors.CLIError)
        self.assertIs(session.CLIError, errors.CLIError)


if __name__ == "__main__":
    unittest.main()
