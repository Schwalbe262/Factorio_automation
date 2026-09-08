from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_bootstrap import DeterministicBootstrap
from factorio_ai.deterministic_supervisor import DeterministicSupervisor


CELL = {"drill": {"name": "electric-mining-drill", "position": {"x": .5, "y": .5}, "direction": 4},
        "drop_position": {"x": 2.5, "y": .5}, "receiver": {"name": "stone-furnace", "position": {"x": 3, "y": 0}},
        "fuel": 0, "burning": False, "electric": True, "operating": True, "remaining": 1234}


class CellSurveyScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), backend="assisted",
                                    query=Mock(side_effect=lambda body: {"ok": True, "cells": [deepcopy(CELL)]}), act=Mock())
        self.bootstrap = DeterministicBootstrap(self.game)
        self.supervisor = DeterministicSupervisor(self.game)
        self.supervisor.bootstrap = self.bootstrap
        self.obs = {"world_id": "first", "tick": 100}

    def plan(self, observation, until):
        first = self.bootstrap._existing_cells("iron-ore")
        second = self.bootstrap._existing_cells("iron-ore")
        self.assertEqual(first, second)
        return {"type": "take", "name": "stone-furnace", "count": 1}

    def test_only_repeated_resource_is_reused_in_the_supervisor_call_for_both_backends(self):
        for backend in ("assisted", "character"):
            self.game.backend = backend
            self.game.query.reset_mock()
            def plan(observation, until):
                for resource in ("iron-ore", "coal", "iron-ore", "stone", "coal"):
                    self.bootstrap._existing_cells(resource)
                return {"status": "waiting"}
            self.supervisor._plan_action = plan
            with self.subTest(backend=backend):
                self.supervisor.next_action(self.obs, "rocket")
                self.assertEqual(self.game.query.call_count, 3)
                self.assertIsNone(self.bootstrap._cell_surveys)

    def test_direct_calls_are_fresh_before_and_after_an_action_producing_plan(self):
        self.bootstrap._existing_cells("iron-ore")
        self.bootstrap._existing_cells("iron-ore")
        self.assertEqual(self.game.query.call_count, 2)
        self.supervisor._plan_action = self.plan
        choice = self.supervisor.next_action(self.obs, "rocket")
        self.assertEqual(self.game.query.call_count, 3)
        self.game.act(choice)
        self.bootstrap._existing_cells("iron-ore")
        self.assertEqual(self.game.query.call_count, 4)
        self.supervisor.next_action(self.obs, "rocket")  # Same observation still starts fresh.
        self.assertEqual(self.game.query.call_count, 5)

    def test_new_observation_world_rollback_and_new_bootstrap_never_reuse_previous_call(self):
        self.supervisor._plan_action = self.plan
        for obs in (self.obs, dict(self.obs), {**self.obs, "tick": 101}, {**self.obs, "tick": 5},
                    {**self.obs, "world_id": "second"}):
            self.supervisor.next_action(obs, "rocket")
        self.assertEqual(self.game.query.call_count, 5)
        self.supervisor.bootstrap = self.bootstrap = DeterministicBootstrap(self.game)
        self.supervisor.next_action(self.obs, "rocket")
        self.assertEqual(self.game.query.call_count, 6)

    def test_failed_survey_retries_and_successful_empty_survey_is_reused(self):
        self.game.query.side_effect = [{"ok": False, "reason": "RCON timeout"}, {"ok": True, "cells": {}}]
        def plan(observation, until):
            self.assertFalse(self.bootstrap._existing_cells("coal")["ok"])
            self.assertEqual(self.bootstrap._existing_cells("coal"), {"ok": True, "cells": {}})
            self.assertEqual(self.bootstrap._existing_cells("coal"), {"ok": True, "cells": {}})
            return {"status": "waiting"}
        self.supervisor._plan_action = plan
        self.supervisor.next_action(self.obs, "rocket")
        self.assertEqual(self.game.query.call_count, 2)

    def test_caller_mutation_cannot_change_cached_drop_receiver_fuel_or_electric_evidence(self):
        def plan(observation, until):
            result = self.bootstrap._existing_cells("iron-ore")
            result["cells"][0]["drop_position"]["x"] = 90
            result["cells"][0]["receiver"]["name"] = "wooden-chest"
            result["cells"][0].update(fuel=100, electric=False, operating=False, remaining=0)
            cached = self.bootstrap._existing_cells("iron-ore")
            self.assertEqual(cached["cells"], [CELL])
            cached["cells"].clear()
            self.assertEqual(self.bootstrap._existing_cells("iron-ore")["cells"], [CELL])
            return {"status": "waiting"}
        self.supervisor._plan_action = plan
        self.supervisor.next_action(self.obs, "rocket")
        self.assertEqual(self.game.query.call_count, 1)

    def test_planning_exception_clears_scope_and_failed_query_is_not_stored(self):
        def plan(observation, until):
            self.bootstrap._existing_cells("iron-ore")
            raise RuntimeError("planner interrupted")
        self.supervisor._plan_action = plan
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.supervisor.next_action(self.obs, "rocket")
        self.assertIsNone(self.bootstrap._cell_surveys)
        self.bootstrap._existing_cells("iron-ore")
        self.assertEqual(self.game.query.call_count, 2)
        self.game.query.side_effect = [TimeoutError("failed query"), {"ok": True, "cells": []}]
        with self.bootstrap.cell_survey_scope():
            with self.assertRaises(TimeoutError):
                self.bootstrap._existing_cells("coal")
            self.assertTrue(self.bootstrap._existing_cells("coal")["ok"])

    def test_nested_call_discards_outer_snapshot_and_restores_outer_scope(self):
        def plan(observation, until):
            self.bootstrap._existing_cells("iron-ore")
            if observation["world_id"] == "first":
                self.supervisor.next_action({"world_id": "second", "tick": 1}, until)
                self.bootstrap._existing_cells("iron-ore")
                self.bootstrap._existing_cells("iron-ore")
            return {"status": "waiting"}
        self.supervisor._plan_action = plan
        self.supervisor.next_action(self.obs, "rocket")
        self.assertEqual(self.game.query.call_count, 3)
        self.assertIsNone(self.bootstrap._cell_surveys)

    def test_caught_nested_exception_also_invalidates_the_restored_outer_snapshot(self):
        def plan(observation, until):
            self.bootstrap._existing_cells("coal")
            if observation["world_id"] == "second":
                raise RuntimeError("nested failure")
            with self.assertRaisesRegex(RuntimeError, "nested failure"):
                self.supervisor.next_action({"world_id": "second", "tick": 1}, until)
            self.bootstrap._existing_cells("coal")
            self.bootstrap._existing_cells("coal")
            return {"status": "waiting"}
        self.supervisor._plan_action = plan
        self.supervisor.next_action(self.obs, "rocket")
        self.assertEqual(self.game.query.call_count, 3)
        self.assertIsNone(self.bootstrap._cell_surveys)


if __name__ == "__main__":
    unittest.main()
