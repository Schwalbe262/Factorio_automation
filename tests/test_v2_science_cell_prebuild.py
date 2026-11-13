"""Early start of the startup science cell while automation is researched (BootstrapLogistics.prebuild)."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from factorio_agent.board import lab_slot_entities
from factorio_agent.early_start import Deferral
from factorio_agent.handlers.research import prebuild_science_cell
from factorio_agent.research import StarterResearch
import test_v2_bootstrap_research as fixtures


class ScienceCellPrebuildTests(unittest.TestCase):
    def setUp(self):
        f = fixtures.BootstrapResearchTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        self.obs = f.obs
        self.obs["technologies"]["automation"] = False
        self.obs["research"] = "automation"
        self.obs["enabled_recipes"]["assembling-machine-1"] = False
        self.batch = f.make("electric-mining-drill")
        f.builder.ensure_plan = Mock(return_value={"type": "build", "name": "inserter", "position": {"x": 1, "y": 1}})

    def built_plan(self):
        args, kwargs = self.f.builder.ensure_plan.call_args
        return args[1], kwargs.get("defer")

    def test_builds_the_cell_with_assemblers_last_and_deferred_until_automation(self):
        action = self.batch.prebuild(self.obs)
        self.assertEqual(action["type"], "build")
        plan, defer = self.built_plan()
        self.assertIsInstance(defer, Deferral)
        self.assertEqual(defer.coming, {"automation"})
        names = [e["name"] for e in plan["entities"]]
        first = names.index("assembling-machine-1")
        self.assertTrue(all(name == "assembling-machine-1" for name in names[first:]))
        self.assertEqual(sorted(names), sorted(e["name"] for e in self.f.plan["entities"]))
        # Nothing is bound, funded or researched before the unlock.
        self.assertEqual(self.batch.state, {})
        self.assertFalse(self.batch.path.exists())

    def test_decided_slot_labs_are_built_up_front(self):
        decision = dict(target_assemblers=1, target_labs=2, expand=True)
        (self.f.root / "startup-science-capacity.json").write_text(
            json.dumps(dict(world_id="w", decisions={"electric-mining-drill": decision})), encoding="utf-8"
        )
        self.batch.prebuild(self.obs)
        plan, _ = self.built_plan()
        slots = lab_slot_entities(self.f.plan)
        self.assertTrue(slots)
        self.assertIn(slots[0]["lab"], plan["entities"])
        self.assertEqual(sum(e["name"] == "lab" for e in plan["entities"]), 1)

    def test_only_while_the_unlock_research_runs_and_before_any_batch_state(self):
        for change in ("other_research", "unlocked", "target_done", "state"):
            obs = json.loads(json.dumps(self.obs))
            batch = self.f.make("electric-mining-drill")
            if change == "other_research":
                obs["research"] = None
            elif change == "unlocked":
                obs["technologies"]["automation"] = True
            elif change == "target_done":
                obs["technologies"]["electric-mining-drill"] = True
            else:
                batch.state = {"world_id": "w"}
            self.assertIsNone(batch.prebuild(obs), change)
        self.f.builder.ensure_plan.assert_not_called()

    def test_a_blocked_placement_is_recorded_once_and_not_retried(self):
        decision = dict(target_assemblers=1, target_labs=1)
        path = self.f.root / "startup-science-capacity.json"
        path.write_text(json.dumps(dict(world_id="w", decisions={"electric-mining-drill": decision})), encoding="utf-8")
        self.f.builder.ensure_plan = Mock(return_value={"status": "blocked", "reason": "tree"})
        self.assertIsNone(self.batch.prebuild(self.obs))
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["decisions"]["electric-mining-drill"]["prebuild_blocked"], "tree")
        self.assertIsNone(self.batch.prebuild(self.obs))
        self.assertEqual(self.f.builder.ensure_plan.call_count, 1)

    def test_handler_returns_prebuild_actions_only_from_a_plain_starter_wait(self):
        waiting = {"status": "waiting", "reason": "lab consumes normal startup science"}
        ex = SimpleNamespace(_next_bootstrap_target=Mock(return_value=self.batch))
        action = prebuild_science_cell(ex, self.obs, {"technology": "automation"}, waiting)
        self.assertEqual(action["type"], "build")
        research = {"type": "research", "technology": "automation"}
        self.assertIs(prebuild_science_cell(ex, self.obs, {"technology": "automation"}, research), research)
        self.assertIs(prebuild_science_cell(ex, self.obs, {"technology": "electric-mining-drill"}, waiting), waiting)
        self.f.builder.ensure_plan = Mock(return_value={"status": "waiting", "reason": "deferred"})
        self.assertIs(prebuild_science_cell(ex, self.obs, {"technology": "automation"}, waiting), waiting)

    def test_next_target_is_the_first_unresearched_bootstrap_research_of_the_roadmap(self):
        from factorio_agent.jobs import StarterExecutors

        ex = StarterExecutors.__new__(StarterExecutors)
        ex.game, ex.bootstrap, ex.builder, ex.catalog = self.f.game, self.f.bootstrap, self.f.builder, self.f.catalog
        ex.industry = self.f.make("logistics")
        self.obs["technologies"].pop("logistics")
        self.assertEqual(ex._next_bootstrap_target(self.obs).technology, "electric-mining-drill")
        self.obs["technologies"]["electric-mining-drill"] = True
        self.assertIs(ex._next_bootstrap_target(self.obs), ex.industry)
        self.obs["technologies"]["logistics"] = True
        self.assertIsNone(ex._next_bootstrap_target(self.obs))

    def test_the_temporary_cell_queue_is_the_roadmap_bootstrap_steps(self):
        from factorio_agent.industry import bootstrap_technologies

        self.assertEqual(bootstrap_technologies(), ["electric-mining-drill", "logistics"])

    def test_starter_lab_prefers_the_connected_lab_that_holds_the_packs(self):
        research = StarterResearch(self.f.game, self.f.bootstrap, self.f.catalog)
        slot = dict(name="lab", unit_number=90, position={"x": 9.5, "y": 9.5}, electric_network_connected=False)
        busy = dict(
            name="lab",
            unit_number=91,
            position={"x": 0.5, "y": 0.5},
            electric_network_connected=True,
            inventory={"automation-science-pack": 4},
        )
        idle = dict(name="lab", unit_number=92, position={"x": 5.5, "y": 5.5}, electric_network_connected=True)
        obs = {"entities": [slot, idle, busy]}
        self.assertIs(research.lab(obs)["entity"], busy)
        obs = {"entities": [slot, idle]}
        self.assertIs(research.lab(obs)["entity"], idle)
        self.assertEqual(research.lab({"entities": [slot]})["reason"], "existing_lab_not_connected")


if __name__ == "__main__":
    unittest.main()
