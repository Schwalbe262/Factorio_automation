"""Default roadmap data and the pure autopilot that proposes its next steps."""

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from factorio_agent import progression as pg
from factorio_agent.jobs import CAPABILITIES, Request


def job(step_id, status="succeeded"):
    step = {s.id: s for s in pg.ROADMAP}[step_id]
    return {"capability": step.capability, "parameters": dict(step.parameters), "status": status}


def done_through(step_id):
    """Succeeded jobs for every roadmap step up to and including ``step_id``."""
    rows = []
    for step in pg.ROADMAP:
        rows.append(job(step.id))
        if step.id == step_id:
            return rows
    raise AssertionError(step_id)


def ids(nodes):
    return [node["id"] for node in nodes]


class RoadmapConsistencyTests(unittest.TestCase):
    def test_ids_are_unique_and_after_refers_to_earlier_steps(self):
        seen = set()
        for step in pg.ROADMAP:
            self.assertNotIn(step.id, seen)
            self.assertTrue(set(step.after) <= seen, step.id)
            seen.add(step.id)

    def test_every_step_validates_as_a_request(self):
        for step in pg.ROADMAP:
            Request("k", "world", 0, step.capability, dict(step.parameters)).validate()

    def test_every_requires_is_produced_by_an_earlier_step(self):
        produced = set()
        for step in pg.ROADMAP:
            missing = set(CAPABILITIES[step.capability]["requires"]) - produced
            self.assertFalse(missing, step.id)
            produced.add(step.capability)

    def test_node_ids_fit_the_queue_id_rule(self):
        for step in pg.ROADMAP:
            self.assertRegex(pg.NODE_PREFIX + step.id, re.compile(r"^[A-Za-z0-9_-]{1,64}$"))

    def test_priorities_stay_inside_the_queue_range(self):
        lowest = pg.TOP_PRIORITY - len(pg.ROADMAP)
        self.assertGreaterEqual(lowest, -10000)
        self.assertLessEqual(pg.TOP_PRIORITY, 10000)

    def test_roadmap_ends_with_chemical_science(self):
        self.assertEqual(pg.ROADMAP[0].capability, "starter")
        self.assertEqual(pg.ROADMAP[-1].capability, "chemical_science")


class AutopilotTests(unittest.TestCase):
    def propose(self, jobs=(), queue=(), observation=None, limit=3):
        return pg.autopilot_nodes(list(jobs), list(queue), observation or {}, CAPABILITIES, limit=limit)

    def test_fresh_map_starts_with_starter_only(self):
        nodes = self.propose()
        self.assertEqual(ids(nodes), ["auto_starter"])
        self.assertEqual(
            nodes[0],
            {
                "id": "auto_starter",
                "capability": "starter",
                "parameters": {},
                "priority": pg.TOP_PRIORITY,
                "depends_on": [],
                "reason": nodes[0]["reason"],
            },
        )

    def test_after_starter_earliest_ready_steps_in_order_with_descending_priority(self):
        self.assertEqual(ids(self.propose([job("starter")])), ["auto_power"])
        nodes = self.propose(done_through("power"))
        self.assertEqual(
            ids(nodes), ["auto_starter_research_automation", "auto_mining_outpost_iron", "auto_mining_outpost_copper"]
        )
        self.assertGreater(nodes[0]["priority"], nodes[1]["priority"])
        self.assertGreater(nodes[1]["priority"], nodes[2]["priority"])

    def test_limit_caps_the_proposal(self):
        self.assertEqual(len(self.propose(done_through("power"), limit=1)), 1)
        self.assertEqual(self.propose(done_through("power"), limit=0), [])

    def test_queue_node_or_running_job_represents_a_step(self):
        jobs = done_through("power")
        queued = {
            "id": "luna_outpost",
            "capability": "mining_outpost",
            "parameters": {"resource": "iron-ore", "rate_per_minute": 60},
        }
        self.assertNotIn("auto_mining_outpost_iron", ids(self.propose(jobs, [queued])))
        running = job("starter_research_automation", status="running")
        self.assertNotIn("auto_starter_research_automation", ids(self.propose(jobs + [running])))
        paused = job("starter_research_automation", status="paused")
        self.assertNotIn("auto_starter_research_automation", ids(self.propose(jobs + [paused])))

    def test_existing_node_id_is_not_proposed_again(self):
        queued = {"id": "auto_power", "capability": "power", "parameters": {}}
        self.assertNotIn("auto_power", ids(self.propose([job("starter")], [queued])))

    def test_blocked_step_is_skipped_without_stopping_independent_steps(self):
        jobs = done_through("power") + [job("mining_outpost_iron", status="blocked")]
        nodes = self.propose(jobs)
        self.assertNotIn("auto_mining_outpost_iron", ids(nodes))
        self.assertEqual(ids(nodes)[0], "auto_starter_research_automation")

    def test_blocked_step_holds_back_steps_ordered_after_it(self):
        jobs = done_through("power") + [job("starter_research_automation", status="failed")]
        nodes = self.propose(jobs, limit=10)
        self.assertNotIn("auto_starter_research_electric_mining_drill", ids(nodes))
        self.assertNotIn("auto_bootstrap_logistics", ids(nodes))

    def test_researched_technology_satisfies_research_steps(self):
        jobs = done_through("power")
        observation = {"technologies": ["automation"]}
        nodes = self.propose(jobs, observation=observation, limit=10)
        self.assertNotIn("auto_starter_research_automation", ids(nodes))
        self.assertIn("auto_starter_research_electric_mining_drill", ids(nodes))
        self.assertIn("auto_bootstrap_logistics", ids(nodes))
        # The raw observation format (name -> researched) works the same way.
        nodes = self.propose(jobs, observation={"technologies": {"automation": True}}, limit=10)
        self.assertIn("auto_bootstrap_logistics", ids(nodes))

    def test_unresearched_mapping_entries_do_not_count(self):
        nodes = self.propose(done_through("power"), observation={"technologies": {"automation": False}}, limit=10)
        self.assertIn("auto_starter_research_automation", ids(nodes))

    def test_native_recipe_locks_hold_back_a_step(self):
        jobs = done_through("bootstrap_logistics")
        locked = {"enabled_recipes": ["transport-belt"]}
        self.assertFalse([n for n in self.propose(jobs, observation=locked) if n["capability"] == "mining_outpost"])
        unlocked = {"enabled_recipes": ["electric-mining-drill"]}
        self.assertEqual(
            ids(self.propose(jobs, observation=unlocked)),
            ["auto_mining_outpost_iron", "auto_mining_outpost_copper", "auto_mining_outpost_coal"],
        )
        gates = {"native_capability_gates": {"mining_outpost": {"native_recipes_ready": False}}}
        self.assertFalse([n for n in self.propose(jobs, observation=gates) if n["capability"] == "mining_outpost"])

    def test_requires_must_have_succeeded_jobs(self):
        # automation_science requires smelting_district; a queued node is not enough.
        jobs = done_through("mining_outpost_coal")
        queue = [{"id": "x", "capability": "smelting_district", "parameters": {}}]
        self.assertNotIn("auto_automation_science", ids(self.propose(jobs, queue, limit=10)))

    def test_the_bus_follows_the_smelting_district_and_red_science_the_bus(self):
        # paired_smelting / paired_mining migrate the old development map only.
        self.assertFalse({"paired_smelting", "paired_mining"} & {s.capability for s in pg.ROADMAP})
        nodes = self.propose(done_through("smelting_district"), limit=10)
        self.assertEqual(ids(nodes)[0], "auto_main_bus")
        self.assertNotIn("auto_automation_science", ids(nodes))
        nodes = self.propose(done_through("main_bus"), limit=10)
        self.assertEqual(ids(nodes)[0], "auto_automation_science")
        order = [s.id for s in pg.ROADMAP]
        start, stop = order.index("smelting_district"), order.index("logistic_science")
        self.assertEqual(
            order[start : stop + 1],
            [
                "smelting_district",
                "main_bus",
                "automation_science",
                "research_logistic_science_pack",
                "research_steel_processing",
                "research_fast_inserter",
                "logistic_science",
            ],
        )

    def test_gas_header_comes_before_the_plastic_cell(self):
        # gas_distribution refuses a gas outlet a chemical plan already owns; plastic first left
        # sulfur without gas on the GUI map. The header is a real input of the plastic cell.
        jobs = done_through("chemical_coal_supply")
        nodes = ids(self.propose(jobs, limit=10))
        self.assertIn("auto_gas_distribution", nodes)
        self.assertNotIn("auto_chemical_production_plastic", nodes)
        jobs.append(job("gas_distribution"))
        self.assertIn("auto_chemical_production_plastic", ids(self.propose(jobs, limit=10)))

    def test_first_steam_pair_needs_electric_coal_not_green_science(self):
        jobs = done_through("research_fast_inserter")  # electric_coal done, no logistic_science job
        self.assertNotIn("logistic_science", [j["capability"] for j in jobs])
        self.assertIn("auto_steam_expansion", ids(self.propose(jobs, limit=10)))
        without = [j for j in jobs if j["capability"] != "electric_coal"]
        self.assertNotIn("auto_steam_expansion", ids(self.propose(without, limit=10)))

    def test_research_step_waits_for_available_research_listing(self):
        jobs = done_through("automation_science")
        available = {"available_research": {"technologies": [{"technology": "steel-processing"}]}}
        nodes = self.propose(jobs, observation=available, limit=10)
        self.assertIn("auto_research_steel_processing", ids(nodes))
        self.assertNotIn("auto_research_logistic_science_pack", ids(nodes))

    def test_green_research_waits_for_the_green_block_which_is_proposed_first(self):
        # eval-4177156614: three green-pack research steps filled the autopilot batch and the
        # research lane while no logistic science block existed.
        jobs = done_through("research_fast_inserter")
        red, green = (
            [{"name": "automation-science-pack", "amount": 1}],
            [{"name": "automation-science-pack", "amount": 1}, {"name": "logistic-science-pack", "amount": 1}],
        )
        listed = [
            {"technology": "gun-turret", "science_ingredients": red},
            {"technology": "advanced-material-processing", "science_ingredients": green},
            {"technology": "automation-2", "science_ingredients": green},
            {"technology": "engine", "science_ingredients": green},
        ]
        observation = {"available_research": {"technologies": listed}}
        nodes = ids(self.propose(jobs, observation=observation, limit=10))
        self.assertIn("auto_logistic_science", nodes)
        self.assertIn("auto_research_gun_turret", nodes)
        for step in ("advanced_material_processing", "automation_2", "engine"):
            self.assertNotIn("auto_research_" + step, nodes)
        jobs.append(job("logistic_science"))
        nodes = ids(self.propose(jobs, observation=observation, limit=10))
        self.assertIn("auto_research_advanced_material_processing", nodes)

    def test_live_stall_shape_still_proposes_green_steel_with_research_nodes_queued(self):
        # eval-4177156614 at 100 game min: the green research job runs (waiting for packs), the
        # automation-2 and engine nodes are ready behind it; logistic science and steel are due.
        jobs = [j for j in done_through("dedicated_materials_stone") if j["capability"] != "logistic_science"]
        jobs = [j for j in jobs if j["capability"] not in ("steam_expansion", "defense_perimeter")]
        jobs += [job("dedicated_materials_coal"), job("steel_supply"), job("brick_production", "blocked")]
        jobs.append(dict(job("research_advanced_material_processing"), status="waiting"))
        queue = [
            {"id": "auto_research_" + s, "capability": "research", "parameters": {"technology": t}, "status": "ready"}
            for s, t in (("automation_2", "automation-2"), ("engine", "engine"))
        ]
        green = [{"name": "automation-science-pack", "amount": 1}, {"name": "logistic-science-pack", "amount": 1}]
        listed = [
            {"technology": t, "science_ingredients": green}
            for t in ("advanced-material-processing", "automation-2", "engine", "electric-energy-distribution-1")
        ]
        observation = {"available_research": {"technologies": listed}, "tick": 400000}
        nodes = ids(self.propose(jobs, queue, observation=observation, limit=3))
        # The first steam pair no longer waits for green science (it needs only electric_coal),
        # so it is proposed beside them in roadmap order.
        self.assertEqual(nodes[:3], ["auto_logistic_science", "auto_steam_expansion", "auto_steel_production"])
        self.assertNotIn("auto_research_electric_energy_distribution_1", nodes)

    def test_completed_roadmap_proposes_nothing(self):
        # A finished roadmap still retires the starter cells its permanent sources replaced, once each.
        jobs = done_through("chemical_science")
        self.assertEqual(ids(self.propose(jobs)), ["auto_starter_retirement_iron-ore"])
        retired = [
            {"capability": "starter_retirement", "parameters": {"resource": r}, "status": "succeeded"}
            for r in ("iron-ore", "copper-ore", "stone", "coal")
        ]
        self.assertEqual(self.propose(jobs + retired), [])

    def test_depends_on_only_references_known_ids(self):
        jobs = done_through("main_bus")
        queue = [{"id": "q1", "capability": "power", "parameters": {}}]
        nodes = self.propose(jobs, queue, limit=10)
        known = {"q1"} | set(ids(nodes))
        for node in nodes:
            self.assertTrue(set(node["depends_on"]) <= known)

    def test_input_is_not_mutated(self):
        jobs = done_through("power")
        snapshot = [dict(row) for row in jobs]
        self.propose(jobs)
        self.assertEqual(jobs, snapshot)


def iron_short(shortfall=133):
    """Receipt with the live numbers: one iron cell, 133 plates short, net +0.6/min."""
    evidence = dict(
        shortfall=shortfall,
        produced_per_minute=15.1,
        consumed_per_minute=14.5,
        net_per_minute=0.6,
        cell_capacity_per_minute=15.0,
        productive_cells=1,
        installed_live_cells=1,
        raw_cost_per_cell={"iron-plate": 9, "stone": 10},
        available=0,
    )
    stone = dict(produced_per_minute=6.0, consumed_per_minute=0.0, net_per_minute=6.0, available=30)
    return {
        "resource_supply_options": {
            "iron-plate": {"action": "wait", "evidence": evidence},
            "stone": {"action": "wait", "evidence": stone},
        }
    }


class CapacityAutopilotTests(unittest.TestCase):
    def propose(self, jobs=(), queue=(), observation=None, limit=3):
        return pg.autopilot_nodes(list(jobs), list(queue), observation or {}, CAPABILITIES, limit=limit)

    def test_capacity_node_comes_first_with_higher_priority(self):
        nodes = self.propose(done_through("power"), observation=iron_short())
        self.assertEqual(ids(nodes)[0], "auto_capacity_iron-ore_3")
        self.assertEqual(nodes[0]["capability"], "starter_capacity")
        self.assertEqual(nodes[0]["parameters"], {"resource": "iron-ore", "cells": 3})
        self.assertEqual(nodes[0]["priority"], pg.CAPACITY_PRIORITY)
        self.assertTrue(all(nodes[0]["priority"] > node["priority"] for node in nodes[1:]))
        self.assertEqual(len(nodes), 3)
        self.assertIn("T(0)=", nodes[0]["reason"])
        Request("k", "world", 0, "starter_capacity", nodes[0]["parameters"]).validate()
        self.assertRegex(nodes[0]["id"], re.compile(r"^[A-Za-z0-9_-]{1,64}$"))

    def test_capacity_counts_toward_the_limit(self):
        self.assertEqual(
            ids(self.propose(done_through("power"), observation=iron_short(), limit=1)), ["auto_capacity_iron-ore_3"]
        )

    def test_no_capacity_before_starter_or_without_need(self):
        self.assertEqual(ids(self.propose(observation=iron_short())), ["auto_starter"])
        self.assertNotIn("starter_capacity", [n["capability"] for n in self.propose([job("starter")])])
        nodes = self.propose([job("starter")], observation=iron_short(shortfall=0))
        self.assertNotIn("starter_capacity", [n["capability"] for n in nodes])

    def test_open_capacity_node_or_job_for_the_resource_blocks_a_new_one(self):
        jobs = done_through("power")
        queued = {
            "id": "luna_capacity",
            "capability": "starter_capacity",
            "parameters": {"resource": "iron-ore", "cells": 2},
            "status": "ready",
        }
        nodes = self.propose(jobs, [queued], observation=iron_short())
        self.assertNotIn("starter_capacity", [n["capability"] for n in nodes])
        running = {"capability": "starter_capacity", "parameters": {"resource": "iron-ore", "cells": 2}}
        nodes = self.propose(jobs + [dict(running, status="running")], observation=iron_short())
        self.assertNotIn("starter_capacity", [n["capability"] for n in nodes])
        # A finished smaller target does not block; a failed identical target does.
        nodes = self.propose(jobs + [dict(running, status="succeeded")], observation=iron_short())
        self.assertIn("auto_capacity_iron-ore_3", ids(nodes))
        failed = {"capability": "starter_capacity", "parameters": {"resource": "iron-ore", "cells": 3}}
        nodes = self.propose(jobs + [dict(failed, status="failed")], observation=iron_short())
        self.assertNotIn("starter_capacity", [n["capability"] for n in nodes])

    @patch("factorio_agent.capacity_planning.PLATE_CELL_POLICY", "cap")
    def test_succeeded_mining_outpost_ends_starter_capacity_for_that_resource(self):
        jobs = done_through("power") + [job("mining_outpost_iron")]
        nodes = self.propose(jobs, observation=iron_short())
        self.assertNotIn("starter_capacity", [n["capability"] for n in nodes])

    def test_demand_policy_plate_cells_end_at_retirement_not_at_the_outpost(self):
        """Default demand policy: the outpost's ore waits for the district; district + bus retire the cells."""
        jobs = done_through("power") + [job("mining_outpost_iron")]
        observation = dict(iron_short(), technologies={"electric-mining-drill": True}, research="logistics")
        nodes = self.propose(jobs, observation=observation)
        self.assertIn("auto_capacity_iron-ore_3", ids(nodes))
        retired = jobs + [job("smelting_district"), job("main_bus")]
        nodes = self.propose(retired, observation=observation)
        self.assertNotIn("starter_capacity", [n["capability"] for n in nodes])

    def test_demand_policy_reads_the_retirement_bill_and_its_gate(self):
        six = iron_short(shortfall=0)
        six["resource_supply_options"]["iron-plate"]["evidence"].update(
            produced_per_minute=90.0, productive_cells=6, installed_live_cells=6
        )
        six["roadmap_material_demand"] = {"iron-plate": 300}  # the 8-step lookahead misses the bus
        self.assertEqual(self.capacity(six), [])
        six["retirement_demand"] = {"items": {"iron-plate": 1500}, "gate_seconds": 200}
        self.assertEqual([n["parameters"] for n in self.capacity(six)], [{"resource": "iron-ore", "cells": 8}])
        six["retirement_demand"]["gate_seconds"] = 2000
        self.assertEqual(self.capacity(six), [])

    def test_demand_policy_proposes_no_plate_cell_once_the_district_builds(self):
        """Critical path first: the district job's plates and actor time are not spent on burner cells."""
        six = iron_short(shortfall=0)
        six["resource_supply_options"]["iron-plate"]["evidence"].update(
            produced_per_minute=90.0, productive_cells=6, installed_live_cells=6
        )
        six["retirement_demand"] = {"items": {"iron-plate": 1500}, "gate_seconds": 200}
        before = done_through("mining_outpost_coal")
        # The district is proposed in the same pass and waits only for a job slot: no plate cell
        # beside it (district_waits_for_slot, tests/test_v2_district_slot_capacity.py).
        self.assertEqual(self.capacity(six, before), [])
        for status in ("running", "waiting", "succeeded"):
            self.assertEqual(self.capacity(six, before + [job("smelting_district", status=status)]), [], status)
        # A blocked district (no site yet) does not stop the cells.
        self.assertEqual(len(self.capacity(six, before + [job("smelting_district", status="blocked")])), 1)
        # Before the district starts, its small remaining bill ends the payback: no cell.
        six["retirement_demand"]["district"] = {"items": {"iron-plate": 40}, "gate_seconds": 0}
        self.assertEqual(self.capacity(six, before), [])

    def capacity(self, observation, jobs=None):
        return [
            n
            for n in self.propose(jobs or done_through("power"), observation=observation)
            if n["capability"] == "starter_capacity"
        ]

    @patch("factorio_agent.capacity_planning.PLATE_CELL_POLICY", "cap")
    def test_after_the_drill_unlock_plate_cells_stop_at_what_the_capability_accepts(self):
        # pace1: 9 nodes for 3-6 plate cells were proposed around the unlock and blocked
        # (bulk_starter_capacity_requires_electric_mining_after_unlock).
        unlocked = dict(iron_short(), technologies={"electric-mining-drill": True})
        self.assertEqual([n["parameters"] for n in self.capacity(unlocked)], [{"resource": "iron-ore", "cells": 2}])
        unlocked["resource_supply_options"]["iron-plate"]["evidence"].update(productive_cells=2, installed_live_cells=2)
        self.assertEqual(self.capacity(unlocked), [])

    @patch("factorio_agent.capacity_planning.PLATE_CELL_POLICY", "cap")
    def test_no_plate_cell_while_the_drill_research_runs(self):
        self.assertEqual(self.capacity(dict(iron_short(), research="electric-mining-drill")), [])
        self.assertEqual(len(self.capacity(dict(iron_short(), research="automation"))), 1)


class FakeCatalog:
    """Two recipes and one technology: gear = 2 iron, red pack = 1 copper + 1 gear."""

    technologies = {
        "automation": {"unit_count": 10, "ingredients": [{"name": "automation-science-pack", "amount": 1}]},
    }
    recipes = {
        "iron-gear-wheel": {
            "ingredients": [{"name": "iron-plate", "amount": 2, "type": "item"}],
            "products": [{"name": "iron-gear-wheel", "amount": 1}],
        },
        "automation-science-pack": {
            "ingredients": [
                {"name": "copper-plate", "amount": 1, "type": "item"},
                {"name": "iron-gear-wheel", "amount": 1, "type": "item"},
            ],
            "products": [{"name": "automation-science-pack", "amount": 1}],
        },
        "lab": {
            "ingredients": [{"name": "iron-gear-wheel", "amount": 10, "type": "item"}],
            "products": [{"name": "lab", "amount": 1}],
        },
        "pipe": {
            "ingredients": [{"name": "iron-plate", "amount": 1, "type": "item"}],
            "products": [{"name": "pipe", "amount": 1}],
        },
    }

    def recipe_for_product(self, item):
        return self.recipes.get(item)


REAL_CATALOG = Path("C:/Users/NEC/Documents/git/factorio_automation/runtime/v2/eval-2150100594/catalog.json")


class LookaheadDemandTests(unittest.TestCase):
    def test_research_step_is_unit_count_times_pack_cost(self):
        step = pg.Step("r", "starter_research", {"technology": "automation"})
        self.assertEqual(pg._research_items(FakeCatalog(), step), {"automation-science-pack": 10})
        self.assertEqual(
            pg.raw_cost(FakeCatalog(), pg._research_items(FakeCatalog(), step)),
            {"copper-plate": 10, "iron-plate": 20},
        )

    def test_lookahead_sums_research_and_builds(self):
        jobs = done_through("power")
        demand = pg.lookahead_demand(FakeCatalog(), jobs, {}, horizon=1)
        # automation research (10 x (1 copper + 2 iron)) + one lab (10 gears = 20 iron).
        self.assertEqual(demand, {"copper-plate": 10, "iron-plate": 40})

    def test_builds_expansion_keeps_raw_items_and_drops_unknown(self):
        self.assertEqual(
            pg.raw_cost(FakeCatalog(), {"pipe": 8, "coal": 16, "mystery": 3, "iron-gear-wheel": 2}),
            {"iron-plate": 12, "coal": 16},
        )

    def test_researched_technology_and_horizon(self):
        jobs = done_through("power")
        self.assertEqual(pg.lookahead_demand(FakeCatalog(), jobs, {}, horizon=0), {})
        demand = pg.lookahead_demand(FakeCatalog(), jobs, {"technologies": ["automation"]}, horizon=1)
        self.assertNotIn("copper-plate", demand)

    def test_retirement_demand_runs_to_the_bus_and_skips_in_flight_and_off_path_steps(self):
        class Catalog(FakeCatalog):
            technologies = {
                "automation": {
                    "unit_count": 10,
                    "unit_energy": 600,
                    "ingredients": FakeCatalog.technologies["automation"]["ingredients"],
                },
                "logistics": {
                    "unit_count": 20,
                    "unit_energy": 900,
                    "ingredients": FakeCatalog.technologies["automation"]["ingredients"],
                },
            }

        jobs = done_through("power")
        observation = {"research": "automation", "research_progress": 0.5, "entities": {"lab": 2}}
        demand = pg.retirement_demand(Catalog(), jobs, observation)
        self.assertEqual(demand["steps"][-1], "main_bus")  # beyond LOOKAHEAD_STEPS, up to the retirement step
        self.assertNotIn("power_coal_feed", demand["steps"])  # fixed priority: off the path
        self.assertNotIn("automation_science", demand["steps"])
        # Lab gate: half of automation (50 s) + logistics (300 s) on 2 labs.
        self.assertEqual(demand["gate_seconds"], 175.0)
        # Half the automation packs (5 x (1 copper + 2 iron)), one lab (20 iron), logistics packs (20 x 3).
        self.assertEqual(demand["items"]["copper-plate"], 5 + 20)
        self.assertEqual(demand["items"]["iron-plate"], 10 + 20 + 40)
        in_flight = jobs + [job("starter_research_automation", status="running")]
        self.assertEqual(pg.retirement_demand(Catalog(), in_flight, observation)["items"]["iron-plate"], 10 + 40)
        # The district part: the steps up to smelting_district, the payback horizon of a new plate cell.
        district = demand["district"]
        self.assertEqual(district["steps"][-1], "smelting_district")
        self.assertLessEqual(district["items"]["iron-plate"], demand["items"]["iron-plate"])
        self.assertLessEqual(district["gate_seconds"], demand["gate_seconds"])
        built = done_through("smelting_district")
        self.assertNotIn("district", pg.retirement_demand(Catalog(), built, observation))

    def test_steps_without_builds_add_nothing(self):
        # automation_science has builds (its labs and machines), so start after it
        self.assertEqual(pg.lookahead_demand(FakeCatalog(), done_through("automation_science"), {}, horizon=3), {})

    def test_plan_fixed_bills_of_steps_without_builds(self):
        steam = next(step for step in pg.ROADMAP if step.capability == "steam_expansion")
        bill = pg.step_bill(FakeCatalog(), steam)
        self.assertEqual((bill["boiler"], bill["steam-engine"]), (1, 2))
        self.assertEqual(bill["electric-mining-drill"], pg.MINING_OUTPOST_BUILDS["electric-mining-drill"])
        green = next(step for step in pg.ROADMAP if step.capability == "logistic_science")
        self.assertEqual(pg.step_bill(FakeCatalog(), green), {})  # no recipe tree: nothing guessed
        defense = next(step for step in pg.ROADMAP if step.capability == "defense_perimeter")
        self.assertEqual(pg.step_bill(FakeCatalog(), defense), {})  # threat-sized: not derived
        science = next(step for step in pg.ROADMAP if step.capability == "automation_science")
        self.assertIs(pg.step_bill(FakeCatalog(), science), science.builds)

    @unittest.skipUnless(REAL_CATALOG.exists(), "live catalog not present")
    def test_logistic_science_bill_comes_from_its_cluster_plan(self):
        from factorio_agent.core.world_catalog import WorldCatalog

        catalog = WorldCatalog.from_dict(json.loads(REAL_CATALOG.read_text(encoding="utf-8")))
        green = next(step for step in pg.ROADMAP if step.capability == "logistic_science")
        bill = pg.step_bill(catalog, green)
        self.assertGreaterEqual(bill.get("assembling-machine-1", 0), 2)
        self.assertGreater(bill.get("inserter", 0), 0)
        jobs = done_through("research_fast_inserter")
        demand = pg.lookahead_demand(catalog, jobs, {}, horizon=1)
        self.assertGreater(demand.get("iron-plate", 0), 0)

    @unittest.skipUnless(REAL_CATALOG.exists(), "live catalog not present")
    def test_real_catalog_is_sane(self):
        from factorio_agent.core.world_catalog import WorldCatalog

        catalog = WorldCatalog.from_dict(json.loads(REAL_CATALOG.read_text(encoding="utf-8")))
        research = pg.lookahead_demand(catalog, done_through("power"), {}, horizon=1)
        # 10 x (1 copper + 1 gear) + lab (10 circuits, 10 gears, 4 belts).
        self.assertEqual(research, {"copper-plate": 25, "iron-plate": 56})
        demand = pg.lookahead_demand(catalog, [], {})
        self.assertEqual(set(demand) - {"iron-plate", "copper-plate", "coal", "stone", "wood"}, set())
        self.assertGreater(demand["iron-plate"], 100)
        self.assertLess(demand["iron-plate"], 1000)


class WantedRecipeTests(unittest.TestCase):
    def test_wanted_recipes_follow_the_next_unsatisfied_steps(self):
        jobs = done_through("bootstrap_logistics")
        recipes = pg.roadmap_wanted_recipes(jobs, CAPABILITIES, horizon=4)
        self.assertEqual(recipes, ["electric-mining-drill", "underground-belt", "splitter"])

    def test_horizon_limits_the_lookahead(self):
        jobs = done_through("main_bus")
        self.assertEqual(
            pg.roadmap_wanted_recipes(jobs, CAPABILITIES, horizon=1),
            ["assembling-machine-1", "automation-science-pack", "lab"],
        )
        self.assertEqual(pg.roadmap_wanted_recipes(jobs, CAPABILITIES, horizon=0), [])

    def test_wanted_technologies_skip_researched_ones(self):
        jobs = done_through("automation_science")
        self.assertEqual(
            pg.roadmap_wanted_technologies(jobs, {"steel-processing": True}, horizon=3),
            ["logistic-science-pack", "fast-inserter"],
        )


if __name__ == "__main__":
    unittest.main()
