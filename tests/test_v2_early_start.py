"""Early start: a block is planned and partly built once its inputs are planned (early_start.py)."""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from factorio_agent import early_start as es
from factorio_agent import progression as pg
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.jobs import CAPABILITIES, MAX_ACTIVE_JOBS, Ledger, Request, run_slice, submit
from factorio_agent.smelting import SmeltingDistrict, district_sources

INPUTS = {"known": True, "reason": "every source bank has a saved, published output port"}


def job(step_id, status="succeeded"):
    step = {s.id: s for s in pg.ROADMAP}[step_id]
    return {"capability": step.capability, "parameters": dict(step.parameters), "status": status}


def outposts_in_flight():
    """Minute ~21 of a live map: outposts building, logistics researching."""
    rows = [job(s) for s in ("starter", "power", "starter_research_automation")]
    rows += [job("starter_research_electric_mining_drill"), job("electric_coal")]
    rows += [job("bootstrap_logistics", "running"), job("mining_outpost_iron"), job("mining_outpost_copper")]
    return rows + [job("mining_outpost_coal", "running")]


def ids(nodes):
    return [node["id"] for node in nodes]


class ProposalTests(unittest.TestCase):
    def propose(self, jobs, observation):
        return pg.autopilot_nodes(jobs, [], observation, CAPABILITIES, limit=10)

    def test_district_is_proposed_early_once_the_banks_have_ports(self):
        nodes = self.propose(outposts_in_flight(), {"early_start_inputs": {"smelting_district": INPUTS}})
        node = next(n for n in nodes if n["id"] == "auto_smelting_district")
        self.assertIn("early start", node["reason"])
        self.assertIn("waits for research logistics", node["reason"])
        # Lower priority than every step it runs ahead of: it only uses their idle actor time.
        ahead = {s.id: pg.TOP_PRIORITY - i for i, s in enumerate(pg.ROADMAP)}
        for step in ("bootstrap_logistics", "mining_outpost_iron", "mining_outpost_copper", "mining_outpost_coal"):
            self.assertLess(node["priority"], ahead[step])

    def test_not_before_the_inputs_are_known(self):
        unknown = {"early_start_inputs": {"smelting_district": {"known": False, "reason": "x"}}}
        self.assertNotIn("auto_smelting_district", ids(self.propose(outposts_in_flight(), unknown)))
        self.assertNotIn("auto_smelting_district", ids(self.propose(outposts_in_flight(), {})))

    def test_not_while_a_prerequisite_step_is_not_in_flight(self):
        jobs = [j for j in outposts_in_flight() if j["parameters"].get("resource") != "coal"]
        observation = {"early_start_inputs": {"smelting_district": INPUTS}}
        self.assertNotIn("auto_smelting_district", ids(self.propose(jobs, observation)))
        blocked = jobs + [job("mining_outpost_coal", "blocked")]
        self.assertNotIn("auto_smelting_district", ids(self.propose(blocked, observation)))

    def test_not_when_the_locking_research_is_not_coming(self):
        jobs = [j for j in outposts_in_flight() if j["capability"] != "bootstrap_logistics"]
        observation = {"early_start_inputs": {"smelting_district": INPUTS}}
        # The roadmap's next research step counts as coming (rule 7) unless it stalled.
        self.assertIn("auto_smelting_district", ids(self.propose(jobs, observation)))
        jobs = jobs + [job("bootstrap_logistics", "blocked")]
        self.assertNotIn("auto_smelting_district", ids(self.propose(jobs, observation)))
        # Logistics researching natively counts as coming.
        nodes = self.propose(jobs, dict(observation, research="logistics"))
        self.assertIn("auto_smelting_district", ids(nodes))

    def test_a_native_gate_on_another_technology_holds_it_back(self):
        jobs = outposts_in_flight()
        gate = {"native_recipes_ready": False, "recipe_unlockers": {"splitter": ["logistics"]}}
        observation = {"early_start_inputs": {"smelting_district": INPUTS}}
        observation["native_capability_gates"] = {"smelting_district": gate}
        self.assertIn("auto_smelting_district", ids(self.propose(jobs, observation)))
        gate["recipe_unlockers"] = {"splitter": ["logistics-2"]}
        self.assertNotIn("auto_smelting_district", ids(self.propose(jobs, observation)))

    def test_bus_starts_early_beside_a_running_district(self):
        jobs = [job(s.id) for s in pg.ROADMAP[: [s.id for s in pg.ROADMAP].index("smelting_district")]]
        observation = {"technologies": ["logistics"]}
        observation["early_start_inputs"] = {"planned_main_bus": {"known": True, "reason": "anchors built"}}
        self.assertNotIn("auto_main_bus", ids(self.propose(jobs, observation)))  # no district job
        running = jobs + [job("smelting_district", "running")]
        node = next(n for n in self.propose(running, observation) if n["id"] == "auto_main_bus")
        self.assertLess(node["priority"], pg.TOP_PRIORITY - [s.id for s in pg.ROADMAP].index("smelting_district"))

    def test_roadmap_without_early_inputs_is_unchanged(self):
        observation = {"technologies": ["logistics"]}
        jobs = outposts_in_flight()[:-1] + [job("mining_outpost_coal")]
        jobs = [j for j in jobs if j["capability"] != "bootstrap_logistics"] + [job("bootstrap_logistics")]
        self.assertIn("auto_smelting_district", ids(self.propose(jobs, observation)))
        node = next(n for n in self.propose(jobs, observation) if n["id"] == "auto_smelting_district")
        self.assertNotIn("early start", node["reason"])


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
    def __init__(self, result, proof=None):
        self.result, self.proof_value = result, proof

    def step(self, capability, obs, parameters=None):
        return dict(self.result)

    def proof(self, capability, obs, result, parameters=None):
        return self.proof_value


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = Game()
        for capability in ("starter", "power"):
            self.submit(capability, capability)
            done = run_slice(self.root, self.game, Executor({"status": "succeeded"}, {"ok": True}), capability)
            self.assertEqual(done["status"], "succeeded")

    def submit(self, key, capability, parameters=None):
        request = Request(key, "world", self.game.observation["tick"], capability, parameters or {})
        return submit(self.root, self.game, request)

    def test_admitted_on_its_early_requires(self):
        self.assertEqual(self.submit("district", "smelting_district")["status"], "pending")
        with self.assertRaisesRegex(ValueError, "prerequisites"):
            self.submit("science", "automation_science", {"rate_per_minute": 30})

    def test_an_early_job_never_takes_the_last_free_slot(self):
        for index, capability in enumerate(("bootstrap_logistics", "power_coal_feed", "regularize_power_layout")):
            self.submit(f"j{index}", capability)
        self.assertEqual(MAX_ACTIVE_JOBS - es.EARLY_FREE_SLOTS, 3)
        with self.assertRaisesRegex(ValueError, "jobs may be active"):
            self.submit("district", "smelting_district")

    def test_deadline_and_stall_rules_spare_a_job_waiting_for_research(self):
        self.submit("district", "smelting_district")
        waiting = Executor({"status": "waiting", "reason": "waiting for research logistics to unlock splitter"})
        run_slice(self.root, self.game, waiting, "district", seconds=1)
        for progress in (0.2, 0.4, 0.6):  # research advances: the progress key changes
            self.game.observation["tick"] += 17000
            self.game.observation["research_progress"] = progress
            result = run_slice(self.root, self.game, waiting, "district", seconds=1)
        self.assertEqual(result["status"], "waiting", result.get("reason"))
        self.game.observation["tick"] += 60000
        self.game.observation["research_progress"] = 0.8
        job = Ledger(self.root).data["jobs"]["district"]
        self.assertGreater(self.game.observation["tick"] - job["created_tick"], 108000)
        result = run_slice(self.root, self.game, waiting, "district", seconds=1)
        self.assertEqual(result["status"], "waiting", result.get("reason"))
        self.assertGreater(Ledger(self.root).data["jobs"]["district"]["early_ticks"], 108000)

    def test_early_clock_stops_once_the_prerequisites_succeeded(self):
        row = {}
        es.track_early_time(row, 10, 1.0, True)
        es.track_early_time(row, 110, 3.0, True)
        es.track_early_time(row, 500, 9.0, False)
        es.track_early_time(row, 900, 19.0, False)
        self.assertEqual((row["early_ticks"], row["early_seconds"]), (100, 2.0))
        self.assertNotIn("early_mark", row)

    def test_is_early_only_for_declared_capabilities(self):
        self.assertTrue(es.is_early(CAPABILITIES, "smelting_district", {"power"}))
        done = {"power", "bootstrap_logistics", "mining_outpost"}
        self.assertFalse(es.is_early(CAPABILITIES, "smelting_district", done))
        self.assertFalse(es.is_early(CAPABILITIES, "automation_science", set()))
        self.assertEqual(es.admission_requires(CAPABILITIES, "automation_science"), ["smelting_district"])


def recipe(name, ingredients, category="crafting"):
    return {
        "name": name,
        "ingredients": [{"type": "item", "name": n, "amount": a} for n, a in ingredients.items()],
        "products": [{"type": "item", "name": name, "amount": 1}],
    }


class Catalog:
    fingerprint = "c"
    entities = {
        "stone-furnace": {"type": "furnace"},
        "splitter": {"type": "splitter"},
        "transport-belt": {"type": "transport-belt"},
        "inserter": {"type": "inserter"},
    }
    recipes = {
        "stone-furnace": recipe("stone-furnace", {"stone": 5}),
        "iron-gear-wheel": recipe("iron-gear-wheel", {"iron-plate": 2}),
        "transport-belt": recipe("transport-belt", {"iron-gear-wheel": 1, "iron-plate": 1}),
        "splitter": recipe("splitter", {"transport-belt": 4, "iron-plate": 5}),
        "inserter": recipe("inserter", {"iron-gear-wheel": 1, "iron-plate": 1}),
    }
    technologies = {"logistics": {"unlocks": ["splitter", "underground-belt"]}}

    def recipe_for_product(self, item):
        return self.recipes.get(item)


class Bootstrap:
    def __init__(self):
        self.calls = []

    def ensure_item(self, observation, item, count):
        self.calls.append(item)
        if item == "splitter":
            return {"status": "blocked", "reason": "required recipe is locked"}
        return {"type": "craft", "recipe": item, "count": count}


class BuilderGame:
    backend = "character"

    def __init__(self, root):
        self.cfg = SimpleNamespace(runtime_dir=str(root))

    def query(self, body):
        return {"ok": True}


def entity(name, x):
    return {"name": name, "position": {"x": x, "y": 0.5}, "direction": 0}


class PartialBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bootstrap = Bootstrap()
        self.builder = FactoryBuilder(BuilderGame(self.temp.name), self.bootstrap, Catalog())
        self.plan = {
            "ok": True,
            "entities": [entity("splitter", 0.5), entity("stone-furnace", 3), entity("transport-belt", 6.5)],
        }

    def obs(self, inventory, entities=()):
        return {
            "world_id": "w",
            "tick": 1,
            "entities": list(entities),
            "inventory": inventory,
            "enabled_recipes": {"stone-furnace": True, "transport-belt": True, "splitter": False},
        }

    def defer(self, obs, held=()):
        return es.Deferral(Catalog(), obs, coming={"logistics"}, held=held)

    def test_a_locked_splitter_waits_and_the_rest_is_built(self):
        obs = self.obs({"stone-furnace": 1})
        action = self.builder.ensure_plan(obs, self.plan, defer=self.defer(obs))
        self.assertEqual((action["type"], action["name"]), ("build", "stone-furnace"))
        self.assertNotIn("splitter", self.bootstrap.calls)

    def test_items_are_crafted_for_later_entities(self):
        obs = self.obs({})
        action = self.builder.ensure_plan(obs, self.plan, defer=self.defer(obs))
        self.assertEqual(action, {"type": "craft", "recipe": "stone-furnace", "count": 1})

    def test_everything_else_built_reports_waiting_naming_the_research(self):
        built = [dict(entity("stone-furnace", 3), unit_number=1), dict(entity("transport-belt", 6.5), unit_number=2)]
        obs = self.obs({}, built)
        result = self.builder.ensure_plan(obs, self.plan, defer=self.defer(obs))
        self.assertEqual(result["status"], "waiting")
        self.assertIn("research logistics", result["reason"])
        self.assertEqual(result["evidence"]["deferred"], ["splitter"])

    def test_without_deferral_the_lock_still_blocks(self):
        result = self.builder.ensure_plan(self.obs({}), self.plan)
        self.assertEqual(result["status"], "blocked")

    def test_a_contested_plate_is_never_crafted_but_stone_furnaces_are(self):
        obs = self.obs({})
        plan = {"ok": True, "entities": [entity("transport-belt", 6.5), entity("stone-furnace", 3)]}
        action = self.builder.ensure_plan(obs, plan, defer=self.defer(obs, held={"iron-plate"}))
        self.assertEqual(action["recipe"], "stone-furnace")
        built = [dict(entity("stone-furnace", 3), unit_number=1)]
        result = self.builder.ensure_plan(self.obs({}, built), plan, defer=self.defer(obs, held={"iron-plate"}))
        self.assertEqual(result["status"], "waiting")
        self.assertIn("iron-plate kept for jobs waiting for it", result["reason"])

    def test_inserters_keep_plan_order_behind_a_deferred_entity(self):
        obs = self.obs({"inserter": 1})
        plan = {"ok": True, "entities": [entity("splitter", 0.5), entity("inserter", 9)]}
        result = self.builder.ensure_plan(obs, plan, defer=self.defer(obs))
        self.assertEqual(result["status"], "waiting")
        self.assertIn("inserter", result["evidence"]["deferred"])


class ContestedTests(unittest.TestCase):
    def test_contested_plates_exclude_the_asking_capability(self):
        jobs = [
            {
                "capability": "mining_outpost",
                "status": "waiting",
                "reason": "waiting for drill-fed iron-plate production",
            },
            {"capability": "smelting_district", "status": "waiting", "reason": "waiting for drill-fed copper-plate"},
            {"capability": "power", "status": "succeeded", "reason": "waiting coal"},
        ]
        self.assertEqual(es.contested_plates(jobs, exclude=("smelting_district",)), {"iron-plate"})

    def test_deferral_holds_plates_only_while_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = {
                "world_id": "w",
                "jobs": {
                    "a": {"capability": "power", "status": "succeeded"},
                    "b": {"capability": "mining_outpost", "status": "waiting", "reason": "waiting for iron-plate"},
                    "c": {"capability": "smelting_district", "status": "running"},
                },
            }
            (Path(tmp) / "jobs.json").write_text(json.dumps(ledger))
            obs = {"world_id": "w", "technologies": {}}
            early = es.deferral(tmp, obs, Catalog(), "smelting_district")
            self.assertEqual(early.held, {"iron-plate"})
            for name in ("bootstrap_logistics", "mining_outpost"):
                ledger["jobs"][name] = {"capability": name, "status": "succeeded"}
            (Path(tmp) / "jobs.json").write_text(json.dumps(ledger))
            self.assertIsNone(
                es.deferral(tmp, {"world_id": "w", "technologies": {"logistics": True}}, Catalog(), "smelting_district")
            )
            self.assertTrue(es.capability_pending(tmp, "w", "smelting_district"))
            self.assertFalse(es.capability_pending(tmp, "w", "power"))


def bank(resource, x, *, ready, rate=60):
    port = {"item": resource, "position": {"x": x, "y": 0.5}, "direction": "output", "facing": 0}
    return {
        "ok": True,
        "ready": ready,
        "nominal_rate_per_minute": rate,
        "plan": {"resource": resource, "ports": [port]},
    }


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, banks, published):
        mining = {"world_id": "w", "banks": banks}
        (self.root / "mining-outposts.json").write_text(json.dumps(mining))
        blocks = {f"{b['plan']['resource']}_mining": b["plan"] for b in banks if b["plan"]["resource"] in published}
        (self.root / "pcb-board.json").write_text(json.dumps({"world_id": "w", "blocks": blocks}))

    def test_saved_unbuilt_banks_are_district_sources(self):
        banks = [
            bank("iron-ore", 1.5, ready=True),
            bank("copper-ore", 5.5, ready=False),
            bank("coal", 9.5, ready=False),
        ]
        sources, reason = district_sources({"banks": banks})
        self.assertIsNone(reason)
        self.assertEqual(sorted(sources), ["coal", "copper-ore", "iron-ore"])
        self.assertEqual(district_sources({"banks": banks[:2]})[1], "one_ready_coal_bank_with_nominal_30_required")
        later = [bank("iron-ore", 20.5, ready=False), bank("iron-ore", 1.5, ready=True)]
        self.assertEqual(district_sources({"banks": later + banks[1:]})[0]["iron-ore"]["position"]["x"], 1.5)

    def test_inputs_known_only_when_every_bank_port_is_published(self):
        banks = [
            bank("iron-ore", 1.5, ready=True),
            bank("copper-ore", 5.5, ready=False),
            bank("coal", 9.5, ready=False),
        ]
        self.write(banks, published=("iron-ore", "copper-ore"))
        row = es.mining_bank_ports(self.root, {"world_id": "w"})
        self.assertFalse(row["known"])
        self.write(banks, published=("iron-ore", "copper-ore", "coal"))
        self.assertTrue(es.inputs_receipt(self.root, {"world_id": "w"}, CAPABILITIES)["smelting_district"]["known"])
        self.assertFalse(es.mining_bank_ports(self.root, {"world_id": "other"})["known"])


class InvalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.banks = [
            bank("iron-ore", 1.5, ready=True),
            bank("copper-ore", 5.5, ready=False),
            bank("coal", 9.5, ready=False),
        ]
        sources, _ = district_sources({"banks": self.banks})
        self.plan = {"ok": True, "entities": [entity("stone-furnace", 30)], "ports": []}
        state = {"world_id": "w", "catalog_fingerprint": "c", "plan": self.plan, "source_ports": sources}
        (self.root / "smelting-district.json").write_text(json.dumps(state))
        board = {"world_id": "w", "blocks": {"smelting_district": self.plan}}
        (self.root / "pcb-board.json").write_text(json.dumps(board))

    def district(self, banks):
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": "w", "banks": banks}))
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(self.root)))
        return SmeltingDistrict(game, None, None, Catalog())

    def test_unchanged_ports_keep_the_plan(self):
        self.assertIsNone(self.district(self.banks)._inputs_changed({"world_id": "w", "entities": []}))

    def test_changed_port_before_any_build_drops_the_plan_for_a_new_one(self):
        moved = self.banks[:2] + [bank("coal", 40.5, ready=False)]
        district = self.district(moved)
        self.assertEqual(district._inputs_changed({"world_id": "w", "entities": []}), {"replan": True})
        self.assertEqual(district.state, {})
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertNotIn("smelting_district", board["blocks"])

    def test_changed_port_after_a_partial_build_blocks(self):
        moved = self.banks[:2] + [bank("coal", 40.5, ready=False)]
        built = [dict(entity("stone-furnace", 30), unit_number=7)]
        result = self.district(moved)._inputs_changed({"world_id": "w", "entities": built})
        self.assertEqual(result, {"status": "blocked", "reason": "smelting_inputs_changed_after_partial_build"})

    def test_proof_still_needs_every_plan_entity(self):
        district = self.district(self.banks)
        district.state["automatic_feed_baseline"] = {"tick": 1}
        district.state["plan"] = {"entities": [entity("splitter", 0.5)], "underground_pairs": []}
        self.assertIsNone(district.proof({"world_id": "w", "entities": []}))


class StarterSecondIronCellTests(unittest.TestCase):
    def test_starter_builds_a_second_iron_cell_while_it_waits(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from factorio_agent.handlers.early import STARTER_IRON_CELLS, starter_step

        build = {"type": "build", "name": "burner-mining-drill"}
        ex = SimpleNamespace(
            catalog=None,
            builder=Mock(),
            starter_upkeep=Mock(return_value={"status": "waiting", "reason": "waiting for unlock"}),
            capacity=Mock(step=Mock(return_value=build)),
        )
        self.assertEqual(starter_step(ex, {"tick": 1}, {}), build)
        ex.capacity.step.assert_called_with({"tick": 1}, {"resource": "iron-ore", "cells": STARTER_IRON_CELLS})
        ex.capacity.step.return_value = {"status": "succeeded"}
        self.assertEqual(starter_step(ex, {"tick": 1}, {})["status"], "waiting")
        ex.starter_upkeep.return_value = {"type": "craft"}
        self.assertEqual(starter_step(ex, {"tick": 1}, {}), {"type": "craft"})


if __name__ == "__main__":
    unittest.main()
