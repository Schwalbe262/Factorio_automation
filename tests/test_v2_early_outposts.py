"""Early outposts, parked jobs, the stocked-belt coal feed gate and starter cell keep-outs.

Rules: early_start.py (rules 7-9), docs/v2/PLANNERS.md "Early start" and
"Starter power in two phases".
"""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from factorio_agent import early_start as es
from factorio_agent import power_coal_feed as pcf
from factorio_agent import progression as pg
from factorio_agent.board import KEEPOUT_LUA, starter_keepouts
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.jobs import CAPABILITIES, MAX_ACTIVE_JOBS, Request, admission_lane, run_slice, submit
from factorio_agent.outposts import MiningOutposts
from factorio_agent.power_layout import compact_coal_cell, fringe_coal_cells
from factorio_agent.supply_planning import _pending

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None

ZONES = {"known": True, "reason": "zones"}
EMD = "electric-mining-drill"


def job(step_id, status="succeeded"):
    step = {s.id: s for s in pg.ROADMAP}[step_id]
    return {"capability": step.capability, "parameters": dict(step.parameters), "status": status}


def minute_eleven():
    """Power just stands, automation is researching, nothing else runs."""
    return [job("starter"), job("power"), job("starter_research_automation", "running")]


def ids(nodes):
    return [n["id"] for n in nodes]


class ProposalTests(unittest.TestCase):
    def propose(self, jobs, observation):
        return pg.autopilot_nodes(jobs, [], observation, CAPABILITIES, limit=10)

    def observation(self, **extra):
        return {"early_start_inputs": {"mining_outpost": ZONES}, "enabled_recipes": ["burner-mining-drill"], **extra}

    def test_outposts_are_proposed_right_after_power_with_drills_waiting(self):
        nodes = {n["id"]: n for n in self.propose(minute_eleven(), self.observation())}
        for resource in ("iron", "copper", "coal"):
            node = nodes["auto_mining_outpost_" + resource]
            self.assertIn("early start", node["reason"])
            self.assertIn("waits for research electric-mining-drill", node["reason"])

    def test_not_without_the_site_plan_zones_or_a_coming_drill_research(self):
        unknown = self.observation(early_start_inputs={"mining_outpost": {"known": False, "reason": "x"}})
        self.assertNotIn("auto_mining_outpost_iron", ids(self.propose(minute_eleven(), unknown)))
        stalled = minute_eleven() + [job("starter_research_electric_mining_drill", "blocked")]
        self.assertNotIn("auto_mining_outpost_iron", ids(self.propose(stalled, self.observation())))
        before_power = [job("starter")]
        self.assertNotIn("auto_mining_outpost_iron", ids(self.propose(before_power, self.observation())))

    def test_district_follows_the_early_banks_while_nothing_is_researched(self):
        jobs = minute_eleven() + [job(s, "waiting") for s in ("mining_outpost_iron", "mining_outpost_copper")]
        jobs.append(job("mining_outpost_coal", "waiting"))
        observation = self.observation()
        observation["early_start_inputs"]["smelting_district"] = {"known": True, "reason": "ports"}
        node = next(n for n in self.propose(jobs, observation) if n["id"] == "auto_smelting_district")
        self.assertIn("waits for research logistics", node["reason"])

    def test_roadmap_research_counts_as_coming_only_when_it_is_next(self):
        self.assertEqual(es.roadmap_technologies_next({}, [job("starter")]), set())
        coming = es.technologies_coming({}, minute_eleven())
        self.assertTrue({"automation", EMD, "logistics"} <= coming)
        self.assertNotIn("steel-processing", coming)  # its step needs automation_science first


class InputTests(unittest.TestCase):
    def test_mining_zones_need_a_zone_with_an_output_side_per_outpost_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            zones = [{"kind": "mining", "name": "mining:" + r, "output_side": "S"} for r in ("iron-ore", "copper-ore")]
            (Path(tmp) / "site-plan.json").write_text(json.dumps({"world_id": "w", "plan": {"zones": zones}}))
            row = es.mining_zones(tmp, {"world_id": "w"})
            self.assertFalse(row["known"])
            self.assertIn("coal", row["reason"])
            zones.append({"kind": "mining", "name": "mining:coal", "output_side": "W"})
            (Path(tmp) / "site-plan.json").write_text(json.dumps({"world_id": "w", "plan": {"zones": zones}}))
            self.assertTrue(es.inputs_receipt(tmp, {"world_id": "w"}, CAPABILITIES)["mining_outpost"]["known"])
            self.assertFalse(es.mining_zones(tmp, {"world_id": "other"})["known"])

    def test_the_outpost_declaration(self):
        early = CAPABILITIES["mining_outpost"]["early_start"]
        self.assertEqual(early, {"requires": ["power"], "inputs": "mining_zones", "locked_until": [EMD]})
        self.assertEqual(CAPABILITIES["mining_outpost"]["requires"], ["power"])  # proof and strict rule unchanged
        self.assertTrue(es.runs_early(CAPABILITIES, "mining_outpost", {"power"}, {}))
        self.assertFalse(es.runs_early(CAPABILITIES, "mining_outpost", {"power"}, {EMD: True}))
        self.assertFalse(es.is_early(CAPABILITIES, "mining_outpost", {"power"}))


class Game:
    def __init__(self):
        self.observation = {"ok": True, "world_id": "world", "tick": 100, "entities": [], "production": {}}

    def observe(self):
        return json.loads(json.dumps(self.observation))

    def act(self, action):
        self.observation["tick"] += 1
        return {"ok": True}

    def save(self):
        pass


class Executor:
    def __init__(self, result):
        self.result = result

    def step(self, capability, obs, parameters=None):
        return json.loads(json.dumps(self.result))

    def proof(self, capability, obs, result, parameters=None):
        return {"ok": True}


PARKED = {
    "status": "waiting",
    "reason": "waiting for deferred plan entities: electric-mining-drill: waiting for research",
    "evidence": {"deferred": [EMD], "research_only": True},
}


class LaneAndSlotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = Game()
        for capability in ("starter", "power"):
            self.submit(capability, capability)
            run_slice(self.root, self.game, Executor({"status": "succeeded"}), capability)

    def submit(self, key, capability, parameters=None):
        request = Request(key, "world", self.game.observation["tick"], capability, parameters or {})
        return submit(self.root, self.game, request)

    def test_outposts_run_one_per_resource(self):
        self.assertNotEqual(
            admission_lane("mining_outpost", {"resource": "iron-ore"}),
            admission_lane("mining_outpost", {"resource": "coal"}),
        )
        self.submit("iron", "mining_outpost", {"resource": "iron-ore", "rate_per_minute": 60})
        with self.assertRaisesRegex(ValueError, "already active"):
            self.submit("iron2", "mining_outpost", {"resource": "iron-ore", "rate_per_minute": 30})

    def test_parked_outposts_hold_no_job_slot(self):
        for resource in ("iron-ore", "copper-ore", "coal"):
            self.submit(resource, "mining_outpost", {"resource": resource, "rate_per_minute": 30})
        self.submit("research", "bootstrap_research", {"technology": EMD})
        # The three outposts share one slot (all-1113-2107: the coal outpost waited 5 minutes for one).
        self.submit("cell", "supply_cell", {"products": ["transport-belt"]})
        with self.assertRaisesRegex(ValueError, "early start keeps a job slot free"):
            self.submit("district", "smelting_district")
        for resource in ("iron-ore", "copper-ore", "coal"):
            self.assertEqual(
                run_slice(self.root, self.game, Executor(PARKED), resource, seconds=1)["status"], "waiting"
            )
        # Three parked outposts: research + cell hold two slots, an early district keeps the last one free.
        self.submit("district", "smelting_district")
        self.assertEqual(MAX_ACTIVE_JOBS, 4)

    def test_outposts_of_every_resource_hold_one_slot_together(self):
        self.submit("research", "bootstrap_research", {"technology": EMD})
        self.submit("cell", "supply_cell", {"products": ["transport-belt"]})
        self.submit("iron", "mining_outpost", {"resource": "iron-ore", "rate_per_minute": 60})
        self.submit("copper", "mining_outpost", {"resource": "copper-ore", "rate_per_minute": 60})
        self.submit("coal", "mining_outpost", {"resource": "coal", "rate_per_minute": 30})  # was refused
        with self.assertRaisesRegex(ValueError, "jobs may be active|early start"):
            self.submit("district", "smelting_district")

    def test_a_parked_job_does_not_run_its_deadline_while_its_research_is_missing(self):
        self.submit("iron", "mining_outpost", {"resource": "iron-ore", "rate_per_minute": 60})
        run_slice(self.root, self.game, Executor(PARKED), "iron", seconds=1)
        for progress in (0.2, 0.4, 0.6, 0.8):
            self.game.observation["tick"] += 30000
            self.game.observation["research_progress"] = progress
            result = run_slice(self.root, self.game, Executor(PARKED), "iron", seconds=1)
        self.assertEqual(result["status"], "waiting", result.get("reason"))
        self.assertGreater(result["early_ticks"], 108000)


def recipe(name, ingredients):
    return {
        "name": name,
        "ingredients": [{"type": "item", "name": n, "amount": a} for n, a in ingredients.items()],
        "products": [{"type": "item", "name": name, "amount": 1}],
    }


class Catalog:
    fingerprint = "c"
    entities = {
        EMD: {"type": "mining-drill"},
        "transport-belt": {"type": "transport-belt"},
        "small-electric-pole": {"type": "electric-pole"},
    }
    recipes = {
        EMD: recipe(EMD, {"iron-plate": 10}),
        "transport-belt": recipe("transport-belt", {"iron-plate": 2}),
        "small-electric-pole": recipe("small-electric-pole", {"wood": 1}),
    }
    technologies = {EMD: {"unlocks": [EMD]}}

    def recipe_for_product(self, item):
        return self.recipes.get(item)


class Bootstrap:
    def __init__(self):
        self.calls = []

    def ensure_item(self, observation, item, count):
        self.calls.append(item)
        return {"type": "craft", "recipe": item, "count": count}


def at(name, x, y=0.5, direction=0, **extra):
    return {"name": name, "position": {"x": x, "y": y}, "direction": direction, **extra}


BANK = {
    "ok": True,
    "resource_cell": True,
    "entities": [at(EMD, 1.5, 1.5), at("transport-belt", 3.5), at("small-electric-pole", 5.5)],
}


class BankBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bootstrap = Bootstrap()
        game = SimpleNamespace(
            backend="character", cfg=SimpleNamespace(runtime_dir=self.temp.name), query=lambda body: {"ok": True}
        )
        self.builder = FactoryBuilder(game, self.bootstrap, Catalog())

    def obs(self, inventory=None, entities=()):
        enabled = {"transport-belt": True, "small-electric-pole": True, EMD: False}
        return {
            "world_id": "w",
            "tick": 1,
            "entities": list(entities),
            "inventory": inventory or {},
            "enabled_recipes": enabled,
        }

    def test_belts_and_poles_are_built_and_the_drills_wait_parked(self):
        obs = self.obs({"transport-belt": 5})
        defer = es.Deferral(Catalog(), obs, coming={EMD})
        action = self.builder.ensure_plan(obs, BANK, defer=defer)
        self.assertEqual((action["type"], action["name"]), ("build", "transport-belt"))
        self.assertNotIn(EMD, self.bootstrap.calls)
        built = [at("transport-belt", 3.5, unit_number=1), at("small-electric-pole", 5.5, unit_number=2)]
        result = self.builder.ensure_plan(self.obs({}, built), BANK, defer=es.Deferral(Catalog(), obs, coming={EMD}))
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["evidence"]["deferred"], [EMD])
        self.assertIs(result["evidence"]["research_only"], True)
        self.assertTrue(es.parked({"status": "waiting", "evidence": result["evidence"]}))

    def test_a_belt_from_the_supply_cell_is_never_hand_crafted_while_early(self):
        obs = self.obs({})
        defer = es.Deferral(Catalog(), obs, coming={EMD}, machine_made={"transport-belt"})
        built = [at("small-electric-pole", 5.5, unit_number=2)]
        result = self.builder.ensure_plan(self.obs({}, built), BANK, defer=defer)
        self.assertEqual(result["status"], "waiting")
        self.assertIn("machine-made transport-belt", result["reason"])
        self.assertIs(result["evidence"]["research_only"], False)  # waits for stock, not only research

    def test_machine_made_items_come_from_an_unretired_supply_cell(self):
        cell = {"capability": "supply_cell", "status": "running", "parameters": {"products": ["transport-belt"]}}
        self.assertEqual(es.machine_made_items([cell], {}), {"transport-belt"})
        retire = {"capability": "supply_cell", "status": "running", "parameters": {"retire": True}}
        self.assertEqual(es.machine_made_items([cell, retire], {}), set())
        receipt = {
            "supply_cell_candidates": {"products": ["iron-gear-wheel"]},
            "enabled_recipes": ["assembling-machine-1"],
        }
        self.assertEqual(es.machine_made_items([], receipt), {"iron-gear-wheel"})
        self.assertEqual(es.machine_made_items([], dict(receipt, enabled_recipes=[])), set())


class OutpostStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.outposts = MiningOutposts.__new__(MiningOutposts)
        self.outposts.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(self.root)))
        self.outposts.builder = SimpleNamespace(
            catalog=Catalog(), ensure_plan=Mock(return_value={"status": "waiting", "reason": "w"})
        )
        self.outposts.perimeter = None
        self.outposts.path = self.root / "mining-outposts.json"
        bank = {"ok": True, "ready": False, "nominal_rate_per_minute": 60, "plan": dict(BANK, resource="iron-ore")}
        self.outposts.state = {"world_id": "w", "banks": [bank]}
        self.obs = {"world_id": "w", "tick": 5, "entities": [], "technologies": {}, "enabled_recipes": {EMD: False}}

    def ledger(self, *jobs):
        (self.root / "jobs.json").write_text(json.dumps({"world_id": "w", "jobs": dict(enumerate(jobs))}))

    def test_blocked_while_the_drill_research_is_not_coming(self):
        self.ledger({"capability": "power", "status": "succeeded"})
        result = self.outposts.step(self.obs, {"resource": "iron-ore", "rate_per_minute": 60})
        self.assertEqual(result["reason"], "electric_mining_research_required")

    def test_the_saved_bank_is_built_with_its_drills_deferred_while_research_is_coming(self):
        self.ledger(
            {"capability": "power", "status": "succeeded"},
            {"capability": "bootstrap_research", "status": "running", "parameters": {"technology": EMD}},
        )
        result = self.outposts.step(self.obs, {"resource": "iron-ore", "rate_per_minute": 60})
        self.assertEqual(result["status"], "waiting")
        defer = self.outposts.builder.ensure_plan.call_args.kwargs["defer"]
        self.assertEqual(defer.coming, {EMD})
        self.assertFalse(self.outposts.state["banks"][0]["ready"])  # proof unchanged: no drills, not ready


class SupplyPlanningTests(unittest.TestCase):
    def test_an_early_outpost_never_holds_back_starter_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = {"capability": "mining_outpost", "status": "waiting", "parameters": {"resource": "iron-ore"}}
            (Path(tmp) / "jobs.json").write_text(json.dumps({"world_id": "w", "jobs": {"a": row}}))
            self.assertEqual(_pending(Path(tmp), "w", drills_researched=False)[0], {})
            self.assertEqual(_pending(Path(tmp), "w")[0], {"iron-ore": "waiting"})


HAND_FED_POWER = {
    "capability": "power",
    "status": "succeeded",
    "evidence": {"hand_fed": True, "tick": 20000, "coal_feed": "planned"},
}


class CoalFeedGateTests(unittest.TestCase):
    def test_stocked_belts_open_the_feed_step_before_the_cell_job_succeeds(self):
        building = {"capability": "supply_cell", "status": "running", "parameters": {"products": ["transport-belt"]}}
        jobs = [HAND_FED_POWER, building]
        self.assertFalse(pcf.feed_step_ready(jobs, {"tick": 30000, "coal_feed_belts": {"needed": 95, "stocked": 94}}))
        self.assertTrue(pcf.feed_step_ready(jobs, {"tick": 30000, "coal_feed_belts": {"needed": 95, "stocked": 95}}))

    def test_receipt_counts_carried_belts_and_the_cell_chest(self):
        with tempfile.TemporaryDirectory() as tmp:
            belts = [at("transport-belt", x) for x in range(12)]
            builder = {"world_id": "w", "power_plan": {"entities": []}, "power_mode": "hand_fed"}
            builder["coal_plan"] = {"entities": belts + [at("burner-mining-drill", 0, 5)]}
            (Path(tmp) / "factory-builder.json").write_text(json.dumps(builder))
            chest = {"x": 36.5, "y": -1.5}
            cell = {"world_id": "w", "phase": "building", "products": [{"item": "transport-belt", "output": chest}]}
            (Path(tmp) / "supply-cell.json").write_text(json.dumps(cell))
            box = {"name": "wooden-chest", "position": chest, "inventory": {"transport-belt": 9}}
            obs = {"world_id": "w", "inventory": {"transport-belt": 4}, "entities": [box]}
            self.assertEqual(pcf.feed_belt_receipt(tmp, obs), {"needed": 12, "stocked": 13, "chest": chest})
            builder["power_mode"] = "belt_fed"
            (Path(tmp) / "factory-builder.json").write_text(json.dumps(builder))
            self.assertIsNone(pcf.feed_belt_receipt(tmp, obs))


class CoalFeedSiteTests(unittest.TestCase):
    def test_fringe_cells_survive_the_candidate_cap(self):
        # An interior run of rich sites first (richest-first order), the fringe column last.
        interior = [{"x": x, "y": y} for y in range(10) for x in range(30)]
        fringe = [{"x": 30, "y": y} for y in range(10)]
        cells = fringe_coal_cells(compact_coal_cell, interior + fringe)
        drills = [c["drill"]["position"] for c in cells]
        self.assertEqual(drills[-10:], fringe)
        self.assertNotIn({"x": 0, "y": 0}, drills)  # its arm stands on the site (2, 1)
        self.assertLess(len(cells), 256)

    def test_starter_keepouts_cover_reserved_areas_and_unbuilt_hardware(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = {"entities": [at("transport-belt", 10.5, 4.5), at("burner-mining-drill", 4, 4)]}
            feed["reserved_area"] = [[3, 3], [5, 5]]
            board = {"world_id": "w", "blocks": {"power_coal_feed": feed}}
            (Path(tmp) / "pcb-board.json").write_text(json.dumps(board))
            built = {"name": "transport-belt", "position": {"x": 10.5, "y": 4.5}, "unit_number": 3}
            keepouts = starter_keepouts(tmp, {"world_id": "w", "entities": []})
            self.assertEqual(keepouts["areas"], [[3, 3, 5, 5]])
            self.assertIn("10,4", keepouts["tiles"])
            self.assertNotIn("10,4", starter_keepouts(tmp, {"world_id": "w", "entities": [built]})["tiles"])
            self.assertEqual(starter_keepouts(tmp, {"world_id": "x"}), {"areas": [], "tiles": []})

    @unittest.skipIf(LuaRuntime is None, "lupa not installed")
    def test_keepout_lua_rejects_overlapping_boxes(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        keepouts = {"areas": [[3, 3, 5, 5]], "tiles": ["10,4"]}
        body = "local keepouts=" + lua_table(keepouts) + KEEPOUT_LUA
        hit = lua.execute(body + "return function(l,t,r,b) return keepout_hit(l,t,r,b) end")
        self.assertTrue(hit(4, 4, 6, 6))  # inside the reserved drill footprint
        self.assertFalse(hit(5, 3, 7, 5))  # touches its edge only
        self.assertTrue(hit(9, 3, 11, 5))  # covers the planned belt tile (10, 4)
        self.assertFalse(hit(11, 3, 13, 5))


def lua_table(keepouts):
    areas = ",".join("{" + ",".join(str(v) for v in a) + "}" for a in keepouts["areas"])
    tiles = ",".join(json.dumps(t) for t in keepouts["tiles"])
    return "{areas={" + areas + "},tiles={" + tiles + "}}"


class AuditTests(unittest.TestCase):
    def test_the_build_chain_starts_early_and_every_declaration_is_complete(self):
        for capability in ("mining_outpost", "smelting_district", "planned_main_bus"):
            early = es.spec(CAPABILITIES, capability)
            self.assertIsNotNone(early, capability)
            self.assertIn(early["inputs"], es.INPUT_CHECKS)
        for name in CAPABILITIES:
            early = es.spec(CAPABILITIES, name)
            if early is not None:
                self.assertIn(early.get("inputs"), es.INPUT_CHECKS, name)
                self.assertTrue(set(early.get("requires", [])) <= set(CAPABILITIES), name)


if __name__ == "__main__":
    unittest.main()
