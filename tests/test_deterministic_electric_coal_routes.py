"""Electric coal transfer modeling; opt-in Lua uses inert tables, never entities."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config
from tests import test_deterministic_shared_coal as fixtures


class ElectricCoalFixture:
    def setUp(self):
        fixtures.SharedCoalTests.setUp(self)
        self.energy.game.query.side_effect = self.survey

    def electric(self, name="fast-inserter", rate=120):
        self.branch.update(name=name, coal_per_minute=rate)
        self.obs["entities"] = deepcopy(self.rows)

    def survey(self, body):
        # Respect the real request census: a name omitted from either production
        # whitelist cannot be reintroduced by this fixture's query response.
        literal = re.search(r'helpers.json_to_table\((".*?")\);local pole=', body).group(1)
        self.request = json.loads(json.loads(literal))
        identities = {(r["name"], r["unit_number"], r["direction"]) for r in self.request["rows"]}
        return {"ok": True, "world_id": "coal-test", "rows": deepcopy([
            r for r in self.rows if (r["name"], r["unit_number"], r["direction"]) in identities])}

    def capacity(self):
        self.evidence["coal_routes"] = self.energy._coal_routes(self.obs)
        return self.energy.capacity(self.evidence)


class ElectricCoalRouteTests(ElectricCoalFixture, unittest.TestCase):
    def test_owned_basic_and_fast_arm_survive_census_tail_and_reload(self):
        for name in ("inserter", "fast-inserter"):
            with self.subTest(name=name):
                self.setUp()
                self.electric(name)
                before = deepcopy(self.energy.state)
                result = self.capacity()
                self.assertTrue(any(r["name"] == name for r in self.request["rows"]))
                self.assertIn(self.branch, self.evidence["coal_routes"]["0"]["1"])
                intact, intakes = self.energy._coal_transit(self.energy._coal_observation, 1)
                self.assertIn(self.branch, self.energy._coal_tail({"x": .5, "y": .5}, intact, intakes))
                self.assertEqual(self.energy.state, before)
                self.energy.state = json.loads(json.dumps(self.energy.state))
                self.assertEqual(self.capacity(), result)

    def test_electric_arm_spends_no_coal_but_both_boiler_burners_still_do(self):
        before = self.capacity()
        self.electric()
        after = self.capacity()
        # Four sources each pay drill150 + self-feed144; two distinct boiler
        # burners pay144 each. Replacing a branch removes only its own144.
        self.assertEqual(before["total_kw"], 2392)
        self.assertEqual(after["total_kw"], 4 * (1000 - 150 - 144) - 2 * 144)
        self.assertEqual(after["total_kw"] - before["total_kw"], 144)
        self.assertEqual(after["fuel_backed_kw"], [1800, 736])

    def test_electric_transit_cap_still_reserves_downstream_burner_fuel(self):
        self.electric(rate=9)
        result = self.capacity()
        self.assertEqual(result["fuel_backed_kw"][1], 9 * 4000000 / 60000 - 144)
        self.assertEqual(result["fuel_backed_kw"][0], 1800)

    def test_live_proof_exclusion_disconnects_capacity_without_charging_remote_loss(self):
        self.electric()
        self.rows.remove(self.branch)
        result = self.capacity()
        self.assertTrue(all("1" not in paths for paths in self.evidence["coal_routes"].values()))
        self.assertEqual(result["fuel_backed_kw"], [1800, 0])

    def test_changed_observation_direction_or_unit_cannot_restore_electric_link(self):
        for field, value in (("direction", 4), ("unit_number", 9999)):
            with self.subTest(field=field):
                self.setUp()
                self.electric()
                next(r for r in self.obs["entities"] if r["name"] == "fast-inserter")[field] = value
                self.assertEqual(self.capacity()["fuel_backed_kw"], [1800, 0])


SHADOW = r'''
local game=nil;local storage=nil
local x=helpers.json_to_table(FIXTURE);local o=x.options
local d={world_id="coal-test"};local f={};local foreign={};local s={}
local prototypes={item={coal={fuel_value=4000000}}};local entities={}
local function pos(p) return {x=p.x,y=p.y} end
for _,row in ipairs(x.rows) do
 local e={name=row.name,position=row.position,direction=row.direction,unit_number=row.unit_number,
  force=f,electric_network_id=1,energy=100,held_stack={valid_for_read=false},
  prototype={belt_speed=.03125,get_inserter_rotation_speed=function() return .014 end,
   get_inserter_extension_speed=function() return .04 end}}
 if row.name=="transport-belt" then
  e.type="transport-belt";e.belt_neighbours={outputs={}}
  e.get_transport_line=function() return {get_contents=function() return {} end} end
 elseif string.find(row.name,"inserter") then
  e.type="inserter";e.pickup_position=row.pickup_position;e.drop_position=row.drop_position
  if row.name=="burner-inserter" then
   e.burner={remaining_burning_fuel=4000000,inventory={get_item_count=function() return 1 end}}
  end
 elseif row.name=="burner-mining-drill" then e.type="mining-drill";e.drop_position=row.drop_position
 elseif row.name=="steam-engine" then e.type="generator" end
 if row.name==x.arm_name then
  if o.unpowered then e.energy=0 end
  if o.network then e.electric_network_id=2 end
  if o.foreign then e.force=foreign end
  if o.unit then e.unit_number=e.unit_number+999 end
  if o.direction then e.direction=4 end
  if o.contaminated then e.held_stack={valid_for_read=true,name="iron-plate"} end
  if o.kinematics then e.prototype.get_inserter_rotation_speed=function() return 0 end end
 end
 entities[#entities+1]=e
end
local function target(p,name)
 for _,e in ipairs(entities) do if e.name==name and e.position.x==p.x and e.position.y==p.y then return e end end
end
for _,e in ipairs(entities) do
 if e.type=="transport-belt" then
  for _,other in ipairs(entities) do
   if other.type=="transport-belt" and other.position.x==e.position.x+1 and other.position.y==e.position.y then
    e.belt_neighbours.outputs[#e.belt_neighbours.outputs+1]=other
   end
  end
 end
end
s.find_entities_filtered=function(args)
 local out={};assert(args.name=="boiler" and args.force==f)
 for _,e in ipairs(entities) do if e.name=="boiler" and math.abs(e.position.x-args.position.x)<.5
  and math.abs(e.position.y-args.position.y)<.5 then out[#out+1]=e end end
 return out
end
'''


@unittest.skipUnless(os.environ.get("FACTORIO_ELECTRIC_COAL_LUA_TEST") == "1", "requires explicit inert Lua test opt-in")
class ElectricCoalLuaTests(ElectricCoalFixture, unittest.TestCase):
    def execute(self, name, options):
        self.electric(name)
        self.energy._coal_routes(self.obs)
        body = self.energy.game.query.call_args.args[0]
        fixture = {"rows": self.rows, "arm_name": name, "options": options}
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary), server_port=34214,
                rcon_port=int(os.environ.get("FACTORIO_ELECTRIC_COAL_RCON_PORT", "27029"))))
            with patch.object(deterministic_game, "_HELPERS", ""):
                proof = game.query(SHADOW.replace("FIXTURE", json.dumps(json.dumps(fixture))) + body)
        self.assertTrue(proof["ok"], proof)
        self.energy.game.query.side_effect = lambda body: deepcopy(proof)
        return proof, self.capacity()

    def test_actual_emitted_lua_accepts_owned_powered_pure_basic_and_fast(self):
        for name in ("inserter", "fast-inserter"):
            with self.subTest(name=name):
                self.setUp()
                proof, result = self.execute(name, {})
                self.assertTrue(any(r["name"] == name for r in proof["rows"]))
                self.assertEqual(result["fuel_backed_kw"], [1800, 736])

    def test_actual_emitted_lua_rejects_power_identity_purity_and_kinematics_failures(self):
        for name in ("inserter", "fast-inserter"):
            for failure in ("unpowered", "network", "foreign", "unit", "direction", "contaminated", "kinematics"):
                with self.subTest(name=name, failure=failure):
                    self.setUp()
                    proof, result = self.execute(name, {failure: True})
                    self.assertFalse(any(r["name"] == name for r in proof["rows"]))
                    self.assertEqual(result["fuel_backed_kw"], [1800, 0])


if __name__ == "__main__":
    unittest.main()
