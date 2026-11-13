"""Critical path of a roadmap and the slack allocation rule (critical_path.py), on synthetic roadmaps."""

import unittest

from factorio_agent.critical_path import (
    claims,
    critical_claims,
    critical_jobs,
    critical_path,
    slack_refusal,
    slack_view,
)
from factorio_agent.progression import Step

CAPS = {
    "a": {"requires": []},
    "b": {"requires": ["a"]},
    "c": {"requires": ["a"], "native_recipes": ["widget"]},
    "d": {"requires": ["c"]},
    "e": {"requires": []},
    "r": {"requires": []},
    "side": {"requires": []},
    "make": {"requires": []},
}
CATALOG = {
    "technologies": {
        "t1": {"prerequisites": [], "unlocks": ["widget"]},
        "t2": {"prerequisites": ["t1"], "unlocks": []},
    },
    "items": {name: {} for name in ("iron-plate", "copper-plate", "coal", "transport-belt", "widget")},
}
ROADMAP = (
    Step("a", "a"),
    Step("t1", "r", {"technology": "t1"}),
    Step("b", "b"),
    Step("side", "side", priority=-500),
    Step("c", "c"),
    Step("d", "d", after=("side",)),
    Step("t2", "r", {"technology": "t2"}),
    Step("e", "e"),
)


def done(capability, parameters=None, status="succeeded"):
    return {"capability": capability, "parameters": parameters or {}, "status": status}


def path(jobs, technologies=(), horizon=3, enabled=None):
    obs = {"technologies": {t: True for t in technologies}}
    if enabled is not None:
        obs["enabled_recipes"] = {r: True for r in enabled}
    return critical_path(jobs, obs, CAPS, roadmap=ROADMAP, catalog=CATALOG, horizon=horizon)


class CriticalPathTests(unittest.TestCase):
    def test_frontier_is_the_first_open_steps_in_roadmap_order(self):
        result = path([])
        self.assertEqual(result.frontier, ("a", "t1", "b"))
        self.assertEqual(result.ready, ("a", "t1"))

    def test_satisfied_steps_leave_and_a_researched_technology_satisfies_its_step(self):
        result = path([done("a")], technologies=["t1"], enabled=["widget"])
        self.assertEqual(result.frontier, ("b", "c", "t2"))  # d waits on the off-path step "side"
        self.assertNotIn("side", result.steps)

    def test_prerequisites_are_computed_from_requires_recipes_and_technologies(self):
        # c needs capability a (requires) and recipe widget (unlocked by t1); t2 needs t1.
        result = path([], horizon=8, enabled=[])
        self.assertIn("t1", result.steps)
        only_c = critical_path(
            [], {"enabled_recipes": {}}, CAPS, roadmap=(ROADMAP[0], ROADMAP[1], ROADMAP[4]), catalog=CATALOG, horizon=1
        )
        self.assertEqual(only_c.frontier, ("a",))
        deep = critical_path(
            [done("a")],
            {"enabled_recipes": {}},
            CAPS,
            roadmap=(ROADMAP[0], ROADMAP[4], ROADMAP[1]),
            catalog=CATALOG,
            horizon=1,
        )
        self.assertEqual(deep.frontier, ("c",))
        self.assertEqual(deep.steps, ("c", "t1"))  # the recipe lock pulls its research in, out of roadmap order
        self.assertEqual(deep.ready, ("t1",))

    def test_stalled_steps_are_reported_and_skipped(self):
        result = path([done("a"), done("r", {"technology": "t1"}, status="blocked")])
        self.assertEqual(result.stalled, ("t1",))
        self.assertNotIn("t1", result.frontier)

    def test_off_path_dependency_keeps_a_step_out_of_the_frontier(self):
        jobs = [done("a"), done("b"), done("c"), done("r", {"technology": "t1"}), done("r", {"technology": "t2"})]
        result = path(jobs, technologies=["t1", "t2"], enabled=["widget"])
        self.assertEqual(result.frontier, ("e",))
        jobs.append(done("side"))
        self.assertEqual(path(jobs, technologies=["t1", "t2"], enabled=["widget"]).frontier, ("d", "e"))


class AllocationTests(unittest.TestCase):
    def jobs(self):
        return {
            "k_a": {
                "capability": "a",
                "parameters": {},
                "status": "waiting",
                "reason": "waiting for drill-fed iron-plate production",
                "evidence": {"item": "iron-plate", "have": 4, "need": 20},
            },
            "k_e": {"capability": "e", "parameters": {}, "status": "running", "reason": "build"},
            "k_make": {
                "capability": "make",
                "parameters": {"products": ["transport-belt"]},
                "status": "waiting",
                "reason": "waiting for copper-plate",
            },
        }

    def test_critical_jobs_and_their_claims(self):
        jobs = self.jobs()
        result = path([])
        keys = critical_jobs(jobs, result, roadmap=ROADMAP, known_items=CATALOG["items"])
        self.assertEqual(keys, ["k_a"])
        self.assertEqual(claims(jobs["k_a"], CATALOG["items"]), {"iron-plate": 20.0})
        claimed = critical_claims(jobs, keys, CATALOG["items"])
        self.assertEqual(claimed, {"iron-plate": {"units": 20.0, "jobs": ["a"]}})

    def test_a_slack_supplier_of_a_claimed_non_raw_item_becomes_critical(self):
        jobs = self.jobs()
        jobs["k_a"]["evidence"] = {"needs": {"transport-belt": 30}}
        keys = critical_jobs(jobs, path([]), roadmap=ROADMAP, known_items=CATALOG["items"])
        self.assertEqual(keys, ["k_a", "k_make"])
        # Raw items do not promote: the supplier of a plate is capacity, which stays slack.
        jobs["k_make"]["parameters"] = {"products": ["iron-plate"]}
        jobs["k_a"]["evidence"] = {"item": "iron-plate", "need": 5}
        self.assertEqual(critical_jobs(jobs, path([]), roadmap=ROADMAP, known_items=CATALOG["items"]), ["k_a"])

    def test_slack_view_hides_claimed_units_and_outputs(self):
        obs = {
            "inventory": {"iron-plate": 30, "coal": 5, "copper-plate": 3},
            "entities": [
                {"name": "stone-furnace", "output_inventory": {"iron-plate": 7}},
                {"name": "wooden-chest", "inventory": {"iron-plate": 2}},
            ],
        }
        claimed = {"iron-plate": {"units": 20.0, "jobs": ["a"]}, "copper-plate": {"units": None, "jobs": ["a"]}}
        view = slack_view(obs, claimed)
        self.assertEqual(view["inventory"], {"iron-plate": 10, "coal": 5})
        self.assertEqual(view["entities"][0]["output_inventory"], {})
        self.assertEqual(view["entities"][1]["inventory"], {"iron-plate": 2})
        self.assertEqual(obs["inventory"]["iron-plate"], 30)  # the real observation is untouched

    def test_slack_refusal_turns_spending_a_claimed_item_into_a_wait(self):
        obs = {"inventory": {"iron-plate": 30}}
        claimed = {"iron-plate": {"units": 20.0, "jobs": ["power"]}, "coal": {"units": None, "jobs": ["power"]}}
        ok = {"type": "insert", "item": "iron-plate", "count": 10}
        self.assertIsNone(slack_refusal(ok, obs, claimed))
        too_much = {"type": "insert", "item": "iron-plate", "count": 11}
        wait = slack_refusal(too_much, obs, claimed)
        self.assertEqual(wait["status"], "waiting")
        self.assertEqual(wait["evidence"]["reserved_for"], ["power"])
        self.assertIsNotNone(slack_refusal({"type": "take_many", "item": "iron-plate", "count": 5}, obs, claimed))
        self.assertIsNotNone(slack_refusal({"type": "insert", "item": "coal", "count": 1}, obs, claimed))
        self.assertIsNone(slack_refusal({"type": "mine", "name": "tree"}, obs, claimed))
        recipes = {"recipes": {"boiler": {"ingredients": [{"name": "coal", "amount": 1}]}}}
        self.assertIsNotNone(slack_refusal({"type": "craft", "recipe": "boiler", "count": 1}, obs, claimed, recipes))


if __name__ == "__main__":
    unittest.main()
