"""Opt-in exact emitted Lua, shadow entities only; no engine entity mutations."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config
from tests.test_deterministic_coal_replacement_guard import replacement_action


SHADOW = r'''
local storage=nil;local game=nil;local o=options
local request=helpers.json_to_table(ACTION);local q=request.coal_transit_replacement
local d={world_id=o.world or "coal-world"};local f={recipes={["fast-inserter"]={enabled=not o.recipe_disabled}}};local s={}
local counts={mined=0,started=0,slots=0};local rows={}
local function point(p) return {x=p.x or p[1],y=p.y or p[2]} end
local function proto(p)
 local result={type=p.type,inserter_pickup_position=point(p.inserter_pickup_position),
  inserter_drop_position=point(p.inserter_drop_position),
  collision_box={left_top=point(p.collision_box.left_top),right_bottom=point(p.collision_box.right_bottom)},
  electric_energy_source_prototype=p.electric_energy_source_prototype and true or nil,
  burner_prototype=p.burner_prototype and true or nil,mineable_properties={products={}}}
 for _,v in pairs(p.mineable_properties.products) do
  result.mineable_properties.products[#result.mineable_properties.products+1]={type=v.type,name=v.name,amount=v.amount,probability=v.probability}
 end
 return result
end
local oldproto=proto(prototypes.entity["burner-inserter"])
local fastproto=proto(prototypes.entity["fast-inserter"])
local prototypes={entity={["fast-inserter"]=fastproto}}
local function number(key,fallback) if o[key]~=nil then return o[key] end;return fallback end
local inv={valid=not o.invalid_inventory,
 get_item_count=function(spec) assert(spec.name=="fast-inserter" and spec.quality=="normal");return number("replacement_stock",1) end,
 count_empty_stacks=function(filtered,barred) assert(filtered==false and barred==false);counts.slots=counts.slots+1;return number("empty_slots",3) end,
 remove=function() error("no manual item removal") end,insert=function() error("no manual item insertion") end}
local a=setmetatable({valid=true,unit_number=o.actor_unit or 58,force=o.actor_foreign and {} or f,
 surface=o.actor_surface and {} or s,get_main_inventory=function() return inv end,
 mine_entity=function(e) assert(e.unit_number==3349);counts.mined=counts.mined+1;return not o.mine_fails end},
 {__newindex=function() error("no actor writes") end})
local function make(spec,kind)
 local p=point(spec.position)
 local e={valid=true,name=spec.name,type=kind,position=p,lookup=point(p),unit_number=spec.unit_number,direction=spec.direction,
  surface=s,force=f,quality={name="normal"},health=100,max_health=100,minable=true,
  bounding_box={left_top={x=p.x-.4,y=p.y-.4},right_bottom={x=p.x+.4,y=p.y+.4}},lines={}}
 e.get_transport_line=function(index) return {get_contents=function() return e.lines[index] or {} end} end
 rows[#rows+1]=e;return e
end
local old=make({name="burner-inserter",position=request.position,unit_number=3349,direction=12},"inserter")
old.prototype=oldproto;old.pickup_position={x=33.5,y=16.5};old.drop_position={x=35.69921875,y=16.5}
old.held_stack={valid_for_read=true,name="coal",quality={name="normal"},count=1}
local fuelrows={{name="coal",quality="normal",count=1}}
local burner={valid=true,inventory={valid=true,get_contents=function() return fuelrows end},
 burnt_result_inventory={valid=true,is_empty=function() return not o.burnt_result end},
 currently_burning={name={name="coal"},quality={name="normal"}},remaining_burning_fuel=number("remaining_energy",1512271)}
old.burner=burner
local pickup=make(q.pickup,"transport-belt");local drop=make(q.drop,"transport-belt");local pole=make(q.pole,"electric-pole")
pickup.lines[1]={{name="coal",quality="normal",count=7}};drop.lines[2]={{name="coal",quality="normal",count=2}}
pole.electric_network_id=number("pole_network",1)
pole.prototype={get_supply_area_distance=function(quality) assert(quality.name=="normal");return number("supply_reach",2.5) end}
local generator={valid=true,force=f,surface=s,quality={name="normal"},health=400,max_health=400,
 electric_network_id=number("generator_network",1),energy=number("generator_energy",100)}
s.find_entities_filtered=function(filter) assert(filter.force==f and filter.type=="generator");return {generator} end
if o.target_unit then old.unit_number=o.target_unit end
if o.target_direction then old.direction=o.target_direction end
if o.target_offset then old.position.x=old.position.x+.1 end
if o.target_foreign then old.force={} end
if o.target_surface then old.surface={} end
if o.target_damaged then old.health=99 end
if o.target_quality then old.quality.name=o.target_quality end
if o.target_unminable then old.minable=false end
if o.pickup_modified then old.pickup_position.x=33.25 end
if o.drop_modified then old.drop_position.y=16.6 end
if o.pickup_proto then fastproto.inserter_pickup_position.y=-2 end
if o.drop_proto then fastproto.inserter_drop_position.y=1 end
if o.collision_proto then fastproto.collision_box.left_top.x=-.5 end
if o.burner_proto then fastproto.burner_prototype=true end
if o.mining_product then oldproto.mineable_properties.products[1].amount=2 end
if o.endpoint then
 local target=({pickup=pickup,drop=drop,pole=pole})[o.endpoint]
 if o.endpoint_change=="missing" then target.valid=false
 elseif o.endpoint_change=="unit" then target.unit_number=999
 elseif o.endpoint_change=="direction" then target.direction=(target.direction+4)%16
 elseif o.endpoint_change=="offset" then target.position.x=target.position.x+.1
 elseif o.endpoint_change=="foreign" then target.force={}
 elseif o.endpoint_change=="surface" then target.surface={}
 elseif o.endpoint_change=="damaged" then target.health=99
 elseif o.endpoint_change=="quality" then target.quality.name="rare" end
end
if o.pickup_contaminated then pickup.lines[2]={{name="iron-plate",quality="normal",count=1}} end
if o.drop_quality then drop.lines[1]={{name="coal",quality="rare",count=1}} end
if o.generator_foreign then generator.force={} end
if o.generator_damaged then generator.health=399 end
if o.fuel_invalid then burner.inventory.valid=false end
if o.fuel_name then fuelrows[1].name=o.fuel_name end
if o.fuel_quality then fuelrows[1].quality=o.fuel_quality end
if o.fuel_count~=nil then fuelrows[1].count=o.fuel_count end
if o.held_name then old.held_stack.name=o.held_name end
if o.held_quality then old.held_stack.quality.name=o.held_quality end
if o.held_count~=nil then old.held_stack.count=o.held_count end
if o.burning_name then burner.currently_burning.name.name=o.burning_name end
if o.burning_quality then burner.currently_burning.quality.name=o.burning_quality end
local function target(position,name)
 for _,e in ipairs(rows) do if e.name==name and e.lookup.x==position.x and e.lookup.y==position.y then return e end end
end
local function success(row) row.ok=true;return row end
local function failure(reason) return {ok=false,reason=reason} end
'''


@unittest.skipUnless(os.environ.get("FACTORIO_COAL_REPLACEMENT_LUA_TEST") == "1", "requires inert fixture RCON opt-in")
class CoalReplacementLuaTests(unittest.TestCase):
    def execute(self, scenarios):
        action = replacement_action()
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary),
                rcon_port=int(os.environ.get("FACTORIO_COAL_REPLACEMENT_RCON_PORT", "27029"))))
            with patch.object(game, "query", return_value={"ok": True}) as query, patch.object(game, "_record_action"):
                game.act(action)
                body = query.call_args.args[0]
            shadow = SHADOW.replace("ACTION", json.dumps(json.dumps(action)))
            body = "local result=(function() " + body + " end)();result.counts=counts;result.remaining_energy=burner.remaining_burning_fuel;return result"
            batch = ("local scenarios=helpers.json_to_table(" + json.dumps(json.dumps(scenarios)) + ");local outcomes={};"
                     "for _,options in ipairs(scenarios) do outcomes[#outcomes+1]=(function() " + shadow + body
                     + " end)() end;return {outcomes=outcomes}")
            with patch.object(deterministic_game, "_HELPERS", ""):
                return game.query(batch)["outcomes"]

    def test_live_prototypes_allow_one_native_mine_and_never_convert_burning_energy(self):
        scenarios = [{}, {"remaining_energy": 4000000}, {"remaining_energy": 0}, {"mine_fails": True},
                     {"fuel_count": 2, "held_count": 3, "empty_slots": 6}]
        for options, result in zip(scenarios, self.execute(scenarios), strict=True):
            with self.subTest(options=options):
                self.assertEqual(result["ok"], not options.get("mine_fails", False), result)
                if options.get("mine_fails"):
                    self.assertEqual(result["reason"], "mining_failed")
                self.assertEqual(result["counts"], {"mined": 1, "started": 0, "slots": 1})
                self.assertEqual(result["remaining_energy"], options.get("remaining_energy", 1512271))

    def test_changed_identity_geometry_purity_power_or_joint_recovery_capacity_never_mines(self):
        scenarios = [{key: True} for key in ("actor_foreign", "actor_surface", "target_offset", "target_foreign",
            "target_surface", "target_damaged", "target_unminable", "pickup_modified", "drop_modified", "pickup_proto",
            "drop_proto", "collision_proto", "burner_proto", "mining_product", "recipe_disabled", "pickup_contaminated",
            "drop_quality", "generator_foreign", "generator_damaged", "fuel_invalid", "burnt_result", "invalid_inventory")]
        scenarios += [{"world": "changed"}, {"actor_unit": 59}, {"target_unit": 999}, {"target_direction": 4},
                      {"target_quality": "rare"}, {"pole_network": 2, "generator_network": 2}, {"generator_network": 2},
                      {"generator_energy": 0}, {"supply_reach": 1}, {"fuel_name": "wood"}, {"fuel_quality": "rare"},
                      {"fuel_count": 0}, {"fuel_count": 1.5}, {"held_name": "wood"}, {"held_quality": "rare"},
                      {"held_count": 0}, {"held_count": 1.5}, {"burning_name": "wood"}, {"burning_quality": "rare"},
                      {"replacement_stock": 0}, {"empty_slots": 2}, {"fuel_count": 2, "held_count": 3, "empty_slots": 5}]
        scenarios += [{"endpoint": endpoint, "endpoint_change": change} for endpoint in ("pickup", "drop", "pole")
                      for change in ("missing", "unit", "direction", "offset", "foreign", "surface", "damaged", "quality")]
        for options, result in zip(scenarios, self.execute(scenarios), strict=True):
            with self.subTest(options=options):
                self.assertFalse(result["ok"], result)
                self.assertIn("reason", result)
                self.assertEqual(result["counts"]["mined"], 0)
                self.assertEqual(result["counts"]["started"], 0)
                self.assertEqual(result["remaining_energy"], 1512271)


if __name__ == "__main__":
    unittest.main()
