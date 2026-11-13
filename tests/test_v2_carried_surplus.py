"""Carried fuel/ore beyond known demand goes into consumers, then depots (carried_surplus)."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from factorio_agent import carried_surplus as cs
from factorio_agent import starter_retirement as sr
from factorio_agent.construction_supply import FUEL_FETCH, V2Bootstrap
from factorio_agent.power_coal_feed import boiler_sizing
from factorio_agent.wood_burner_feed import WOOD_BATCH
from test_v2_starter_retirement import COAL_BANK, COAL_DONE, PLATES_DONE, STONE_DONE, board, entity, retired_jobs

WORLD = "world-1"
CATALOG = SimpleNamespace(items={"coal": {"stack_size": 50}, "iron-ore": {"stack_size": 50}}, entities={})
ALLOW = cs.allowance("coal", CATALOG)


def boiler(coal, unit=1, x=10):
    return entity("boiler", x, 0, unit, inventory={"coal": coal}, status_name="working")


def drill(coal, unit=2, x=20, status="working"):
    return entity("burner-mining-drill", x, 0, unit, inventory={"coal": coal}, status_name=status)


def furnace(unit=3, x=30, **inventory):
    return entity("stone-furnace", x, 0, unit, inventory=inventory, output_inventory={}, status_name="working")


def obs(carried, *entities, **extra):
    return {
        "world_id": WORLD,
        "position": {"x": 0, "y": 0},
        "inventory": {"coal": carried},
        "entities": list(entities),
        **extra,
    }


class AllowanceTests(unittest.TestCase):
    def test_allowance_is_one_boiler_fill_plus_one_burner_fetch(self):
        self.assertEqual(ALLOW, boiler_sizing(CATALOG)["target"] + FUEL_FETCH)
        small = SimpleNamespace(items={"coal": {"stack_size": 50}}, entities={"boiler": {"energy_usage": 5000}})
        self.assertLess(cs.allowance("coal", small), ALLOW)  # follows the demand, not a fixed number
        self.assertEqual(cs.threshold("coal", CATALOG), 50)
        self.assertIsNone(cs.allowance("iron-plate", CATALOG))  # plates are construction stock

    def test_no_action_within_allowance_plus_threshold(self):
        surplus = cs.CarriedSurplus(CATALOG)
        self.assertIsNone(surplus.step(obs(ALLOW + 49, boiler(0))))
        action = surplus.step(obs(ALLOW + 50, boiler(0)))
        self.assertEqual((action["type"], action["count"], action["inventory"]), ("insert", 50, "fuel"))


class DisposalOrderTests(unittest.TestCase):
    def setUp(self):
        self.storage = SimpleNamespace(
            state={"world_id": WORLD, "chests": [{"name": "wooden-chest", "unit_number": 9}]}
        )
        self.retirement = SimpleNamespace(cell_keys=lambda o: {("burner-mining-drill", 40, 0)})
        self.surplus = cs.CarriedSurplus(CATALOG, storage=self.storage, retirement=self.retirement)

    def test_boiler_then_drill_then_smelting_furnace_then_depot(self):
        chest = entity("wooden-chest", 5, 5, 9, inventory={"iron-plate": 100})
        retired = drill(0, unit=4, x=40)
        rows = [furnace(**{"iron-ore": 5}), drill(30), boiler(45), retired, chest]
        state = obs(400, *rows)
        first = self.surplus.step(state)
        self.assertEqual((first["name"], first["count"]), ("boiler", 5))
        rows[2]["inventory"]["coal"] = 50
        self.assertEqual(self.surplus.step(state)["name"], "burner-mining-drill")
        self.assertEqual(self.surplus.step(state)["position"]["x"], 20)  # never the retiring cell's drill
        rows[1]["inventory"]["coal"] = 50
        self.assertEqual(self.surplus.step(state)["name"], "stone-furnace")
        rows[0]["inventory"]["coal"] = 50
        deposit = self.surplus.step(state)
        self.assertEqual((deposit["type"], deposit["expected_entity_unit"]), ("deposit", 9))
        self.assertEqual(deposit["count"], 400 - ALLOW)  # the whole surplus, the chest has 14 free slots

    def test_idle_or_stuck_consumers_take_nothing(self):
        idle = entity("stone-furnace", 30, 0, 3, inventory={}, output_inventory={}, status_name="no_ingredients")
        stuck = drill(0, status="waiting_for_space_in_destination")
        self.storage.state["chests"] = []
        self.assertIsNone(self.surplus.step(obs(400, idle, stuck)))

    def test_ore_goes_into_a_fuelled_furnace_smelting_the_same_ore(self):
        copper = furnace(unit=5, x=8, coal=5, **{"copper-ore": 2})
        iron = furnace(unit=6, x=12, coal=5, **{"iron-ore": 10})
        state = dict(obs(0, copper, iron), inventory={"iron-ore": cs.allowance("iron-ore", CATALOG) + 60})
        action = self.surplus.step(state)
        self.assertEqual((action["position"]["x"], action["inventory"], action["count"]), (12, "furnace_source", 40))


class RetirementTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        jobs = PLATES_DONE + STONE_DONE + COAL_DONE + retired_jobs("iron-ore", "copper-ore", "stone")
        ledger = {"schema_version": 2, "world_id": WORLD, "jobs": {str(i): j for i, j in enumerate(jobs)}}
        (root / "jobs.json").write_text(json.dumps(ledger), encoding="utf-8")
        (root / "pcb-board.json").write_text(json.dumps(board(blocks=COAL_BANK)), encoding="utf-8")
        cells = [
            {
                "drill": {"name": "burner-mining-drill", "position": {"x": -30, "y": -50}},
                "receiver": {"name": "wooden-chest", "position": {"x": -29.5, "y": -51.5}},
            }
        ]
        bootstrap = SimpleNamespace(_existing_cells=Mock(return_value={"ok": True, "cells": cells}))
        game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={"ok": True, "linked": {}})
        )
        self.retirement = sr.StarterRetirement(game, bootstrap, SimpleNamespace(owns_automated_burner=lambda e: False))
        self.storage = SimpleNamespace(state={})
        self.retirement.surplus = cs.CarriedSurplus(CATALOG, storage=self.storage, retirement=self.retirement)
        self.chest = entity("wooden-chest", -29.5, -51.5, 21, inventory={"coal": 700})
        self.cell_drill = entity("burner-mining-drill", -30, -50, 22, inventory={}, status_name="no_fuel")
        self.boiler = boiler(30)

    def tearDown(self):
        self.tmp.cleanup()

    def step(self, carried, **extra):
        state = obs(carried, self.chest, self.cell_drill, self.boiler, tick=1, **extra)
        return self.retirement.step(state, {"resource": "coal"})

    def test_chest_above_the_allowance_moves_in_consumer_sized_trips(self):
        take = self.step(ALLOW)
        self.assertEqual((take["type"], take["count"]), ("take", 20))  # only what the boiler accepts
        insert = self.step(ALLOW + 20)
        self.assertEqual((insert["type"], insert["name"], insert["count"]), ("insert", "boiler", 20))
        self.boiler["inventory"]["coal"] = 50
        waiting = self.step(ALLOW)
        self.assertEqual(waiting["reason"], "starter_retirement_waits_for_a_stock_sink")

    def test_overflow_without_sink_still_progresses_through_storage(self):
        self.boiler["inventory"]["coal"] = 50
        take = self.step(ALLOW, inventory_empty_slots=10)
        self.assertEqual((take["type"], take["count"]), ("take", 50))
        self.assertIn("no consumer or depot room", take["reason"])

    def test_registered_depot_receives_the_excess_then_retirement_finishes(self):
        self.boiler["inventory"]["coal"] = 50
        self.storage.state = {"world_id": WORLD, "chests": [{"name": "iron-chest", "unit_number": 30}]}
        depot = entity("iron-chest", 4, 4, 30, inventory={})
        state = obs(ALLOW + 50, self.chest, self.cell_drill, self.boiler, depot, tick=1)
        action = self.retirement.step(state, {"resource": "coal"})
        self.assertEqual((action["type"], action["count"], action["expected_entity_unit"]), ("deposit", 50, 30))
        self.chest["inventory"] = {}
        action = self.retirement.step(state, {"resource": "coal"})
        self.assertEqual((action["type"], action["name"]), ("mine", "burner-mining-drill"))


class FetchTests(unittest.TestCase):
    def bootstrap(self):
        boot = V2Bootstrap.__new__(V2Bootstrap)
        boot.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=None))
        boot.catalog = SimpleNamespace(fingerprint="fp", items={})
        boot.construction_buffers = {}
        boot._existing_cells = Mock(return_value={"ok": True, "cells": []})
        return boot

    def test_fuel_fetch_takes_from_the_depot_first(self):
        boot = self.bootstrap()
        storage = SimpleNamespace(state={"world_id": WORLD, "chests": [{"name": "wooden-chest", "unit_number": 9}]})
        boot.fuel_depot = cs.CarriedSurplus(CATALOG, storage=storage).depot_take
        cell_chest = entity("wooden-chest", 1, 1, 8, inventory={"coal": 200})
        depot = entity("wooden-chest", 50, 50, 9, inventory={"coal": 30})
        action = boot._take_output(obs(0, cell_chest, depot), "coal", 24)
        self.assertEqual(
            (action["position"], action["count"], action["reason"]), (depot["position"], 24, "collect depot coal")
        )

    def test_collect_produced_coal_never_exceeds_the_request(self):
        boot = self.bootstrap()
        cell_chest = entity("wooden-chest", 1, 1, 8, inventory={"coal": 400})
        action = boot._take_output(obs(0, cell_chest), "coal", 24)
        self.assertEqual((action["reason"], action["count"]), ("collect produced coal", 24))


def wood_obs(wood, *entities, coal=0):
    return dict(obs(coal, *entities), inventory={"coal": coal, "wood": wood})


class WoodTests(unittest.TestCase):
    """Owner, 2026-11-13: carried wood is burned in the power plant, boilers first."""

    def test_wood_has_no_allowance_and_waits_for_an_idle_pass_or_a_full_stack(self):
        self.assertEqual((cs.allowance("wood", CATALOG), cs.threshold("wood", CATALOG)), (0, WOOD_BATCH))
        surplus = cs.CarriedSurplus(CATALOG)
        empty = entity("boiler", 10, 0, 1, inventory={}, status_name="working")
        self.assertIsNone(surplus.step(wood_obs(WOOD_BATCH - 1, empty), idle=True))
        self.assertIsNone(surplus.step(wood_obs(60, empty)))  # a clearing batch is not interrupted for 60 wood
        idle = surplus.step(wood_obs(WOOD_BATCH, empty), idle=True)
        self.assertEqual((idle["type"], idle["name"], idle["item"], idle["count"]), ("insert", "boiler", "wood", 25))
        full = surplus.step(wood_obs(cs.WOOD_CARRY_CAP, empty))
        self.assertEqual((full["item"], full["count"]), ("wood", 50))  # one slot: 50 = default stack in CATALOG

    def test_coal_filled_boiler_takes_no_wood_but_the_wood_feed_chest_does_before_drills(self):
        feed_chest = entity("wooden-chest", 40, 0, 31, inventory={"wood": 10})
        feed = SimpleNamespace(chest=lambda o: feed_chest)
        surplus = cs.CarriedSurplus(CATALOG, wood_feed=feed)
        wood_drill = entity("burner-mining-drill", 20, 0, 2, inventory={}, status_name="working")
        state = wood_obs(120, boiler(45), wood_drill)
        action = surplus.step(state, idle=True)
        self.assertEqual((action["type"], action["expected_entity_unit"], action["count"]), ("deposit", 31, 120))
        self.assertIn("wood feed", action["reason"])
        state["inventory"]["wood"] = 0  # the deposit moved every carried wood
        self.assertIsNone(surplus.step(state, idle=True))
        no_feed = cs.CarriedSurplus(CATALOG).step(wood_obs(120, boiler(45), wood_drill), idle=True)
        self.assertEqual(no_feed["name"], "burner-mining-drill")  # drills stay secondary consumers

    def test_hand_fed_reserve_holds_one_slot_of_wood_for_the_boiler(self):
        surplus = cs.CarriedSurplus(CATALOG, reserve=lambda o, item: 100 if item == "wood" else 0)
        drill = entity("burner-mining-drill", 20, 0, 2, inventory={}, status_name="working")
        self.assertIsNone(surplus.step(wood_obs(120, boiler(45), drill), idle=True))
        action = surplus.step(wood_obs(130, boiler(45), drill), idle=True)
        self.assertEqual((action["name"], action["count"]), ("burner-mining-drill", 30))

    def test_coal_behaviour_unchanged_by_wood(self):
        surplus = cs.CarriedSurplus(CATALOG, wood_feed=SimpleNamespace(chest=lambda o: None))
        action = surplus.step(wood_obs(0, boiler(0), coal=ALLOW + 50))
        self.assertEqual((action["item"], action["count"], action["name"]), ("coal", 50, "boiler"))


if __name__ == "__main__":
    unittest.main()
