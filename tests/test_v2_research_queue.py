"""Native research queue: policy order, action envelope, Lua body, roadmap steps and planners."""

import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent import progression
from factorio_agent.core.deterministic_game import DeterministicGame
from factorio_agent.normal_research import NormalResearch
from factorio_agent.research_capacity import queue_targets
from factorio_agent.research_queue import (
    RESEARCH_QUEUE_SIZE,
    native_queue,
    plan_queue,
    queue_action,
    validate_queue_action,
)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None

RED = {"name": "automation-science-pack", "amount": 1}
GREEN = {"name": "logistic-science-pack", "amount": 1}


def tech(prerequisites=(), ingredients=(RED,), unit_count=10, **extra):
    return {
        "prerequisites": list(prerequisites),
        "ingredients": list(ingredients),
        "unit_count": unit_count,
        "unit_energy": 600,
        "unlocks": [],
        **extra,
    }


def catalog(**technologies):
    return SimpleNamespace(fingerprint="f", technologies=technologies)


def obs(**extra):
    production = {name: {"consumed": 0} for name in ("automation-science-pack", "logistic-science-pack")}
    return {"world_id": "w", "tick": 10, "technologies": {}, "production": production, **extra}


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog(
            cheap=tech(unit_count=1),
            base=tech(unit_count=30),
            wanted=tech(prerequisites=["base"], unit_count=50),
            green=tech(ingredients=(RED, GREEN), unit_count=1),
            liked=tech(unit_count=40),
            trigger=tech(research_trigger={"type": "craft-item"}),
            after_trigger=tech(prerequisites=["trigger"], unit_count=1),
        )

    def plan(self, observation=None, **kwargs):
        kwargs.setdefault("supplied_packs", {"automation-science-pack"})
        return plan_queue(self.catalog, observation or obs(), **kwargs)

    def test_roadmap_first_with_prerequisites_before_then_preferences_then_cheapest(self):
        plan = self.plan(wanted_technologies=["wanted"], preferences=["liked"])
        self.assertEqual(plan, ["base", "wanted", "liked", "cheap"])

    def test_running_research_stays_first(self):
        plan = self.plan(obs(research="liked"), wanted_technologies=["wanted"])
        self.assertEqual(plan[:3], ["liked", "base", "wanted"])

    def test_unsupplied_packs_and_triggers_are_never_queued(self):
        plan = self.plan(wanted_technologies=["green", "after_trigger"])
        self.assertNotIn("green", plan)
        self.assertNotIn("trigger", plan)
        self.assertNotIn("after_trigger", plan)
        self.assertIn("green", self.plan(supplied_packs={"automation-science-pack", "logistic-science-pack"}))

    def test_no_supplied_packs_means_no_queue(self):
        self.assertEqual(self.plan(supplied_packs=set(), wanted_technologies=["wanted"]), [])

    def test_plan_is_bounded_and_unique(self):
        many = catalog(**{f"t{index:02d}": tech(unit_count=index + 1) for index in range(20)})
        plan = plan_queue(many, obs(), supplied_packs={"automation-science-pack"})
        self.assertEqual(len(plan), RESEARCH_QUEUE_SIZE)
        self.assertEqual(len(set(plan)), len(plan))
        self.assertEqual(plan[0], "t00")

    def test_researched_technologies_are_skipped(self):
        observation = obs(technologies={"base": True, "cheap": True})
        self.assertEqual(self.plan(observation, wanted_technologies=["wanted"])[0], "wanted")


class ActionTests(unittest.TestCase):
    def test_no_duplicate_enqueue(self):
        observation = obs(research="a", research_queue=["a", "b"])
        self.assertIsNone(queue_action(observation, ["a", "b"], reason="r"))
        action = queue_action(observation, ["a", "b", "c"], reason="r")
        self.assertEqual(action["technologies"], ["b", "c"])

    def test_full_queue_only_takes_a_roadmap_step(self):
        queue = [f"t{index}" for index in range(RESEARCH_QUEUE_SIZE)]
        observation = obs(research=queue[0], research_queue=queue)
        self.assertIsNone(queue_action(observation, [queue[0], "new"], reason="r"))
        self.assertEqual(
            queue_action(observation, [queue[0], "new"], reason="r", require_room=False)["technologies"], ["new"]
        )

    def test_native_queue_includes_current_research(self):
        self.assertEqual(native_queue({"research": "a"}), ["a"])
        self.assertEqual(native_queue({"research": "a", "research_queue": ["a", "b"]}), ["a", "b"])
        self.assertEqual(native_queue({"research_queue": {}}), [])

    def test_envelope_is_validated_before_any_query(self):
        for bad in (
            {"type": "research_queue", "technologies": []},
            {"type": "research_queue", "technologies": ["a", "a"]},
            {"type": "research_queue", "technologies": ["Bad name"]},
            {"type": "research_queue", "technologies": ["a"] * (RESEARCH_QUEUE_SIZE + 1)},
            {"type": "research_queue", "technologies": ["a"], "count": 1},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_queue_action(bad)
        game = DeterministicGame.__new__(DeterministicGame)
        game.backend = "assisted"
        game.query = Mock()
        with self.assertRaises(ValueError):
            game.act({"type": "research_queue", "technologies": []})
        game.query.assert_not_called()


class RoadmapStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.temp.name))
        self.research = NormalResearch(
            game,
            catalog(**{"steel-processing": tech(), "fast-inserter": tech(), "other": tech()}),
            SimpleNamespace(proof=lambda o: {"science_units": [1]}),
        )

    def test_step_queues_its_technology_behind_the_running_one(self):
        action = self.research.step(
            obs(research="other", research_queue=["other"]),
            {"technology": "steel-processing"},
            plan=["other", "steel-processing", "fast-inserter"],
        )
        self.assertEqual(action["type"], "research_queue")
        self.assertEqual(action["technologies"], ["steel-processing", "fast-inserter"])

    def test_step_waits_while_its_technology_sits_in_the_queue(self):
        result = self.research.step(
            obs(research="other", research_queue=["other", "steel-processing"]), {"technology": "steel-processing"}
        )
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["evidence"]["queue_position"], 1)

    def test_step_succeeds_once_researched_by_the_queue(self):
        observation = obs(research_queue=["other"])
        self.research.step(observation, {"technology": "steel-processing"})
        observation = obs(technologies={"steel-processing": True})
        observation["production"]["automation-science-pack"]["consumed"] = 10
        result = self.research.step(observation, {"technology": "steel-processing"})
        self.assertEqual(result["status"], "succeeded")

    def test_autopilot_proposes_no_job_for_a_natively_queued_research_step(self):
        step = next(step for step in progression.ROADMAP if step.id == "research_steel_processing")
        self.assertTrue(progression._natively_queued(step, {"research_queue": ["a", "steel-processing"]}))
        self.assertTrue(progression._natively_queued(step, {"research": "steel-processing"}))
        self.assertFalse(progression._natively_queued(step, {"research_queue": ["a"]}))
        power = next(step for step in progression.ROADMAP if step.id == "power")
        self.assertFalse(progression._natively_queued(power, {"research_queue": ["a"]}))


class PlannerTargetTests(unittest.TestCase):
    def test_capacity_targets_follow_the_native_queue(self):
        known = catalog(a=tech(), b=tech(), c=tech())
        targets = queue_targets(known, [], {"research": "a", "research_queue": ["a", "c", "b"], "technologies": {}})
        self.assertEqual([target.technology for target in targets], ["a", "c", "b"])


@unittest.skipUnless(LuaRuntime, "optional Lua runtime: pip install lupa")
class NativeQueueLuaTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)

        def table(value):
            if isinstance(value, dict):
                return self.lua.table_from({k: table(v) for k, v in value.items()})
            if isinstance(value, list):
                return self.lua.table_from([table(v) for v in value])
            return value

        def decode(value):
            if not hasattr(value, "items"):
                return value
            keys = list(value.keys())
            if keys and all(type(k) is int for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
                return [decode(value[i]) for i in range(1, len(keys) + 1)]
            return {k: decode(v) for k, v in value.items()}

        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.execute(
            """
function success(t) t.ok=true return t end
function failure(r) return {ok=false,reason=r} end
local function T(name,prereqs) return {name=name,researched=false,enabled=true,prototype={},prerequisites=prereqs or {}} end
f={technologies={}}
for _,n in ipairs({"a","b","c","d"}) do f.technologies[n]=T(n) end
f.technologies.done=T("done");f.technologies.done.researched=true
f.technologies.child=T("child",{c=f.technologies.c})
f.technologies.trig=T("trig");f.technologies.trig.prototype.research_trigger={type="craft-item"}
f.research_queue={f.technologies.a,f.technologies.b}
f.current_research=f.technologies.a
writes=0
a={valid=true,get_main_inventory=function() return {} end}
"""
        )
        self.game = DeterministicGame.__new__(DeterministicGame)
        self.game.backend = "assisted"
        self.game._record_action = lambda action, result: result
        self.game.query = lambda script: decode(self.lua.execute("return(function() " + script + " end)()"))
        # The writer stores names; read-back maps them to technology objects.
        self.lua.execute(
            """
local raw=f
local queue=raw.research_queue;raw.research_queue=nil
setmetatable(raw,{__index=function(t,k) if k=="research_queue" then return queue end end,
 __newindex=function(t,k,v) if k=="research_queue" then writes=writes+1;queue={};for i,n in ipairs(v) do queue[i]=raw.technologies[n] end else rawset(t,k,v) end end})
"""
        )

    def act(self, names):
        return self.game.act({"type": "research_queue", "technologies": names, "reason": "test"})

    def test_current_stays_first_new_entries_follow_and_the_rest_is_kept(self):
        result = self.act(["c", "child"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["research_queue"], ["a", "c", "child", "b"])
        self.assertEqual(result["before"], ["a", "b"])

    def test_unresearchable_entries_are_skipped_not_failed(self):
        result = self.act(["child", "done", "trig", "missing", "d"])
        self.assertEqual(result["research_queue"], ["a", "d", "b"])
        self.assertEqual(sorted(result["skipped"]), ["child", "done", "missing", "trig"])

    def test_unchanged_queue_is_not_rewritten(self):
        result = self.act(["b"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.lua.eval("writes"), 0)
        self.assertEqual(result["research_queue"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
