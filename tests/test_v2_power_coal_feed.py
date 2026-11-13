"""Starter power in two phases: hand-fed boiler first, dedicated coal feed later (power_coal_feed.py)."""

import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from factorio_agent import power_coal_feed as pcf
from factorio_agent import progression as pg
from factorio_agent.capacity_planning import CapacityConfig, fuel_cells_needed
from factorio_agent.core.deterministic_builder import (
    BELT_FED,
    HAND_FED,
    FactoryBuilder,
    boiler_coal_per_minute,
    coal_feed_mode,
)
from factorio_agent.executors import StarterExecutors
from factorio_agent.jobs import CAPABILITIES

BOILER = {"name": "boiler", "position": {"x": 34.0, "y": 8.5}, "direction": 0}
DRILL = {"name": "burner-mining-drill", "position": {"x": 10.0, "y": 0.0}, "direction": 4}
POWER = {
    "ok": True,
    "entities": [
        BOILER,
        {"name": "steam-engine", "position": {"x": 34.5, "y": 4.5}, "direction": 0},
        {"name": "steam-engine", "position": {"x": 34.5, "y": -0.5}, "direction": 0},
        {"name": "small-electric-pole", "position": {"x": 36.5, "y": 4.5}},
    ],
    "ports": [{"kind": "power", "item": "electricity", "position": {"x": 34.5, "y": 4.5}}],
}
COAL_PLAN = {
    "ok": True,
    "entities": [
        DRILL,
        {"name": "burner-inserter", "position": {"x": 11.5, "y": 0.5}, "direction": 4},
        {"name": "transport-belt", "position": {"x": 11.5, "y": -0.5}, "direction": 4},
        {"name": "transport-belt", "position": {"x": 12.5, "y": -0.5}, "direction": 4},
    ],
    "ports": [{"kind": "item", "item": "coal", "direction": "output", "position": {"x": 12.5, "y": -0.5}}],
    "required_items": {"burner-mining-drill": 1, "burner-inserter": 1, "transport-belt": 2},
    "drill": DRILL,
}
CATALOG = SimpleNamespace(
    fingerprint="c",
    entities={"boiler": {"energy_usage": 30000}},
    items={"coal": {"fuel_value": 4_000_000, "stack_size": 50}},
)
PLANT_EVIDENCE = {
    "ok": True,
    "water": 200,
    "steam": 400,
    "boiler_fuel": 32_000_000,
    "drill_fuel": 0,
    "drill_powered": False,
    "coal_on_belts": 0,
    "connected_engines": 2,
    "energized_engines": 2,
}


def builder(directory, **state):
    b = FactoryBuilder.__new__(FactoryBuilder)
    b.catalog = CATALOG
    b.path = Path(directory) / "factory-builder.json"
    b.game = SimpleNamespace(backend="assisted", cfg=SimpleNamespace(runtime_dir=directory))
    b.bootstrap = SimpleNamespace(ensure_item=Mock(return_value={"status": "waiting", "reason": "no coal"}))
    b.state = {"schema_version": 1, "world_id": "w", "seeds": {}, "catalog_fingerprint": "c", "power_plan": POWER}
    b.state.update(state)
    b.ensure_plan = Mock(return_value={"status": "succeeded"})
    b._choose_coal_plan = Mock(return_value=COAL_PLAN)
    return b


def boiler_row(coal=8, burning=0.0):
    return dict(BOILER, inventory={"coal": coal} if coal else {}, remaining_burning_fuel=burning, unit_number=7)


def obs(tick=1000, coal_carried=0, boiler=None, extra=()):
    entities = [boiler if boiler is not None else boiler_row()] + list(extra)
    return {
        "world_id": "w",
        "tick": tick,
        "technologies": {"steam-power": True},
        "inventory": {"coal": coal_carried} if coal_carried else {},
        "entities": entities,
        "position": {"x": 34, "y": 8},
    }


def job(capability, status="succeeded", parameters=None, evidence=None):
    return {"capability": capability, "status": status, "parameters": parameters or {}, "evidence": evidence or {}}


HAND_FED_POWER = job(
    "power",
    evidence={"hand_fed": True, "tick": 20000, "boiler_full_load_coal_per_minute": 27.0, "coal_fuel_value": 4_000_000},
)
LEGACY_POWER = job("power", evidence={"coal_on_belts": 8, "tick": 45971, "drill_fuel": 1})


class HandFedPowerTests(unittest.TestCase):
    def test_power_succeeds_without_the_belt_and_reports_hand_fed(self):
        with tempfile.TemporaryDirectory() as d:
            b = builder(d)
            b.power_evidence = Mock(return_value=dict(PLANT_EVIDENCE, tick=1000))
            first = b.ensure_power(obs(tick=1000))
            self.assertEqual(first["status"], "waiting")
            b.power_evidence.return_value = dict(PLANT_EVIDENCE, tick=2900)
            result = b.ensure_power(obs(tick=2900))
            self.assertEqual(result["status"], "succeeded", result)
            self.assertIn("hand-fed boiler", result["reason"])
            evidence = result["evidence"]
            self.assertTrue(evidence["hand_fed"])
            self.assertEqual(evidence["coal_feed"], "planned")
            self.assertEqual(evidence["boiler_full_load_coal_per_minute"], 27.0)
            self.assertTrue(evidence["flow_verified"])
            self.assertGreaterEqual(evidence["verification_ticks"], 1800)
            # Only the plant was built; the feed is reserved, not built.
            b.ensure_plan.assert_called_with(obs(tick=2900), POWER)
            self.assertEqual(b.state["coal_plan"], COAL_PLAN)
            self.assertEqual(b.state["power_mode"], HAND_FED)
            b.power_evidence.assert_called_with(POWER, None)

    def test_cold_boiler_waits_for_the_upkeep(self):
        with tempfile.TemporaryDirectory() as d:
            b = builder(d)
            b.power_evidence = Mock()
            result = b.ensure_power(obs(boiler=boiler_row(coal=0)))
            self.assertEqual(result["status"], "waiting")
            self.assertIn("boiler upkeep", result["reason"])
            b.power_evidence.assert_not_called()

    def test_old_saved_state_with_the_full_power_plan_stays_belt_fed(self):
        with tempfile.TemporaryDirectory() as d:
            b = builder(d, coal_plan=COAL_PLAN, power_verified_once=True, power_sample_tick=0)
            self.assertEqual(coal_feed_mode(b.state), BELT_FED)
            b._seed = Mock(return_value=None)
            b.power_evidence = Mock(return_value=dict(PLANT_EVIDENCE, tick=5000, drill_fuel=1, coal_on_belts=8))
            result = b.ensure_power(obs(tick=5000))
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["reason"], "steam power has a dedicated self-fueled coal feed")
            self.assertNotIn("power_mode", b.state)
            b._choose_coal_plan.assert_not_called()
            # No hand fuelling on an old map.
            self.assertIsNone(pcf.boiler_fuel_action(b, b.bootstrap, CATALOG, obs(boiler=boiler_row(coal=0))))

    def test_feed_step_builds_proves_and_stops_hand_fuelling(self):
        with tempfile.TemporaryDirectory() as d:
            b = builder(d, coal_plan=COAL_PLAN, power_mode=HAND_FED, power_sample_tick=0)
            b._seed = Mock(return_value=None)
            ready = dict(PLANT_EVIDENCE, drill_fuel=1, coal_on_belts=4)
            b.power_evidence = Mock(return_value=dict(ready, tick=10000))
            self.assertEqual(b.ensure_coal_feed(obs(tick=10000))["status"], "waiting")  # own 30 s sample
            self.assertTrue(b.state["coal_feed_started"])
            b.power_evidence.return_value = dict(ready, tick=11900)
            result = b.ensure_coal_feed(obs(tick=11900))
            self.assertEqual(result["status"], "succeeded", result)
            self.assertIn("hand-fuelling stopped", result["reason"])
            self.assertTrue(result["evidence"]["flow_verified"])
            self.assertEqual(b.state["power_mode"], BELT_FED)
            b.ensure_plan.assert_any_call(obs(tick=11900), COAL_PLAN)
            self.assertIsNone(pcf.boiler_fuel_action(b, b.bootstrap, CATALOG, obs(boiler=boiler_row(coal=0))))

    def test_feed_step_replans_a_route_built_over_while_hand_fed(self):
        with tempfile.TemporaryDirectory() as d:
            b = builder(d, coal_plan=COAL_PLAN, power_mode=HAND_FED)
            b.ensure_plan = Mock(
                side_effect=[
                    {"status": "succeeded"},
                    {"status": "blocked", "reason": "reserved block placement is obstructed", "evidence": {}},
                ]
            )
            result = b.ensure_coal_feed(obs())
            self.assertEqual(result["status"], "waiting")
            self.assertNotIn("coal_plan", b.state)
            self.assertEqual(b.state["coal_feed_replans"], 1)


class BoilerUpkeepTests(unittest.TestCase):
    def test_sizing_comes_from_catalog_energy_values(self):
        self.assertEqual(boiler_coal_per_minute(CATALOG), 27.0)  # 30 kJ/tick * 3600 / 4 MJ
        sizing = pcf.boiler_sizing(CATALOG)
        self.assertEqual(sizing, {"burn_per_minute": 27.0, "refill_below": 14, "target": 50})
        half = SimpleNamespace(entities={"boiler": {"energy_usage": 15000}}, items=CATALOG.items)
        self.assertEqual(pcf.boiler_sizing(half), {"burn_per_minute": 13.5, "refill_below": 8, "target": 27})

    def test_refill_in_one_batch_when_low(self):
        with tempfile.TemporaryDirectory() as d:
            b = builder(d, power_mode=HAND_FED)
            self.assertIsNone(pcf.boiler_fuel_action(b, b.bootstrap, CATALOG, obs(boiler=boiler_row(coal=20))))
            action = pcf.boiler_fuel_action(b, b.bootstrap, CATALOG, obs(coal_carried=60, boiler=boiler_row(coal=10)))
            self.assertEqual(action["type"], "insert")
            self.assertEqual((action["name"], action["item"], action["inventory"]), ("boiler", "coal", "fuel"))
            self.assertEqual(action["count"], 40)  # fill the slot to 50
            # Burning remainder counts: 10 coal + 4 coal worth burning = 14, not below the threshold.
            self.assertIsNone(
                pcf.boiler_fuel_action(b, b.bootstrap, CATALOG, obs(coal_carried=60, boiler=boiler_row(10, 16e6)))
            )

    def test_small_carry_waits_for_a_batch_unless_the_boiler_is_out(self):
        with tempfile.TemporaryDirectory() as d:
            b = builder(d, power_mode=HAND_FED)
            b.bootstrap.ensure_item = Mock(return_value={"type": "take_many", "sources": []})
            action = pcf.boiler_fuel_action(b, b.bootstrap, CATALOG, obs(coal_carried=3, boiler=boiler_row(coal=5)))
            self.assertEqual(action["type"], "take_many")
            b.bootstrap.ensure_item.assert_called_once_with(obs(coal_carried=3, boiler=boiler_row(coal=5)), "coal", 45)
            empty = pcf.boiler_fuel_action(b, b.bootstrap, CATALOG, obs(coal_carried=3, boiler=boiler_row(coal=0)))
            self.assertEqual((empty["type"], empty["count"]), ("insert", 3))
            b.bootstrap.ensure_item = Mock(return_value={"status": "waiting", "reason": "waiting for coal"})
            self.assertIsNone(pcf.boiler_fuel_action(b, b.bootstrap, CATALOG, obs(boiler=boiler_row(coal=5))))

    def test_boiler_goes_before_the_burner_cells_in_starter_upkeep(self):
        with tempfile.TemporaryDirectory() as d:
            ex = StarterExecutors.__new__(StarterExecutors)
            ex.builder = builder(d, power_mode=HAND_FED)
            ex.bootstrap = ex.builder.bootstrap
            ex.bootstrap.next_action = Mock(return_value={"type": "insert", "name": "stone-furnace"})
            ex.catalog = CATALOG
            ex.starter_observation = lambda o: o
            result = ex.starter_upkeep(obs(coal_carried=50, boiler=boiler_row(coal=2)))
            self.assertEqual(result["name"], "boiler")
            ex.bootstrap.next_action.assert_not_called()
            # A stocked boiler leaves the turn to the burner cells.
            stocked = obs(coal_carried=50, boiler=boiler_row(coal=30))
            self.assertEqual(ex.starter_upkeep(stocked)["name"], "stone-furnace")


class FeedStepRuleTests(unittest.TestCase):
    def test_not_before_belts_are_machine_made(self):
        self.assertFalse(pcf.feed_step_ready([], {"tick": 30000}))
        self.assertFalse(pcf.feed_step_ready([HAND_FED_POWER], {"tick": 30000}))
        cell = job("supply_cell", parameters={"products": ["iron-gear-wheel"]})
        self.assertFalse(pcf.feed_step_ready([HAND_FED_POWER, cell], {"tick": 30000}))
        building = job("supply_cell", status="running", parameters={"products": ["transport-belt"]})
        self.assertFalse(pcf.feed_step_ready([HAND_FED_POWER, building], {"tick": 30000}))

    def test_ready_once_belts_are_machine_made_or_fallback_or_legacy(self):
        belts = job("supply_cell", parameters={"products": ["iron-gear-wheel", "transport-belt"]})
        self.assertTrue(pcf.feed_step_ready([HAND_FED_POWER, belts], {"tick": 30000}))
        row = job("produce", parameters={"item": "transport-belt", "rate_per_minute": 20})
        self.assertTrue(pcf.feed_step_ready([HAND_FED_POWER, row], {"tick": 30000}))
        self.assertTrue(pcf.feed_step_ready([HAND_FED_POWER], {"tick": 20000 + pcf.FEED_FALLBACK_TICKS}))
        self.assertTrue(pcf.feed_step_ready([LEGACY_POWER], {"tick": 46000}))

    def test_roadmap_proposes_the_feed_step_low_and_electric_coal_waits_for_it(self):
        order = [s.id for s in pg.ROADMAP]
        self.assertLess(order.index("power"), order.index("power_coal_feed"))
        self.assertLess(order.index("power_coal_feed"), order.index("electric_coal"))
        step = {s.id: s for s in pg.ROADMAP}["electric_coal"]
        self.assertIn("power_coal_feed", step.after)
        self.assertIn("power_coal_feed", CAPABILITIES["electric_coal"]["requires"])
        self.assertEqual(CAPABILITIES["power_coal_feed"]["requires"], ["power"])
        # The feed's belts moved out of the power step.
        self.assertEqual(pg.POWER_BUILDS["transport-belt"], 5)
        self.assertNotIn("burner-mining-drill", pg.POWER_BUILDS)
        self.assertGreaterEqual(pg.POWER_COAL_FEED_BUILDS["transport-belt"], 95)

        base = [job("starter"), HAND_FED_POWER]
        ids = [n["id"] for n in pg.autopilot_nodes(base, [], {"tick": 30000}, CAPABILITIES, limit=10)]
        self.assertNotIn("auto_power_coal_feed", ids)
        self.assertIn("auto_starter_research_automation", ids)  # research does not wait for the belt
        self.assertIn("auto_mining_outpost_iron", ids)
        belts = job("supply_cell", parameters={"products": ["transport-belt"]})
        nodes = pg.autopilot_nodes(base + [belts], [], {"tick": 30000}, CAPABILITIES, limit=10)
        feed = next(n for n in nodes if n["id"] == "auto_power_coal_feed")
        self.assertEqual(feed["priority"], pg.TOP_PRIORITY - 400)
        self.assertTrue(all(feed["priority"] < n["priority"] for n in nodes if n["id"].startswith("auto_mining")))
        researched = {"tick": 30000, "technologies": ["automation", "electric-mining-drill"]}
        ids = [n["id"] for n in pg.autopilot_nodes(base, [], researched, CAPABILITIES, limit=10)]
        self.assertNotIn("auto_electric_coal", ids)
        ids = [
            n["id"] for n in pg.autopilot_nodes(base + [job("power_coal_feed")], [], researched, CAPABILITIES, limit=10)
        ]
        self.assertIn("auto_electric_coal", ids)

    def test_old_map_gets_the_feed_step_at_once(self):
        nodes = pg.autopilot_nodes([job("starter"), LEGACY_POWER], [], {"tick": 46000}, CAPABILITIES, limit=10)
        self.assertIn("auto_power_coal_feed", [n["id"] for n in nodes])


class CoalAccountingTests(unittest.TestCase):
    def test_hand_fed_boiler_is_a_coal_consumer_until_the_feed_exists(self):
        measured = {"electricity": {"networks": [{"generation_kw": 300.0}]}}
        self.assertEqual(pcf.hand_fed_coal_per_minute([HAND_FED_POWER], measured), 4.5)  # 300 kW / 4 MJ * 60
        self.assertEqual(pcf.hand_fed_coal_per_minute([HAND_FED_POWER], {}), 27.0 * pcf.UNMEASURED_LOAD_SHARE)
        busy = {"electricity": {"networks": [{"generation_kw": 5000.0}]}}
        self.assertEqual(pcf.hand_fed_coal_per_minute([HAND_FED_POWER], busy), 27.0)
        self.assertEqual(pcf.hand_fed_coal_per_minute([HAND_FED_POWER, job("power_coal_feed")], measured), 0.0)
        self.assertEqual(pcf.hand_fed_coal_per_minute([LEGACY_POWER], measured), 0.0)
        supply = {"iron-plate": {"evidence": {"installed_live_cells": 3}}}
        config = CapacityConfig()
        without = fuel_cells_needed(supply, [], config)
        with_boiler = fuel_cells_needed(supply, [], config, 13.5)
        self.assertEqual(without, math.ceil(3 * 3.6 * 1.2 / 12.75))
        self.assertEqual(with_boiler, math.ceil((3 * 3.6 + 13.5) * 1.2 / 12.75))

    def test_reserved_feed_is_not_in_the_current_bill_until_its_step_starts(self):
        self.assertFalse(pcf.feed_in_bill({"coal_plan": COAL_PLAN, "power_mode": HAND_FED}))
        self.assertTrue(pcf.feed_in_bill({"coal_plan": COAL_PLAN, "power_mode": HAND_FED, "coal_feed_started": True}))
        self.assertTrue(pcf.feed_in_bill({"coal_plan": COAL_PLAN}))  # old map: belt-fed


class BoardTests(unittest.TestCase):
    def test_planned_feed_is_published_while_hand_fed_and_dropped_once_belt_fed(self):
        from factorio_agent.board import foreign_belts, planned_unbuilt_hardware

        with tempfile.TemporaryDirectory() as d:
            b = builder(d, coal_plan=COAL_PLAN, power_mode=HAND_FED)
            ex = SimpleNamespace(builder=b, game=b.game, catalog=CATALOG)
            self.assertIsNone(pcf.publish_planned_feed(ex, obs()))
            board = json.loads((Path(d) / "pcb-board.json").read_text(encoding="utf-8"))
            block = board["blocks"]["power_coal_feed"]
            self.assertEqual(block["ports"], [])
            self.assertEqual(block["reserved_area"], [[9.0, -1.0], [11.0, 1.0]])
            planned = planned_unbuilt_hardware(b, obs(), board["blocks"])
            self.assertEqual(planned[(12.5, -0.5)]["block"], "power_coal_feed")  # a planned belt tile is taken
            belts = foreign_belts(obs(), board["blocks"])
            self.assertEqual(len([e for e in belts if e["name"] == "transport-belt"]), 2)
            b.state["power_mode"] = BELT_FED
            pcf.publish_planned_feed(ex, obs())
            board = json.loads((Path(d) / "pcb-board.json").read_text(encoding="utf-8"))
            self.assertNotIn("power_coal_feed", board["blocks"])


if __name__ == "__main__":
    unittest.main()
