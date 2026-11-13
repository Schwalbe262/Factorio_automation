"""Drill-cell surveys: one folded query, reused within a pass, invalidated by actions, ticks and worlds."""

import json
from pathlib import Path
import tempfile
import unittest

from factorio_agent.core.deterministic_bootstrap import (
    EXISTING_CELLS_LUA,
    SURVEY_LAYOUT_TICKS,
    SURVEY_SAME_PASS,
    DeterministicBootstrap,
)
from factorio_agent.core.deterministic_game import counts_effect
from factorio_agent.jobs import Ledger, Request, run_jobs, run_slice, submit

try:
    from lupa import LuaRuntime
except ImportError:  # pragma: no cover - optional dependency
    LuaRuntime = None

RESOURCES = ("coal", "iron-ore", "copper-ore", "stone")

# The per-resource survey this query replaced (2178ca9), kept as the reference it must match.
PER_RESOURCE_LUA = """
local cells={}
for _,e in pairs(s.find_entities_filtered{force=f,type="mining-drill"}) do
 local target_name=nil
 local good,t=pcall(function() return e.mining_target end)
 if good and t and t.valid then target_name=t.name end
 local counts={};local best=0;local kinds=0
 local radius=e.prototype.mining_drill_radius
 for _,ore in pairs(s.find_entities_filtered{area={{e.position.x-radius,e.position.y-radius},
      {e.position.x+radius,e.position.y+radius}},type="resource"}) do
  if math.abs(ore.position.x-e.position.x)<=radius and math.abs(ore.position.y-e.position.y)<=radius and ore.amount>0 then
   if not counts[ore.name] then kinds=kinds+1 end
   counts[ore.name]=(counts[ore.name] or 0)+ore.amount
   if counts[ore.name]>best then best=counts[ore.name];if not good or not t or not t.valid then target_name=ore.name end end
  end
 end
 if target_name==resource and kinds==1 then
  local receiver=nil;local direct_belt=false
  for _,candidate in pairs(s.find_entities_filtered{position=e.drop_position,radius=1.5,force=f}) do
   if candidate.type=="furnace" or candidate.type=="container" then
    local box=candidate.bounding_box;local p=e.drop_position
    if math.abs(p.x-candidate.position.x)<=candidate.prototype.tile_width/2
     and math.abs(p.y-candidate.position.y)<=candidate.prototype.tile_height/2 then receiver=candidate;break end
   elseif candidate.type=="transport-belt" or candidate.type=="underground-belt" then
    local b=candidate.bounding_box;local p=e.drop_position
    if p.x>=b.left_top.x and p.x<b.right_bottom.x and p.y>=b.left_top.y and p.y<b.right_bottom.y then direct_belt=true end
   end
  end
  local burner=e.burner
  cells[#cells+1]={drill={name=e.name,position=pos(e.position),direction=e.direction},drop_position=pos(e.drop_position),
   receiver=receiver and {name=receiver.name,position=pos(receiver.position)} or nil,direct_belt=direct_belt,
   fuel=e.get_fuel_inventory() and e.get_fuel_inventory().get_item_count("coal") or 0,
   burning=burner and burner.remaining_burning_fuel>0 or false,electric=burner==nil,
   operating=burner==nil and e.energy>0 and e.is_connected_to_electric_network() or false,
   remaining=counts[resource] or 0}
 end
end
return {ok=true,cells=cells}
"""


def cell(name, x):
    return {
        "drill": {"name": "burner-mining-drill", "position": {"x": x, "y": 0}, "direction": 0},
        "drop_position": {"x": x, "y": -1},
        "receiver": {"name": name, "position": {"x": x, "y": -2}},
        "direct_belt": False,
        "fuel": 1,
        "burning": True,
        "electric": False,
        "operating": False,
        "remaining": 500,
    }


SURVEY = {
    "ok": True,
    "cells_by_resource": {
        "coal": [cell("wooden-chest", 1)],
        "iron-ore": [cell("stone-furnace", 3), cell("stone-furnace", 5)],
        "copper-ore": [cell("stone-furnace", 7)],
    },
}


class Game:
    def __init__(self, world="w"):
        self.world = world
        self.tick = 100
        self.queries = 0
        self.act_epoch = 0
        self.actions = []
        self.observations = 0
        self.saved = 0
        self.survey = json.loads(json.dumps(SURVEY))

    def query(self, body):
        assert body == EXISTING_CELLS_LUA
        self.queries += 1
        return json.loads(json.dumps(self.survey))

    def observe(self):
        self.observations += 1
        return {"ok": True, "world_id": self.world, "tick": self.tick, "entities": [], "production": {}}

    @counts_effect
    def act(self, action):
        self.actions.append(action)
        if action.get("fail"):
            raise TimeoutError("server outcome unknown")
        self.tick += 1
        return {"ok": True}

    def save(self):
        self.saved += 1


def obs(tick, world="w"):
    return {"world_id": world, "tick": tick}


class SurveyReuseTests(unittest.TestCase):
    def setUp(self):
        self.game = Game()
        self.bootstrap = DeterministicBootstrap(self.game)

    def survey_all(self, **kw):
        return {r: self.bootstrap._existing_cells(r, **kw) for r in RESOURCES}

    def test_outside_a_slice_every_call_queries_as_before(self):
        self.survey_all()
        self.survey_all()
        self.assertEqual(self.game.queries, 8)

    def test_four_resources_of_one_pass_share_one_query(self):
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            first = self.survey_all()
            again = self.survey_all()
        self.assertEqual(self.game.queries, 1)
        self.assertEqual(first, again)
        self.assertEqual(len(first["iron-ore"]["cells"]), 2)
        self.assertEqual(first["coal"]["cells"][0]["receiver"]["name"], "wooden-chest")
        self.assertEqual(first["stone"], {"ok": True, "cells": []})

    def test_callers_get_independent_copies(self):
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            self.bootstrap._existing_cells("coal")["cells"][0]["fuel"] = 99
            self.assertEqual(self.bootstrap._existing_cells("coal")["cells"][0]["fuel"], 1)

    def test_an_action_invalidates(self):
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            self.bootstrap._existing_cells("coal")
            self.game.act({"type": "insert"})
            self.bootstrap._existing_cells("coal", max_age_ticks=SURVEY_LAYOUT_TICKS)
            self.assertEqual(self.game.queries, 2)

    def test_an_action_with_unknown_outcome_invalidates(self):
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            self.bootstrap._existing_cells("coal")
            with self.assertRaises(TimeoutError):
                self.game.act({"type": "insert", "fail": True})
            self.bootstrap._existing_cells("coal")
            self.assertEqual(self.game.queries, 2)

    def test_tick_advance_invalidates_by_the_callers_freshness(self):
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            self.bootstrap._existing_cells("coal")
            self.bootstrap.begin_survey_pass(obs(101))
            # A layout planner accepts the structure of a pass 1 tick old ...
            self.bootstrap._existing_cells("coal", max_age_ticks=SURVEY_LAYOUT_TICKS)
            self.assertEqual(self.game.queries, 1)
            # ... a fuel or wait decision needs the survey of its own pass.
            self.bootstrap._existing_cells("coal", max_age_ticks=SURVEY_SAME_PASS)
            self.assertEqual(self.game.queries, 2)
            self.bootstrap.begin_survey_pass(obs(101 + SURVEY_LAYOUT_TICKS + 1))
            self.bootstrap._existing_cells("coal", max_age_ticks=SURVEY_LAYOUT_TICKS)
            self.assertEqual(self.game.queries, 3)

    def test_tick_rollback_invalidates(self):
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            self.bootstrap._existing_cells("coal")
            self.bootstrap.begin_survey_pass(obs(99))
            self.bootstrap._existing_cells("coal", max_age_ticks=SURVEY_LAYOUT_TICKS)
            self.assertEqual(self.game.queries, 2)

    def test_a_different_world_invalidates(self):
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            self.bootstrap._existing_cells("coal")
            self.bootstrap.begin_survey_pass(obs(100, world="other"))
            self.bootstrap._existing_cells("coal", max_age_ticks=SURVEY_LAYOUT_TICKS)
            self.assertEqual(self.game.queries, 2)

    def test_slice_exit_drops_the_cache(self):
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            self.bootstrap._existing_cells("coal")
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            self.bootstrap._existing_cells("coal")
        self.assertEqual(self.game.queries, 2)

    def test_failed_survey_is_returned_and_never_reused(self):
        self.game.survey = {"ok": False, "reason": "bound_world_changed"}
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            self.assertEqual(self.bootstrap._existing_cells("coal"), self.game.survey)
            self.bootstrap._existing_cells("coal")
        self.assertEqual(self.game.queries, 2)

    def test_a_game_that_does_not_count_actions_is_never_cached(self):
        del self.game.act_epoch
        with self.bootstrap.survey_slice():
            self.bootstrap.begin_survey_pass(obs(100))
            self.survey_all()
        self.assertEqual(self.game.queries, 4)

    def test_empty_survey_is_an_empty_list(self):
        self.game.survey = {"ok": True, "cells_by_resource": []}  # Lua's empty table
        self.assertEqual(self.bootstrap._existing_cells("coal"), {"ok": True, "cells": []})


class SurveyExecutor:
    """Every step surveys all four resources, as maintain_fuel does per job step."""

    def __init__(self, bootstrap, scripts):
        self.bootstrap = bootstrap
        self.scripts = {capability: list(results) for capability, results in scripts.items()}
        self.per_pass = []

    def survey_slice(self):
        return self.bootstrap.survey_slice()

    def begin_survey_pass(self, obs):
        self.bootstrap.begin_survey_pass(obs)

    def step(self, capability, obs, parameters=None):
        for resource in RESOURCES:
            self.bootstrap._existing_cells(resource)
        self.per_pass.append((capability, obs["tick"], self.bootstrap.game.queries))
        script = self.scripts[capability]
        return script.pop(0) if len(script) > 1 else script[0]

    def proof(self, capability, obs, result, parameters=None):
        return {"ok": True}


class RunJobsSurveyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = Game()
        self.bootstrap = DeterministicBootstrap(self.game)
        self.submit("starter", "starter")
        run_slice(
            self.root, self.game, SurveyExecutor(self.bootstrap, {"starter": [{"status": "succeeded"}]}), "starter"
        )

    def submit(self, key, capability):
        request = Request(key, "world", self.game.tick, capability, {})
        self.game.world = "world"
        return submit(self.root, self.game, request)

    def test_jobs_of_one_pass_share_a_survey_and_an_action_forces_a_new_one(self):
        self.submit("a", "power")
        self.submit("b", "defense_perimeter")
        act = {"type": "craft", "recipe": "iron-gear-wheel", "count": 1}
        wait = {"status": "waiting", "reason": "crafting_queue_busy"}
        executor = SurveyExecutor(self.bootstrap, {"power": [wait], "defense_perimeter": [act, wait]})
        before = self.game.queries
        run_jobs(self.root, self.game, executor, seconds=0.3)
        passes = sorted({tick for _, tick, _ in executor.per_pass})
        # Pass 1: both jobs (8 lookups) cost one query, then b acts (tick +1).
        # Every later pass again costs exactly one query for all its lookups.
        self.assertGreaterEqual(len(passes), 2)
        self.assertEqual(self.game.queries - before, len(passes))
        self.assertEqual(Ledger(self.root).data["jobs"]["b"]["actions"], 1)
        self.assertIsNone(self.bootstrap._cell_surveys)


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class ExistingCellsLuaTests(unittest.TestCase):
    """The folded query groups each single-resource drill under its resource, as the per-resource query did."""

    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute("""f={};pos=function(p)return{x=p.x,y=p.y}end
local function drill(x,fuel,burning)
 local e={name="burner-mining-drill",position={x=x,y=0},direction=0,drop_position={x=x,y=-1.3},
  prototype={mining_drill_radius=0.99},burner={remaining_burning_fuel=burning}}
 e.get_fuel_inventory=function()return{get_item_count=function()return fuel end}end
 e.mining_target=nil
 return e
end
drills={drill(0,2,0),drill(10,0,5),drill(20,0,0),drill(30,1,1)}
ores={{name="coal",position={x=0,y=0},amount=50},{name="iron-ore",position={x=10,y=0},amount=70},
 {name="copper-ore",position={x=20,y=0},amount=10},{name="stone",position={x=20,y=0.5},amount=10},
 {name="stone",position={x=30,y=0},amount=0}}
chest={type="container",name="wooden-chest",position={x=0,y=-1.5},prototype={tile_width=1,tile_height=1}}
belt={type="transport-belt",name="transport-belt",position={x=10.5,y=-1.5},
 bounding_box={left_top={x=9.5,y=-2},right_bottom={x=11,y=-1}},prototype={tile_width=1,tile_height=1}}
s={find_entities_filtered=function(q)
 if q.type=="mining-drill" then return drills end
 if q.type=="resource" then
  local out={}
  for _,o in ipairs(ores) do
   if o.position.x>=q.area[1][1] and o.position.x<=q.area[2][1] and o.position.y>=q.area[1][2]
    and o.position.y<=q.area[2][2] then out[#out+1]=o end
  end
  return out
 end
 local out={}
 for _,c in ipairs({chest,belt}) do
  if math.abs(c.position.x-q.position.x)<=q.radius and math.abs(c.position.y-q.position.y)<=q.radius then out[#out+1]=c end
 end
 return out
end}""")

    def run_lua(self):
        result = self.lua.execute(EXISTING_CELLS_LUA)
        return json.loads(json.dumps(_plain(result)))

    def test_single_resource_drills_are_grouped_and_mixed_or_empty_ones_dropped(self):
        result = self.run_lua()
        self.assertTrue(result["ok"])
        by = result["cells_by_resource"]
        self.assertEqual(sorted(by), ["coal", "iron-ore"])  # copper+stone mixed, depleted stone has no kind
        coal = by["coal"][0]
        self.assertEqual(coal["receiver"], {"name": "wooden-chest", "position": {"x": 0, "y": -1.5}})
        self.assertEqual((coal["fuel"], coal["burning"], coal["remaining"]), (2, False, 50))
        iron = by["iron-ore"][0]
        self.assertTrue(iron["direct_belt"])
        self.assertNotIn("receiver", iron)
        self.assertEqual((iron["fuel"], iron["burning"]), (0, True))

    def test_each_resource_matches_the_per_resource_query_it_replaced(self):
        by = self.run_lua()["cells_by_resource"]
        for resource in ("coal", "iron-ore", "copper-ore", "stone"):
            self.lua.execute("resource=" + json.dumps(resource))
            old = json.loads(json.dumps(_plain(self.lua.execute("local resource=resource " + PER_RESOURCE_LUA))))
            old_cells = old["cells"] if old["cells"] else []
            self.assertEqual(by.get(resource, []), old_cells, resource)


def _plain(value):
    """A lupa table as dicts (string keys) and lists (1..n keys)."""
    if hasattr(value, "items") and not isinstance(value, dict):
        items = dict(value.items())
        if items and all(isinstance(k, int) for k in items):
            return [_plain(items[k]) for k in sorted(items)]
        return {k: _plain(v) for k, v in items.items()}
    return value


if __name__ == "__main__":
    unittest.main()
