"""A built block that never works is diagnosed and repaired through the ladder's clear rung.

aud-1114-0703 seed 477133967: the power plant stood complete, but its water pipe passed the free
end of the second steam engine (-9.5, 51.5); the boiler stayed at water 0 / steam 0 with 84 MJ of
coal in it and the run waited 37 game minutes. The routing cause is fixed (b7eef43); a plant
built before (or any other fluid fault) is now found and rebuilt: paid guarded mines of the old
pipes, a new route planned first and republished in the builder state.
"""

import json
import unittest
from types import SimpleNamespace

from factorio_agent import built_block_repair as repair
from factorio_agent import plan_needs, remedy
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan

from test_v2_power_water_steam_ports import BANK, PUMP, WATER_PORT, e
from test_v2_remedy_ladder import LadderCLI, LadderFixture
from factorio_agent.supervisor import REMEDY_CHECK_SECONDS

# A routed pipe that reaches the second engine's free end (-9.5, 51.5) on its way to the port.
OLD_PIPES = [e("pipe", -7.5, 58.5)] + [e("pipe", -8.5, 58.5)] + [e("pipe", -9.5, y + 0.5) for y in range(51, 59)]
OLD_PIPES += [e("pipe", x + 0.5, 56.5) for x in range(-9, 3)] + [e("pipe", 2.5, 55.5)]
NEW_PATH = [{"x": -7.5, "y": 58.5}, {"x": -6.5, "y": 58.5}] + [{"x": x + 0.5, "y": 57.5} for x in range(-6, 3)]
NEW_PATH += [{"x": 2.5, "y": 56.5}, {"x": 2.5, "y": 55.5}]
SITE = {"direction": 8, "position": dict(PUMP["position"]), "water_out": {"x": -7.5, "y": 58.5}}
EVIDENCE = dict(ok=True, water=0, steam=0, boiler_fuel=84_000_000, connected_engines=2, generation_kw=0, hand_fed=True)


def plan():
    return _plan([PUMP] + BANK + OLD_PIPES, [{"kind": "fluid", "item": "water", "position": WATER_PORT}])


class Diagnosis(unittest.TestCase):
    def test_a_water_pipe_on_a_steam_port_is_a_fault(self):
        faults = repair.water_network_faults(plan()["entities"])
        self.assertIn({"fault": "water_pipe_on_steam_port", "position": {"x": -9.5, "y": 51.5}}, faults)

    def test_live_fluids_name_a_mixed_or_a_disconnected_network(self):
        clean = [p for p in plan()["entities"] if p["name"] != "pipe" or p["position"]["x"] != -9.5]
        pipes = [p for p in clean if p["name"] == "pipe"]
        rows = [{"standing": True, "fluids": {"water": 100}} for _ in pipes]
        self.assertEqual(repair.water_network_faults(clean, rows, boiler_water=50), [])
        faults = repair.water_network_faults(clean, rows, boiler_water=0)
        self.assertEqual([f["fault"] for f in faults], ["water_does_not_reach_boiler"])
        rows[3] = {"standing": True, "fluids": {"steam": 10}}
        self.assertEqual([f["fault"] for f in repair.water_network_faults(clean, rows, 0)], ["steam_in_water_pipe"])
        # An empty Lua table may come back as a list: no fluid, no fault from it.
        self.assertEqual(repair.water_network_faults(clean, [{"standing": True, "fluids": []}] * len(pipes), 0), [])

    def test_a_pole_on_a_planned_lane_head_is_a_belt_fault(self):
        # aud-1114-0526-2446866888: a link pole at (23.5, 10.5) in front of the iron lane head.
        belts = [e("transport-belt", 23.5, y + 0.5, 8) for y in range(8, 13)]
        live = [dict(e("small-electric-pole", 23.5, 10.5), unit_number=77), e("transport-belt", 23.5, 9.5, 8)]
        faults = repair.belt_tile_faults(belts, live)
        self.assertEqual(len(faults), 1)
        self.assertEqual((faults[0]["found"], faults[0]["unit_number"]), ("small-electric-pole", 77))

    def test_the_needs_record_and_the_clear_rung(self):
        faults = repair.water_network_faults(plan()["entities"])
        needs = repair.needs("power", faults)
        self.assertEqual(needs["kind"], "built")
        self.assertTrue(needs["reason"].startswith("built_block_fault:power:water_pipe_on_steam_port"))
        self.assertIn("rebuilt", needs["change"])
        tried = remedy.record([], remedy.REPLAN, needs, {"fits": False}, 0.0)
        rung, _ = remedy.choose("power", needs, tried)
        self.assertIs(rung, remedy.CLEAR_REBUILD)
        self.assertEqual(rung.options, {repair.OPTION: True})
        # Other planners' needs kinds never pick the clear rung.
        rung, _ = remedy.choose("power", plan_needs.make("route", "x"), tried)
        self.assertIsNot(rung, remedy.CLEAR_REBUILD)


def builder(fluids):
    b = FactoryBuilder.__new__(FactoryBuilder)
    b.state = {"world_id": "w", "seeds": {}, "power_plan": plan()}
    b.catalog = SimpleNamespace(items={"pipe": {"stack_size": 100}})
    b.game = SimpleNamespace(query=lambda script: fluids)
    b._save = lambda: None
    b.water_sites = lambda: [SITE]
    b.routes = []

    def route(source, destination, name, reserved, **kwargs):
        b.routes.append((source, destination, name, kwargs.get("ignore")))
        return {"ok": True, "path": NEW_PATH}

    b.route = route
    return b


def observation(tick, entities=()):
    return {"world_id": "w", "tick": tick, "entities": list(entities), "inventory_empty_slots": 20}


class PowerRepair(unittest.TestCase):
    FLUIDS = {"ok": True, "pipes": [{"standing": True, "fluids": {}}] * len(OLD_PIPES), "boiler_water": 0}

    def test_a_faulty_plant_blocks_with_built_needs_after_a_game_minute(self):
        b = builder(self.FLUIDS)
        power = b.state["power_plan"]
        self.assertIsNone(b._power_fault(observation(1000), power, dict(EVIDENCE, tick=1000)))
        self.assertIsNone(b._power_fault(observation(4000), power, dict(EVIDENCE, tick=4000)))
        out = b._power_fault(observation(4600), power, dict(EVIDENCE, tick=4600))
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(out["plan_needs"]["kind"], "built")
        needs = plan_needs.from_result("power", out)
        self.assertEqual(needs["kind"], "built")
        # Water and steam present, or a plant that generates: no fault, the clock restarts.
        self.assertIsNone(b._power_fault(observation(4700), power, dict(EVIDENCE, tick=4700, water=10, steam=5)))
        self.assertNotIn("power_fault_since", b.state)

    def test_the_clear_rung_replans_first_then_mines_only_the_replaced_pipes(self):
        b = builder(self.FLUIDS)
        b.state["power_fault_since"] = 0
        standing = [dict(p, unit_number=100 + i) for i, p in enumerate(OLD_PIPES)]
        obs = observation(5000, standing)
        with remedy.applied({repair.OPTION: True}):
            out = b._power_fault(obs, b.state["power_plan"], dict(EVIDENCE, tick=5000))
        self.assertEqual(out["type"], "mine")
        self.assertEqual(out["name"], "pipe")
        self.assertEqual(out["expected_entity_world_id"], "w")
        # The new route was planned with the old pipes' tiles free and republished before any mine.
        self.assertEqual(b.routes[0][3], [p["position"] for p in OLD_PIPES])
        new = {
            (p["position"]["x"], p["position"]["y"]) for p in b.state["power_plan"]["entities"] if p["name"] == "pipe"
        }
        # The bank's own pipes between its water port and the boiler are template, not route: kept.
        self.assertEqual(new, {(q["x"], q["y"]) for q in NEW_PATH} | {(2.5, 54.5), (2.5, 53.5)})
        removed = {(p["position"]["x"], p["position"]["y"]) for p in b.state["power_repair"]["remove"]}
        self.assertIn((-9.5, 51.5), removed)
        self.assertNotIn((-7.5, 58.5), removed)  # the pump's own outlet pipe stays
        self.assertEqual(b.state["power_repairs"], 1)
        # Mines go on until every replaced pipe is gone; then the record is dropped.
        self.assertEqual(b._continue_power_repair(obs)["type"], "mine")
        self.assertIsNone(b._continue_power_repair(observation(5100, standing[:1])))
        self.assertNotIn("power_repair", b.state)

    def test_repairs_are_bounded(self):
        b = builder(self.FLUIDS)
        b.state.update(power_fault_since=0, power_repairs=repair.REPAIRS_MAX)
        with remedy.applied({repair.OPTION: True}):
            out = b._power_fault(observation(5000), b.state["power_plan"], dict(EVIDENCE, tick=5000))
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(b.routes, [])


class LadderAdmission(LadderFixture):
    def test_a_power_job_with_built_needs_is_repaired_by_the_clear_rung(self):
        self.s.max_seconds = None
        ladder = LadderCLI(self.cli, lambda rung, options: {"fits": None, "reason": "no_pure_planner"})
        self.blocked("pw", "power", "built_block_fault:power:water_pipe_on_steam_port")
        self.cli.jobs["pw"]["needs"] = repair.needs("power", repair.water_network_faults(plan()["entities"]))
        self.assertFalse(self.s._auto_retry(self.s._snapshot()))
        self.clock.advance(REMEDY_CHECK_SECONDS + 1)
        self.assertTrue(self.s._auto_retry(self.s._snapshot()))
        self.assertEqual(ladder.what_ifs, ["clear:rebuild_faulty_part"])
        retry = json.loads(self.retries()[-1][-1])
        self.assertEqual((retry["rung"], retry["options"]), ("clear:rebuild_faulty_part", {repair.OPTION: True}))


if __name__ == "__main__":
    unittest.main()
