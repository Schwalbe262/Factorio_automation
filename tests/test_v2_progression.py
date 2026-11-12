"""Default roadmap data and the pure autopilot that proposes its next steps."""

import re
import unittest

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
        nodes = self.propose([job("starter")])
        self.assertEqual(ids(nodes), ["auto_starter_capacity_coal", "auto_power", "auto_starter_capacity_iron"])
        self.assertGreater(nodes[0]["priority"], nodes[1]["priority"])
        nodes = self.propose(done_through("power"))
        self.assertEqual(
            ids(nodes), ["auto_starter_capacity_iron", "auto_starter_research_automation", "auto_mining_outpost_iron"]
        )

    def test_limit_caps_the_proposal(self):
        self.assertEqual(len(self.propose(done_through("power"), limit=1)), 1)
        self.assertEqual(self.propose(done_through("power"), limit=0), [])

    def test_queue_node_or_running_job_represents_a_step(self):
        jobs = done_through("power")
        queued = {
            "id": "luna_capacity",
            "capability": "starter_capacity",
            "parameters": {"resource": "iron-ore", "cells": 2},
        }
        self.assertNotIn("auto_starter_capacity_iron", ids(self.propose(jobs, [queued])))
        running = job("starter_research_automation", status="running")
        self.assertNotIn("auto_starter_research_automation", ids(self.propose(jobs + [running])))
        paused = job("starter_research_automation", status="paused")
        self.assertNotIn("auto_starter_research_automation", ids(self.propose(jobs + [paused])))

    def test_existing_node_id_is_not_proposed_again(self):
        queued = {"id": "auto_power", "capability": "power", "parameters": {}}
        self.assertNotIn("auto_power", ids(self.propose([job("starter")], [queued])))

    def test_blocked_step_is_skipped_without_stopping_independent_steps(self):
        jobs = done_through("power") + [job("starter_capacity_iron", status="blocked")]
        nodes = self.propose(jobs)
        self.assertNotIn("auto_starter_capacity_iron", ids(nodes))
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
        # automation_science requires paired_smelting; a queued node is not enough.
        jobs = done_through("smelting_district")
        queue = [{"id": "x", "capability": "paired_smelting", "parameters": {}}]
        self.assertNotIn("auto_automation_science", ids(self.propose(jobs, queue, limit=10)))

    def test_research_step_waits_for_available_research_listing(self):
        jobs = done_through("automation_science")
        available = {"available_research": {"technologies": [{"technology": "steel-processing"}]}}
        nodes = self.propose(jobs, observation=available, limit=10)
        self.assertIn("auto_research_steel_processing", ids(nodes))
        self.assertNotIn("auto_research_logistic_science_pack", ids(nodes))

    def test_completed_roadmap_proposes_nothing(self):
        self.assertEqual(self.propose(done_through("chemical_science")), [])

    def test_depends_on_only_references_known_ids(self):
        jobs = done_through("main_bus_factory")
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


class WantedRecipeTests(unittest.TestCase):
    def test_wanted_recipes_follow_the_next_unsatisfied_steps(self):
        jobs = done_through("bootstrap_logistics")
        recipes = pg.roadmap_wanted_recipes(jobs, CAPABILITIES, horizon=4)
        self.assertEqual(recipes, ["electric-mining-drill", "underground-belt", "splitter"])

    def test_horizon_limits_the_lookahead(self):
        jobs = done_through("paired_mining")
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
