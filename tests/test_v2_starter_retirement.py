"""Starter cells stop being served once a permanent source exists, then are mined (starter_retirement)."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from factorio_agent import progression as pg
from factorio_agent import starter_retirement as sr
from factorio_agent.construction_supply import V2Bootstrap
from factorio_agent.jobs import CAPABILITIES

WORLD = "world-1"


def job(capability, status="succeeded", **parameters):
    return {"capability": capability, "parameters": parameters, "status": status, "world_id": WORLD}


EARLY = [job("starter"), job("power"), job("mining_outpost", resource="iron-ore")]
PLATES_DONE = EARLY + [job("smelting_district"), job("planned_main_bus")]
STONE_DONE = [job("dedicated_materials", resource="stone")]
COAL_DONE = [job("electric_coal"), job("mining_outpost", resource="coal")]


def retired_jobs(*resources):
    return [job(sr.CAPABILITY, resource=r) for r in resources]


def bus(stations=()):
    """The live planned bus of seed 2099054105: iron trunk on lane 0, copper on lane 2, 64 tiles, flowing south."""
    frame = {
        "origin": [-104, 0],
        "direction": "S",
        "width": 28,
        "length": 320,
        "lanes": [
            {"group": 0, "index": 0, "material": "iron-plate", "offset": -14},
            {"group": 0, "index": 2, "material": "copper-plate", "offset": -12},
        ],
    }
    entities = [
        {"name": "transport-belt", "position": {"x": x, "y": y + 0.5}, "direction": 8}
        for x in (-90.5, -92.5)
        for y in range(64)
    ]
    return {
        "template": "planned_main_bus",
        "world_id": WORLD,
        "frame": frame,
        "entities": entities,
        "trunks": [
            {"item": "iron-plate", "lane": 0, "length": 64},
            {"item": "copper-plate", "lane": 2, "length": 64},
        ],
        "stations": list(stations),
        "ports": [],
    }


def board(with_bus=True, blocks=None):
    data = {"world_id": WORLD, "catalog_fingerprint": "fp", "blocks": blocks or {}, "connections": []}
    if with_bus:
        data["shared_main_bus"] = bus([{"lane": 0, "along": 18, "item": "iron-plate"}])
    return data


STONE_BANK = {
    "stone_mining": {
        "entities": [{"name": "electric-mining-drill", "position": {"x": -11.5, "y": 64.5}}],
        "ports": [{"direction": "output", "item": "stone", "position": {"x": -3.5, "y": 66.5}}],
    }
}
COAL_BANK = {
    "coal_mining": {
        "entities": [{"name": "electric-mining-drill", "position": {"x": -40.5, "y": -70.5}}],
        "ports": [{"direction": "output", "item": "coal", "position": {"x": -41.5, "y": -74.5}}],
    }
}


class PermanentSourceRuleTests(unittest.TestCase):
    def test_early_game_retires_nothing(self):
        self.assertEqual(sr.retired_by_jobs(EARLY), set())
        self.assertEqual(sr.retired_resources(EARLY, board(), WORLD), set())

    def test_district_alone_is_not_a_permanent_plate_source(self):
        self.assertEqual(sr.retired_by_jobs(EARLY + [job("smelting_district")]), set())

    def test_district_and_bus_retire_both_plate_resources(self):
        self.assertEqual(sr.retired_by_jobs(PLATES_DONE), {"iron-ore", "copper-ore"})

    def test_board_must_show_the_plate_on_the_bus(self):
        self.assertEqual(sr.retired_resources(PLATES_DONE, board(with_bus=False), WORLD), set())
        self.assertEqual(sr.retired_resources(PLATES_DONE, board(), "other-world"), set())
        self.assertEqual(sr.retired_resources(PLATES_DONE, board(), WORLD), {"iron-ore", "copper-ore"})

    def test_stone_needs_a_verified_stone_bank(self):
        jobs = PLATES_DONE + STONE_DONE
        self.assertNotIn("stone", sr.retired_resources(jobs, board(), WORLD))
        self.assertIn("stone", sr.retired_resources(jobs, board(blocks=STONE_BANK), WORLD))
        failed = PLATES_DONE + [job("dedicated_materials", status="blocked", resource="stone")]
        self.assertNotIn("stone", sr.retired_by_jobs(failed))

    def test_coal_cells_go_last_because_they_fuel_every_other_cell(self):
        jobs = PLATES_DONE + STONE_DONE + COAL_DONE
        self.assertNotIn("coal", sr.retired_by_jobs(jobs))
        self.assertNotIn("coal", sr.retired_by_jobs(jobs + retired_jobs("iron-ore", "copper-ore")))
        done = jobs + retired_jobs("iron-ore", "copper-ore", "stone")
        self.assertIn("coal", sr.retired_by_jobs(done))
        self.assertNotIn(
            "coal", sr.retired_by_jobs(PLATES_DONE + STONE_DONE + retired_jobs("iron-ore", "copper-ore", "stone"))
        )
        self.assertIn("coal", sr.retired_resources(done, board(blocks=COAL_BANK), WORLD))


class PlannerTests(unittest.TestCase):
    def nodes(self, jobs, queue=()):
        return sr.starter_retirement_nodes(jobs, list(queue), {}, CAPABILITIES)

    def test_one_low_priority_node_per_retired_resource_and_never_twice(self):
        nodes = self.nodes(PLATES_DONE)
        self.assertEqual([n["parameters"] for n in nodes], [{"resource": "iron-ore"}])
        self.assertEqual(nodes[0]["priority"], sr.RETIRE_PRIORITY)
        running = [job(sr.CAPABILITY, status="running", resource="iron-ore")]
        self.assertEqual([n["parameters"] for n in self.nodes(PLATES_DONE + running)], [{"resource": "copper-ore"}])
        blocked = [job(sr.CAPABILITY, status="blocked", resource=r) for r in ("iron-ore", "copper-ore")]
        self.assertEqual(self.nodes(PLATES_DONE + blocked), [])  # auto-retry owns it

    def test_nothing_before_a_permanent_source(self):
        self.assertEqual(self.nodes(EARLY), [])
        self.assertEqual(self.nodes([]), [])

    def test_capacity_planner_stops_for_a_retired_resource(self):
        decision = SimpleNamespace(resource="copper-ore", target_cells=5, current_cells=3, reason="r")
        original = pg.plan_capacity
        pg.plan_capacity = lambda *a, **k: [decision]
        try:
            self.assertEqual(len(pg.capacity_nodes(EARLY, [], {}, CAPABILITIES)), 1)
            self.assertEqual(pg.capacity_nodes(PLATES_DONE, [], {}, CAPABILITIES), [])
        finally:
            pg.plan_capacity = original

    def test_no_starter_cell_while_its_outpost_is_being_built(self):
        # eval-4177156614: 5 coal cells at 23.6 min while the coal outpost (submitted 21.1) was building.
        decision = SimpleNamespace(resource="coal", target_cells=5, current_cells=4, reason="r")
        original = pg.plan_capacity
        pg.plan_capacity = lambda *a, **k: [decision]
        building = EARLY + [job("mining_outpost", status="running", resource="coal")]
        unlocked = {"technologies": {"electric-mining-drill": True}}
        try:
            self.assertEqual(pg.capacity_nodes(building, [], unlocked, CAPABILITIES), [])
            # Before the drill research the outpost cannot produce: the starter cell still pays.
            self.assertEqual(len(pg.capacity_nodes(building, [], {}, CAPABILITIES)), 1)
            paused = EARLY + [job("mining_outpost", status="paused", resource="coal")]
            self.assertEqual(len(pg.capacity_nodes(paused, [], unlocked, CAPABILITIES)), 1)
        finally:
            pg.plan_capacity = original


def entity(name, x, y, unit, **extra):
    return {"name": name, "position": {"x": x, "y": y}, "unit_number": unit, **extra}


class RetirementJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = list(PLATES_DONE)
        self.board = board()
        self.write()
        self.drill = entity("burner-mining-drill", -3, -48, 11, inventory={}, status_name="no_fuel")
        self.furnace = entity("stone-furnace", -3, -50, 12, inventory={}, output_inventory={}, status_name="no_fuel")
        self.power_drill = entity("burner-mining-drill", -41, -15, 13, inventory={"coal": 3}, status_name="working")
        self.obs = {
            "world_id": WORLD,
            "tick": 120000,
            "inventory": {},
            "entities": [self.drill, self.furnace, self.power_drill],
        }
        cells = [
            {
                "drill": {"name": "burner-mining-drill", "position": {"x": -3, "y": -48}},
                "receiver": {"name": "stone-furnace", "position": {"x": -3, "y": -50}},
                "electric": False,
                "direct_belt": False,
            },
            # the power plant's dedicated coal drill drops onto its belt: never a starter cell
            {
                "drill": {"name": "burner-mining-drill", "position": {"x": -41, "y": -15}},
                "receiver": None,
                "electric": False,
                "direct_belt": True,
            },
        ]
        self.cells = {"iron-ore": cells}
        self.bootstrap = SimpleNamespace(
            _existing_cells=Mock(side_effect=lambda r, **_: {"ok": True, "cells": self.cells.get(r, [])})
        )
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(return_value={"ok": True, "linked": {}})
        )
        self.builder = SimpleNamespace(owns_automated_burner=lambda e: e["position"] == {"x": -41, "y": -15})
        self.retirement = sr.StarterRetirement(self.game, self.bootstrap, self.builder)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self):
        (self.root / "jobs.json").write_text(
            json.dumps({"schema_version": 2, "world_id": WORLD, "jobs": {str(i): j for i, j in enumerate(self.jobs)}}),
            encoding="utf-8",
        )
        (self.root / "pcb-board.json").write_text(json.dumps(self.board), encoding="utf-8")

    def step(self):
        return self.retirement.step(self.obs, {"resource": "iron-ore"})

    def test_cell_keys_hide_only_unlinked_cells_of_retired_resources(self):
        keys = self.retirement.cell_keys(self.obs)
        self.assertIn(("burner-mining-drill", -3, -48), keys)
        self.assertIn(("stone-furnace", -3, -50), keys)
        self.assertNotIn(("burner-mining-drill", -41, -15), keys)  # power coal drill untouched

    def test_early_game_has_no_retired_cells_and_no_survey(self):
        self.jobs = list(EARLY)
        self.write()
        self.assertEqual(self.retirement.cell_keys(self.obs), set())
        self.bootstrap._existing_cells.assert_not_called()

    def test_one_final_collection_then_mining_drill_first(self):
        self.furnace["output_inventory"] = {"iron-plate": 37}
        action = self.step()
        self.assertEqual(action["type"], "take_many")
        self.assertEqual((action["item"], action["count"]), ("iron-plate", 37))
        self.assertEqual([s["unit_number"] for s in action["sources"]], [12])
        self.furnace["output_inventory"] = {}
        action = self.step()
        self.assertEqual(
            (action["type"], action["name"], action["expected_entity_unit"]), ("mine", "burner-mining-drill", 11)
        )
        self.obs["entities"].remove(self.drill)
        action = self.step()
        self.assertEqual((action["type"], action["name"]), ("mine", "stone-furnace"))

    def test_small_stock_waits_while_the_drill_still_burns(self):
        self.drill.update(inventory={"coal": 4}, status_name="working")
        self.furnace.update(output_inventory={"iron-plate": 12}, status_name="working")
        result = self.step()
        self.assertEqual(result["reason"], "starter_retirement_waits_for_cells_to_cool")
        self.furnace["output_inventory"] = {"iron-plate": sr.COLLECT_BATCH}
        self.assertEqual(self.step()["type"], "take_many")

    def test_working_or_stocked_furnace_is_not_mined(self):
        self.obs["entities"].remove(self.drill)
        self.furnace.update(inventory={"coal": 2, "iron-ore": 5}, status_name="working")
        self.assertEqual(self.step()["status"], "waiting")

    def test_linked_cell_is_kept_and_still_fuelled(self):
        self.game.query.return_value = {"ok": True, "linked": [2]}  # an inserter takes from the furnace
        self.assertEqual(self.retirement.cell_keys(self.obs), set())
        result = self.step()
        self.assertEqual(result["status"], "succeeded")  # nothing unlinked left to retire
        self.assertIn(("stone-furnace", -3, -50), {sr._key(e) for e in self.obs["entities"]})

    def test_link_found_before_mining_keeps_the_cell(self):
        self.retirement.cell_keys(self.obs)
        self.game.query.return_value = {"ok": True, "linked": [1]}
        self.assertEqual(self.step()["reason"], "starter_retirement_kept_a_linked_cell")
        self.assertEqual(self.retirement.cell_keys(self.obs), set())

    def test_blocked_without_permanent_source_and_proof_after_demolition(self):
        self.jobs = list(EARLY)
        self.write()
        self.assertEqual(self.step()["reason"], "starter_retirement_without_permanent_source")
        self.jobs = list(PLATES_DONE)
        self.write()
        self.step()
        self.assertIsNone(self.retirement.proof(self.obs, {"resource": "iron-ore"}))
        self.obs["entities"] = [self.power_drill]
        result = self.step()
        self.assertEqual(result["status"], "succeeded")
        proof = self.retirement.proof(self.obs, {"resource": "iron-ore"})
        self.assertEqual(proof["retired_cells"], 1)

    def test_coal_chest_stock_is_taken_before_the_chest_is_mined(self):
        self.jobs = PLATES_DONE + STONE_DONE + COAL_DONE + retired_jobs("iron-ore", "copper-ore", "stone")
        self.board = board(blocks=COAL_BANK)
        self.write()
        chest = entity("wooden-chest", -29.5, -51.5, 21, inventory={"coal": 120})
        coal_drill = entity("burner-mining-drill", -30, -50, 22, inventory={}, status_name="no_fuel")
        self.obs["entities"] = [chest, coal_drill]
        self.cells["coal"] = [
            {
                "drill": {"name": "burner-mining-drill", "position": {"x": -30, "y": -50}},
                "receiver": {"name": "wooden-chest", "position": {"x": -29.5, "y": -51.5}},
            }
        ]
        action = self.retirement.step(self.obs, {"resource": "coal"})
        self.assertEqual((action["type"], action["item"], action["count"]), ("take", "coal", 50))
        chest["inventory"] = {}
        action = self.retirement.step(self.obs, {"resource": "coal"})
        self.assertEqual((action["type"], action["name"]), ("mine", "burner-mining-drill"))


class FuelAndCellUpkeepTests(unittest.TestCase):
    def setUp(self):
        self.bootstrap = V2Bootstrap.__new__(V2Bootstrap)
        self.iron_drill = {"name": "burner-mining-drill", "position": {"x": 9, "y": 1}, "inventory": {}}
        self.stone_drill = {"name": "burner-mining-drill", "position": {"x": 20, "y": 1}, "inventory": {}}
        self.obs = {
            "world_id": WORLD,
            "inventory": {"coal": 30},
            "resources": {},
            "entities": [self.iron_drill, self.stone_drill],
        }
        self.bootstrap._existing_cells = Mock(return_value={"ok": True, "cells": []})
        self.bootstrap.retirement = SimpleNamespace(
            cell_keys=lambda obs: {("burner-mining-drill", 9, 1)}, resources=lambda obs: {"iron-ore"}
        )

    def test_no_fuel_for_a_retired_cell_others_still_fuelled(self):
        action = self.bootstrap.maintain_fuel(self.obs)
        self.assertEqual((action["type"], action["position"]), ("insert", self.stone_drill["position"]))
        self.obs["entities"] = [self.iron_drill]
        self.assertIsNone(self.bootstrap.maintain_fuel(self.obs))

    def test_early_game_fuel_unchanged_without_retirement(self):
        self.bootstrap.retirement = None
        self.obs["entities"] = [self.iron_drill]
        self.assertEqual(self.bootstrap.maintain_fuel(self.obs)["position"], self.iron_drill["position"])

    def test_retired_resource_gets_no_new_cell(self):
        self.bootstrap.discover_cell = Mock()
        self.assertIsNone(self.bootstrap._ensure_cell(self.obs, "iron-ore", "stone-furnace"))
        self.bootstrap.discover_cell.assert_not_called()


class CollectionPointTests(unittest.TestCase):
    def test_surplus_starts_past_the_last_tap_window_of_each_lane(self):
        points = sr.collection_points(
            bus([{"lane": 0, "along": 18}, {"lane": 0, "along": 26}, {"lane": 2, "along": 38}])
        )
        iron = sorted(y for (_, x, y), item in points.items() if item == "iron-plate")
        copper = sorted(y for (_, x, y), item in points.items() if item == "copper-plate")
        # live map: iron rows 30..63 (34 tiles), copper rows 42..63 (22 tiles) after the 4-row window + 1 free row;
        # the last TAIL_KEEP rows stay for the tail proof
        self.assertEqual((len(iron), iron[0], iron[-1]), (34 - 1 - sr.TAIL_KEEP, 31.5, 61.5))
        self.assertEqual((len(copper), copper[0]), (22 - 1 - sr.TAIL_KEEP, 43.5))
        self.assertTrue(all(x == -90.5 for (_, x, _), i in points.items() if i == "iron-plate"))
        self.assertEqual(sr.collection_points(None), {})
        self.assertEqual(sr.collection_points({"template": "old_bus"}), {})

    def construction(self, tmp, belts, furnace_stock, phase="verified", blocks=None):
        root = Path(tmp)
        (root / "pcb-board.json").write_text(json.dumps(board(blocks=blocks)), encoding="utf-8")
        if phase is not None:
            (root / "planned-main-bus.json").write_text(
                json.dumps({"world_id": WORLD, "phase": phase}), encoding="utf-8"
            )
        boot = V2Bootstrap.__new__(V2Bootstrap)
        boot.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
        boot.catalog = SimpleNamespace(fingerprint="fp", items={})
        boot.construction_buffers = {}
        entities = [
            {
                "name": "transport-belt",
                "position": {"x": -90.5, "y": y},
                "unit_number": 100 + i,
                "belt_inventory": {"iron-plate": 8},
            }
            for i, y in enumerate(belts)
        ]
        entities.append(
            {
                "name": "stone-furnace",
                "position": {"x": -3, "y": -50},
                "unit_number": 7,
                "output_inventory": {"iron-plate": furnace_stock},
            }
        )
        production = {"iron-plate": {"produced": 6000}}  # 600 plates/min: a haul batch is the 50 cap
        return boot, {"world_id": WORLD, "tick": 36000, "production": production, "entities": entities, "inventory": {}}

    def test_take_prefers_the_bus_surplus_in_a_full_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot, obs = self.construction(tmp, [58.5, 59.5, 60.5, 61.5], furnace_stock=90)
            action = boot._take_output(obs, "iron-plate", 10)
            self.assertEqual(action["type"], "take_many")
            self.assertEqual([s["kind"] for s in action["sources"]][:4], ["belt"] * 4)
            self.assertEqual(action["sources"][-1]["unit_number"], 7)
            self.assertGreater(action["count"], 10)  # one trip of a haul batch, not only the shortfall

    def test_trunk_rows_feeding_a_tap_are_never_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot, obs = self.construction(tmp, [10.5, 19.5], furnace_stock=0)
            self.assertIsNone(boot._take_output(obs, "iron-plate", 10))

    def test_tail_tiles_are_never_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot, obs = self.construction(tmp, [62.5, 63.5], furnace_stock=0)
            self.assertIsNone(boot._take_output(obs, "iron-plate", 10))

    def test_no_trunk_collection_before_the_bus_proved_its_tails(self):
        # eval-4177156614: the actor emptied the copper trunk at rows 112..130 for 10 minutes
        # while the bus waited for plates on its tail.
        for phase in ("build", "splice", "commission", None):
            with tempfile.TemporaryDirectory() as tmp:
                boot, obs = self.construction(tmp, [60.5, 61.5], furnace_stock=30, phase=phase)
                action = boot._take_output(obs, "iron-plate", 10)
                self.assertEqual([s["kind"] for s in action["sources"]], ["output"], phase)

    def test_spliced_district_port_feeds_the_bus_not_a_construction_trip(self):
        port = {"x": -60.5, "y": -5.5}
        district = {
            "smelting_district": {
                "entities": [{"name": "transport-belt", "position": port, "direction": 4}],
                "ports": [{"direction": "output", "item": "iron-plate", "position": port}],
            }
        }
        for phase, kinds in (("build", ["belt", "output"]), ("commission", ["output"]), ("verified", ["output"])):
            with tempfile.TemporaryDirectory() as tmp:
                boot, obs = self.construction(tmp, [], furnace_stock=30, phase=phase, blocks=district)
                obs["entities"].append(
                    {"name": "transport-belt", "position": port, "unit_number": 55, "belt_inventory": {"iron-plate": 8}}
                )
                action = boot._take_output(obs, "iron-plate", 10)
                self.assertEqual(sorted(s["kind"] for s in action["sources"]), sorted(kinds), phase)


if __name__ == "__main__":
    unittest.main()
