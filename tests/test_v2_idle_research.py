"""Research lane: run_jobs offers action-free passes to executors.idle_action."""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from factorio_agent import progression
from factorio_agent.core.rcon import RconError
from factorio_agent.jobs import (
    IDLE_RESEARCH_INTERVAL_TICKS,
    Ledger,
    Request,
    StarterExecutors,
    run_jobs,
    run_slice,
    submit,
)
from factorio_agent.normal_research import NormalResearch

RED = {"name": "automation-science-pack", "amount": 1}
GREEN = {"name": "logistic-science-pack", "amount": 1}
WAIT = {"status": "waiting", "reason": "crafting_queue_busy"}
RESEARCH = {"type": "research", "technology": "cheap", "reason": "idle"}


class Game:
    def __init__(self, root):
        self.cfg = SimpleNamespace(runtime_dir=str(root))
        self.observation = {"ok": True, "world_id": "world", "tick": 100, "entities": [], "production": {}}
        self.actions = []
        self.act_error = None

    def observe(self):
        return json.loads(json.dumps(self.observation))

    def act(self, action):
        self.actions.append(action)
        if self.act_error is not None:
            raise self.act_error
        self.observation["tick"] += 1
        return {"ok": True}

    def save(self):
        pass


class PlainExecutor:
    """Scripted per-capability results; the last result repeats once a script runs out."""

    def __init__(self, scripts, proofs=None):
        self.scripts = {capability: list(results) for capability, results in scripts.items()}
        self.proofs = proofs or {}
        self.steps = []

    def step(self, capability, obs, parameters=None):
        self.steps.append((capability, obs["tick"]))
        script = self.scripts[capability]
        return script.pop(0) if len(script) > 1 else script[0]

    def proof(self, capability, obs, result, parameters=None):
        return self.proofs.get(capability)


class Executor(PlainExecutor):
    """A scripted idle lane on top of scripted jobs; an exception in the script is raised."""

    def __init__(self, scripts, idle=(), proofs=None):
        super().__init__(scripts, proofs)
        self.idle = list(idle)
        self.idle_calls = []

    def idle_action(self, obs):
        self.idle_calls.append(obs["tick"])
        result = self.idle.pop(0) if self.idle else None
        if isinstance(result, BaseException):
            raise result
        return result


class RunJobsHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = Game(self.root)
        submit(self.root, self.game, Request("starter", "world", 100, "starter"))
        done = run_slice(
            self.root,
            self.game,
            PlainExecutor({"starter": [{"status": "succeeded"}]}, proofs={"starter": {"ok": 1}}),
            "starter",
        )
        self.assertEqual(done["status"], "succeeded")
        submit(self.root, self.game, Request("power", "world", self.game.observation["tick"], "power"))

    def events(self, key="lane:research"):
        lines = (self.root / "job-events.jsonl").read_text(encoding="utf-8").splitlines()
        return [row for row in map(json.loads, lines) if row["key"] == key]

    def test_action_free_pass_performs_and_logs_the_idle_action(self):
        executor = Executor({"power": [WAIT]}, idle=[RESEARCH])
        receipt = run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(self.game.actions[0], RESEARCH)
        self.assertEqual(receipt["idle_actions"], 1)
        self.assertEqual(receipt["actions"], 0)
        kinds = [row["event"] for row in self.events()]
        self.assertEqual(kinds[:2], ["action_intent", "action_result"])
        self.assertEqual(self.events()[0]["action"], RESEARCH)
        # No job record carries the idle action.
        self.assertNotIn("pending_action", Ledger(self.root).data["jobs"]["power"])
        self.assertNotIn("lane:research", Ledger(self.root).data["jobs"])

    def test_idle_action_starts_a_new_pass_on_a_fresh_observation(self):
        executor = Executor({"power": [WAIT]}, idle=[RESEARCH])
        run_jobs(self.root, self.game, executor, seconds=0.05)
        ticks = [tick for capability, tick in executor.steps if capability == "power"]
        self.assertGreaterEqual(len(ticks), 2)
        self.assertEqual(ticks[1], ticks[0] + 1)

    def test_not_called_on_a_pass_where_a_job_acted(self):
        craft = {"type": "craft", "recipe": "iron-gear-wheel", "count": 1}
        executor = Executor({"power": [craft, WAIT]})
        run_jobs(self.root, self.game, executor, seconds=0.05)
        acted_tick = executor.steps[0][1]
        self.assertNotIn(acted_tick, executor.idle_calls)
        self.assertIn(acted_tick + 1, executor.idle_calls)

    def test_receipt_reports_zero_idle_actions_when_lane_returns_nothing(self):
        executor = Executor({"power": [WAIT]})
        receipt = run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(receipt["idle_actions"], 0)
        self.assertEqual(self.game.actions, [])
        self.assertTrue(executor.idle_calls)

    def test_executor_without_hook_still_runs(self):
        receipt = run_jobs(self.root, self.game, PlainExecutor({"power": [WAIT]}), seconds=0.05)
        self.assertEqual(receipt["idle_actions"], 0)

    def test_ordinary_planning_failure_is_logged_and_swallowed(self):
        executor = Executor({"power": [WAIT]}, idle=[ValueError("unknown technology: ghost")])
        receipt = run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(receipt["idle_actions"], 0)
        errors = [row for row in self.events() if row["event"] == "action_error"]
        self.assertIn("ghost", errors[0]["error"])

    def test_ordinary_act_failure_is_logged_and_swallowed(self):
        self.game.act_error = RuntimeError("research rejected")
        executor = Executor({"power": [WAIT]}, idle=[RESEARCH])
        receipt = run_jobs(self.root, self.game, executor, seconds=0.05)
        self.assertEqual(receipt["idle_actions"], 0)
        self.assertEqual([row["event"] for row in self.events()], ["action_intent", "action_error"])

    def test_timeout_propagates(self):
        self.game.act_error = TimeoutError("server outcome unknown")
        executor = Executor({"power": [WAIT]}, idle=[RESEARCH])
        with self.assertRaises(TimeoutError):
            run_jobs(self.root, self.game, executor, seconds=0.05)

    def test_rcon_error_propagates_although_it_is_a_runtime_error(self):
        executor = Executor({"power": [WAIT]}, idle=[RconError("RCON connection closed")])
        with self.assertRaises(RconError):
            run_jobs(self.root, self.game, executor, seconds=0.05)


def tech(prerequisites=(), ingredients=(RED,), unit_count=10, unlocks=()):
    return {
        "prerequisites": list(prerequisites),
        "ingredients": list(ingredients),
        "unit_count": unit_count,
        "unlocks": list(unlocks),
    }


class Supply:
    def __init__(self, ok=True):
        self.ok = ok
        self.state = {}

    def proof(self, obs):
        return {"science_units": 5} if self.ok else None


class IdleActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = SimpleNamespace(
            fingerprint="catalog",
            technologies={
                "cheap": tech(unit_count=1),
                "drill-tech": tech(unit_count=50, unlocks=["electric-mining-drill"]),
                "green-tech": tech(ingredients=(RED, GREEN), unit_count=1),
            },
        )
        self.science, self.green = Supply(), Supply(ok=False)
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(self.root)))
        self.executors = object.__new__(StarterExecutors)
        self.executors.game = game
        self.executors.catalog = self.catalog
        self.executors.normal_research = NormalResearch(game, self.catalog, self.science, self.green)
        self.jobs = {}
        self.write_jobs()

    def test_burner_fuel_upkeep_comes_before_research_and_runs_while_research_is_active(self):
        fuel = {"type": "insert", "item": "coal", "count": 8}
        seen = []

        def maintain_fuel(observation):
            seen.append(observation)
            return fuel

        self.executors.bootstrap = SimpleNamespace(maintain_fuel=maintain_fuel)
        self.executors.starter_observation = lambda obs: obs
        self.assertEqual(self.executors.idle_action(self.obs(research="cheap")), fuel)
        self.assertEqual(len(seen), 1)
        # A plain wait from fuel upkeep is not an action; research selection follows.
        self.executors.bootstrap = SimpleNamespace(maintain_fuel=lambda observation: {"status": "waiting"})
        self.assertIsNone(self.executors.idle_action(self.obs(research="cheap")))

    def write_jobs(self, *rows):
        jobs = {f"j{index}": dict(row, world_id="world") for index, row in enumerate(rows)}
        ledger = {"schema_version": 2, "world_id": "world", "jobs": jobs, "last_tick": 0}
        (self.root / "jobs.json").write_text(json.dumps(ledger), encoding="utf-8")

    def obs(self, tick=1000, **extra):
        production = {name: {"consumed": 0} for name in ("automation-science-pack", "logistic-science-pack")}
        return {"world_id": "world", "tick": tick, "technologies": {}, "production": production, **extra}

    def roadmap_done_through(self, step_id):
        rows = []
        for step in progression.ROADMAP:
            rows.append({"capability": step.capability, "parameters": dict(step.parameters), "status": "succeeded"})
            if step.id == step_id:
                return rows
        raise AssertionError(step_id)

    def test_fallback_starts_cheapest_supplied_research_with_the_research_action_shape(self):
        action = self.executors.idle_action(self.obs())
        expected = self.executors.normal_research.step(self.obs(tick=1000), {"technology": "cheap"})
        self.assertEqual(action, expected)
        self.assertEqual(action["type"], "research")
        self.assertEqual(action["technology"], "cheap")

    def test_running_research_gives_none(self):
        self.assertIsNone(self.executors.idle_action(self.obs(research="cheap")))

    def test_unresolved_pending_action_gives_none(self):
        self.write_jobs({"capability": "power", "parameters": {}, "status": "running", "pending_action": {}})
        self.assertIsNone(self.executors.idle_action(self.obs()))

    def test_active_job_owning_research_gives_none(self):
        self.write_jobs({"capability": "research", "parameters": {"technology": "x"}, "status": "waiting"})
        self.assertIsNone(self.executors.idle_action(self.obs()))

    def test_missing_or_foreign_ledger_gives_none(self):
        (self.root / "jobs.json").unlink()
        self.assertIsNone(self.executors.idle_action(self.obs()))
        self.write_jobs()
        self.assertIsNone(self.executors.idle_action(self.obs(world_id="other")))

    def test_no_proven_lab_supply_gives_none(self):
        self.science.ok = False
        self.assertIsNone(self.executors.idle_action(self.obs()))

    def test_unsupplied_pack_is_never_chosen(self):
        del self.catalog.technologies["cheap"]
        del self.catalog.technologies["drill-tech"]
        self.assertIsNone(self.executors.idle_action(self.obs()))
        self.green.ok = True
        action = self.executors.idle_action(self.obs(tick=1000 + IDLE_RESEARCH_INTERVAL_TICKS))
        self.assertEqual(action["technology"], "green-tech")

    def test_roadmap_recipes_steer_the_choice(self):
        self.write_jobs(*self.roadmap_done_through("bootstrap_logistics"))
        action = self.executors.idle_action(self.obs())
        self.assertEqual(action["technology"], "drill-tech")

    def test_roadmap_research_steps_steer_the_choice(self):
        self.catalog.technologies["steel-processing"] = tech(unit_count=40)
        self.catalog.technologies["logistic-science-pack"] = tech(unit_count=40)
        self.write_jobs(*self.roadmap_done_through("research_logistic_science_pack"))
        action = self.executors.idle_action(self.obs())
        self.assertEqual(action["technology"], "steel-processing")

    def test_attempts_are_rate_limited_by_game_tick(self):
        self.assertIsNotNone(self.executors.idle_action(self.obs(tick=1000)))
        self.assertIsNone(self.executors.idle_action(self.obs(tick=1000 + IDLE_RESEARCH_INTERVAL_TICKS - 1)))
        self.assertIsNotNone(self.executors.idle_action(self.obs(tick=1000 + IDLE_RESEARCH_INTERVAL_TICKS)))

    def test_a_none_result_also_counts_as_an_attempt(self):
        self.science.ok = False
        self.assertIsNone(self.executors.idle_action(self.obs(tick=1000)))
        self.science.ok = True
        self.assertIsNone(self.executors.idle_action(self.obs(tick=1100)))
        self.assertIsNotNone(self.executors.idle_action(self.obs(tick=1000 + IDLE_RESEARCH_INTERVAL_TICKS)))


if __name__ == "__main__":
    unittest.main()
