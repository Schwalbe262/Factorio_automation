"""Several active jobs interleaved at action granularity by run_jobs."""

from contextlib import redirect_stderr
import io
from pathlib import Path
import json
import tempfile
import unittest

from factorio_agent.jobs import MAX_ACTIVE_JOBS, Ledger, Request, control, run_jobs, run_slice, submit
from factorio_agent.__main__ import execute


class Game:
    def __init__(self):
        self.observation = {"ok": True, "world_id": "world", "tick": 100, "entities": [], "production": {}}
        self.actions = []
        self.observations = 0
        self.lose_receipt_for = set()
        self.saved = 0

    def observe(self):
        self.observations += 1
        return json.loads(json.dumps(self.observation))

    def act(self, action):
        self.actions.append(action)
        if action.get("job") in self.lose_receipt_for:
            raise TimeoutError("server outcome unknown")
        # Every performed action changes the world the next pass observes.
        self.observation["tick"] += 1
        return {"ok": True}

    def save(self):
        self.saved += 1


class Executor:
    """Per-capability scripted results; the last result repeats once a script runs out."""

    def __init__(self, scripts, proofs=None):
        self.scripts = {capability: list(results) for capability, results in scripts.items()}
        self.proofs = proofs or {}
        self.calls = []

    def step(self, capability, obs, parameters=None):
        self.calls.append((capability, obs["tick"]))
        script = self.scripts[capability]
        return script.pop(0) if len(script) > 1 else script[0]

    def proof(self, capability, obs, result, parameters=None):
        return self.proofs.get(capability)


def act(job):
    return {"type": "craft", "recipe": "iron-gear-wheel", "count": 1, "job": job}


WAIT = {"status": "waiting", "reason": "crafting_queue_busy"}


class ConcurrentJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = Game()
        # Most capabilities require a verified starter; earn it the normal way.
        self.submit("starter", "starter")
        done = run_slice(
            self.root, self.game, Executor({"starter": [{"status": "succeeded"}]}, {"starter": {"ok": True}}), "starter"
        )
        self.assertEqual(done["status"], "succeeded")

    def submit(self, key, capability, priority=0, parameters=None):
        request = Request(key, "world", self.game.observation["tick"], capability, parameters or {})
        return submit(self.root, self.game, request, priority=priority)

    def jobs(self):
        return Ledger(self.root).data["jobs"]

    def test_two_jobs_can_be_active_together(self):
        self.submit("a", "power")
        self.submit("b", "defense_perimeter")
        jobs = self.jobs()
        self.assertEqual(jobs["a"]["status"], "pending")
        self.assertEqual(jobs["b"]["status"], "pending")

    def test_duplicate_active_capability_is_refused(self):
        self.submit("a", "power")
        with self.assertRaisesRegex(ValueError, "capability power is already active"):
            self.submit("b", "power")
        control(self.root, self.game, "a", "pause")
        self.submit("b", "power")
        with self.assertRaisesRegex(ValueError, "capability power is already active"):
            control(self.root, self.game, "a", "resume")

    def test_only_one_research_owning_job_is_active(self):
        self.submit("power", "power")
        run_slice(self.root, self.game, Executor({"power": [{"status": "succeeded"}]}, {"power": {"ok": 1}}), "power")
        self.submit("logistics", "bootstrap_logistics")
        request = Request(
            "drill", "world", self.game.observation["tick"], "starter_research", {"technology": "automation"}
        )
        with self.assertRaisesRegex(ValueError, "owns research is already active"):
            submit(self.root, self.game, request)
        control(self.root, self.game, "logistics", "pause")
        submit(self.root, self.game, request)
        with self.assertRaisesRegex(ValueError, "owns research is already active"):
            control(self.root, self.game, "logistics", "resume")

    def test_fifth_active_job_is_refused(self):
        self.submit("power", "power")
        run_slice(self.root, self.game, Executor({"power": [{"status": "succeeded"}]}, {"power": {"ok": 1}}), "power")
        for key in ("regularize_power_layout", "defense_perimeter", "bootstrap_logistics", "power_coal_feed"):
            self.submit(key, key)
        self.assertEqual(MAX_ACTIVE_JOBS, 4)
        with self.assertRaisesRegex(ValueError, "at most 4 jobs may be active"):
            self.submit("water_gathering", "water_gathering")
        control(self.root, self.game, "power_coal_feed", "pause")
        self.submit("water_gathering", "water_gathering")
        with self.assertRaisesRegex(ValueError, "at most 4 jobs may be active"):
            control(self.root, self.game, "power_coal_feed", "resume")

    def test_priority_is_stored_but_not_part_of_identity(self):
        job = self.submit("a", "power", priority=7)
        self.assertEqual(job["priority"], 7)
        self.assertNotIn("priority", job["request"])
        # Idempotent resubmission with another priority returns the original job.
        self.assertEqual(self.submit("a", "power", priority=1)["priority"], 7)
        for bad in (10001, -10001, 1.5, True):
            with self.assertRaisesRegex(ValueError, "priority"):
                self.submit("b", "defense_perimeter", priority=bad)

    def test_waiting_job_does_not_stop_another_from_acting(self):
        self.submit("a", "power", priority=5)
        self.submit("b", "defense_perimeter")
        executor = Executor({"power": [WAIT], "defense_perimeter": [act("b")]})
        receipt = run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(receipt["status"], "running")
        self.assertGreaterEqual(receipt["actions"], 1)
        self.assertEqual({action["job"] for action in self.game.actions}, {"b"})
        self.assertEqual(receipt["jobs"]["a"]["status"], "waiting")
        self.assertEqual(receipt["jobs"]["a"]["reason"], "crafting_queue_busy")
        self.assertEqual(receipt["jobs"]["b"]["actions"], receipt["actions"])
        self.assertEqual(receipt["jobs"]["b"]["capability"], "defense_perimeter")
        self.assertEqual(self.jobs()["a"]["slice_yield_reason"], "execution_slice_complete")

    def test_planner_fault_blocks_only_its_own_job(self):
        # Live seed 1400895309: a stale migration job raised FileNotFoundError on
        # every slice and the whole controller backed off for 300 s each time.
        class Faulty(Executor):
            def step(self, capability, obs, parameters=None):
                if capability == "power":
                    raise FileNotFoundError("smelter-lanes.json")
                return super().step(capability, obs, parameters)

        self.submit("a", "power", priority=5)
        self.submit("b", "defense_perimeter")
        receipt = run_jobs(self.root, self.game, Faulty({"defense_perimeter": [act("b")]}), seconds=0.05)
        self.assertEqual(self.jobs()["a"]["status"], "blocked")
        self.assertTrue(self.jobs()["a"]["reason"].startswith("planner_fault:FileNotFoundError"))
        self.assertGreaterEqual(receipt["jobs"]["b"]["actions"], 1)

    def test_an_operator_stop_raised_while_planning_is_a_clean_stop(self):
        # Fleet aud-1114-0703 seed 3743340251: the builder's can_place saw stop.json mid-plan and
        # its InterruptedError escaped the slice as an "invalid typed CLI receipt" traceback.
        root = self.root

        class Stopping(Executor):
            def step(self, capability, obs, parameters=None):
                (root / "stop.json").write_text("{}", encoding="utf-8")  # the stop arrives mid-plan
                raise InterruptedError("operator_stop_requested")

        self.submit("a", "power")
        receipt = run_jobs(self.root, self.game, Stopping({}), seconds=0.05)
        self.assertEqual(self.jobs()["a"]["status"], "waiting")
        self.assertEqual(self.jobs()["a"]["reason"], "operator_stop_requested")
        self.assertNotIn("pending_action", self.jobs()["a"])
        self.assertEqual(self.game.actions, [])
        self.assertIsInstance(receipt, dict)

    def test_capacity_jobs_for_different_resources_run_side_by_side(self):
        # Seed 283895142: copper capacity waited 2.5 min behind iron capacity.
        iron = {"cells": 3, "resource": "iron-ore"}
        self.submit("iron", "starter_capacity", parameters=iron)
        self.submit("copper", "starter_capacity", parameters={"cells": 2, "resource": "copper-ore"})
        with self.assertRaisesRegex(ValueError, "already active"):
            self.submit("iron2", "starter_capacity", parameters={"cells": 4, "resource": "iron-ore"})

    def test_priority_decides_who_acts_first(self):
        # Neither job is on the critical path (power is the frontier): priority decides.
        self.submit("low", "regularize_power_layout")
        self.submit("high", "defense_perimeter", priority=3)
        executor = Executor({"regularize_power_layout": [act("low")], "defense_perimeter": [act("high"), WAIT]})
        run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(self.game.actions[0]["job"], "high")
        self.assertEqual(self.game.actions[1]["job"], "low")

    def test_critical_job_acts_before_a_higher_priority_slack_job(self):
        self.submit("power", "power")
        self.submit("slack", "defense_perimeter", priority=3)
        executor = Executor({"power": [act("power"), WAIT], "defense_perimeter": [act("slack")]})
        run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(self.game.actions[0]["job"], "power")
        self.assertEqual(self.game.actions[1]["job"], "slack")

    def test_slack_job_never_spends_an_item_a_critical_job_waits_for(self):
        self.game.observation["inventory"] = {"iron-plate": 30, "coal": 5}
        self.submit("power", "power")
        self.submit("slack", "defense_perimeter", priority=3)
        waiting = {
            "status": "waiting",
            "reason": "waiting for drill-fed iron-plate production",
            "evidence": {"item": "iron-plate", "have": 30, "need": 50},
        }
        seen = []

        class Recording(Executor):
            def step(self, capability, obs, parameters=None):
                if capability == "defense_perimeter":
                    seen.append(dict(obs.get("inventory") or {}))
                return super().step(capability, obs, parameters)

        spend = {"type": "insert", "name": "wooden-chest", "item": "iron-plate", "count": 10, "job": "slack"}
        executor = Recording({"power": [waiting], "defense_perimeter": [spend]})
        run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(self.game.actions, [])  # the insert became a wait
        self.assertEqual(seen[0], {"coal": 5})  # 30 iron held, 50 claimed: the slack view shows none
        slack = self.jobs()["slack"]
        self.assertEqual(slack["status"], "waiting")
        self.assertEqual(slack["evidence"]["reserved_item"], "iron-plate")
        self.assertIn("power", slack["evidence"]["reserved_for"])

    def test_equal_priority_runs_oldest_first(self):
        self.submit("first", "power")
        self.submit("second", "defense_perimeter")
        executor = Executor({"power": [act("first"), WAIT], "defense_perimeter": [act("second")]})
        run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(self.game.actions[0]["job"], "first")

    def test_next_pass_reobserves_after_an_action(self):
        self.submit("a", "power", priority=1)
        self.submit("b", "defense_perimeter")
        executor = Executor({"power": [act("a"), WAIT], "defense_perimeter": [act("b"), WAIT]})
        run_jobs(self.root, self.game, executor, seconds=0.05)
        # a acts at tick 100 and ends the pass; b must plan against the new world.
        self.assertEqual(executor.calls[0], ("power", 100))
        self.assertEqual(executor.calls[1], ("power", 101))
        self.assertEqual(executor.calls[2], ("defense_perimeter", 101))
        # A pass without an action shares one observation across all jobs.
        self.assertEqual(executor.calls[3], ("power", 102))
        self.assertEqual(executor.calls[4], ("defense_perimeter", 102))

    def test_terminal_job_does_not_end_the_slice_for_others(self):
        self.submit("a", "power", priority=1)
        self.submit("b", "defense_perimeter")
        executor = Executor(
            {"power": [{"status": "succeeded", "reason": "verified"}], "defense_perimeter": [act("b")]},
            {"power": {"verified": True}},
        )
        receipt = run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(receipt["jobs"]["a"]["status"], "succeeded")
        self.assertGreaterEqual(receipt["jobs"]["b"]["actions"], 2)
        self.assertEqual([c for c, _ in executor.calls].count("power"), 1)

    def test_outcome_unknown_blocks_only_that_job_and_propagates(self):
        self.submit("a", "power", priority=1)
        self.submit("b", "defense_perimeter")
        self.game.lose_receipt_for = {"a"}
        executor = Executor({"power": [act("a")], "defense_perimeter": [act("b")]})
        with self.assertRaises(TimeoutError):
            run_jobs(self.root, self.game, executor, seconds=0.05)
        jobs = self.jobs()
        self.assertEqual(jobs["a"]["status"], "blocked")
        self.assertEqual(jobs["a"]["reason"], "action_outcome_unknown_reconcile_required")
        self.assertTrue(jobs["a"]["pending_action"])
        self.assertEqual(jobs["b"]["status"], "pending")
        self.assertEqual(jobs["b"]["actions"], 0)
        # b keeps running in the next slice; a stays blocked until reconciled.
        receipt = run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertNotIn("a", receipt["jobs"])
        self.assertGreaterEqual(receipt["jobs"]["b"]["actions"], 1)
        self.assertEqual(len([action for action in self.game.actions if action["job"] == "a"]), 1)

    def test_stop_file_stops_every_active_job(self):
        self.submit("a", "power")
        self.submit("b", "defense_perimeter")
        (self.root / "stop.json").write_text('{"source":"test"}', encoding="utf-8")
        executor = Executor({"power": [act("a")], "defense_perimeter": [act("b")]})
        receipt = run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(executor.calls, [])
        self.assertEqual(self.game.actions, [])
        for key in ("a", "b"):
            self.assertEqual(receipt["jobs"][key]["status"], "waiting")
            self.assertEqual(receipt["jobs"][key]["reason"], "operator_stop_requested")
        self.assertEqual(receipt["actions"], 0)

    def test_no_active_job_is_idle(self):
        receipt = run_jobs(self.root, self.game, Executor({}), seconds=0.05)
        self.assertEqual(receipt["status"], "idle")
        self.assertEqual(receipt["jobs"], {})
        self.assertEqual(receipt["last_tick"], self.game.observation["tick"])

    def test_waiting_only_slice_reports_waiting_and_saves_once(self):
        self.submit("a", "power")
        self.submit("b", "defense_perimeter")
        saved = self.game.saved
        receipt = run_jobs(self.root, self.game, Executor({"power": [WAIT], "defense_perimeter": [WAIT]}), seconds=0.05)
        self.assertEqual(receipt["status"], "waiting")
        self.assertEqual(self.game.saved, saved + 1)

    def test_run_slice_still_advances_only_its_key(self):
        self.submit("a", "power")
        self.submit("b", "defense_perimeter")
        executor = Executor({"power": [act("a")], "defense_perimeter": [act("b")]})
        result = run_slice(self.root, self.game, executor, "a", seconds=0.05)
        self.assertEqual(result["key"], "a")
        self.assertEqual(result["reason"], "execution_slice_complete")
        self.assertEqual({action["job"] for action in self.game.actions}, {"a"})
        self.assertEqual(self.jobs()["b"]["status"], "pending")

    def test_cli_run_all_uses_service_executors(self):
        self.submit("a", "power")
        self.submit("b", "defense_perimeter")
        executor = Executor({"power": [WAIT], "defense_perimeter": [act("b")]})
        game = self.game

        class Services:
            def game(self, root):
                return game

            def catalog(self, root, game):
                return {}

            def executors(self, root, game, catalog):
                return executor

        receipt = execute(["--runtime", str(self.root), "run", "--all", "--seconds", "0.05"], Services())
        self.assertEqual(set(receipt["jobs"]), {"a", "b"})
        self.assertGreaterEqual(receipt["actions"], 1)
        for argv in (["run"], ["run", "--all", "--key", "a"]):
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                execute(["--runtime", str(self.root), *argv], Services())

    def test_cli_request_threads_priority(self):
        game = self.game

        class Services:
            def game(self, root):
                return game

        argv = ["--runtime", str(self.root), "request", "--key", "p", "--world-id", "world"]
        argv += ["--observed-tick", "100", "--capability", "power", "--priority", "9"]
        self.assertEqual(execute(argv, Services())["priority"], 9)


if __name__ == "__main__":
    unittest.main()
