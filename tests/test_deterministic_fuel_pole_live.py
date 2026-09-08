"""Opt-in local-table execution of the exact new-pole intake survey Lua."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_fuel_intake import _survey


SHADOW = '''
local storage=nil;local game={tick=100};local o=helpers.json_to_table(OPTIONS)
local f={recipes={inserter={enabled=true}}};local d={world_id=o.world};local a=nil
local furnace={name="stone-furnace",force=f,position={x=10,y=-62},unit_number=o.burner_unit,burner={},
 prototype=prototypes.entity["stone-furnace"]}
local belt={name="transport-belt",force=o.foreign_belt and {} or f,position={x=9.5,y=-64.5},
 direction=o.belt_direction,unit_number=2,prototype=prototypes.entity["transport-belt"]}
belt.get_transport_line=function() return {get_contents=function() return {{name=o.cargo,count=1}} end} end
local pole={name="small-electric-pole",force=o.foreign_pole and {} or f,position={x=11.5,y=-63.5},
 direction=o.pole_direction,unit_number=o.pole_unit,quality="normal",electric_network_id=7,
 prototype=prototypes.entity["small-electric-pole"]}
local arm={name="inserter",force=f,position={x=9.5,y=-63.5},direction=o.arm_direction,unit_number=o.arm_unit,
 pickup_position={x=9.5,y=-64.5},drop_position={x=9.5,y=-62.3},energy=o.arm_energy,electric_network_id=7,
 is_connected_to_electric_network=function() return o.connected end}
local function target(p,name)
 if name=="stone-furnace" then return furnace end
 if name=="transport-belt" then return belt end
 if name=="small-electric-pole" and o.pole_present then return pole end
 if name=="inserter" and o.arm_present then return arm end
end
local s={find_entities_filtered=function(spec)
 if spec.type=="generator" then return o.active and {{energy=1,electric_network_id=7}} or {} end
 if spec.type=="electric-pole" then return o.pole_present and {pole} or {} end
 return {}
end,can_place_entity=function() return true end}
'''


@unittest.skipUnless(os.environ.get("FACTORIO_FUEL_POLE_LIVE_TEST") == "1", "requires opt-in existing RCON server")
class FuelPoleLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = DeterministicGame(run_config(runtime=Path("runtime/deterministic/factory-fixture"),
            server_port=34214, rcon_port=int(os.environ.get("FACTORIO_FUEL_POLE_RCON_PORT", "27029"))))

    def survey(self, pending=True, bound=False, **changes):
        options = dict(world="fixture", burner_unit=1, belt_direction=12, cargo="coal", pole_direction=0,
                       arm_direction=0, pole_unit=3, arm_unit=4, pole_present=False, arm_present=False,
                       active=True, arm_energy=268, connected=True)
        options.update(changes)
        burner = {"name": "stone-furnace", "position": {"x": 10, "y": -62}}
        belt = {"name": "transport-belt", "position": {"x": 9.5, "y": -64.5}, "direction": 12}
        record = {"world_id": "fixture", "burner_unit": 1, "new_pole": True,
                  "arm": {"name": "inserter", "position": {"x": 9.5, "y": -63.5}, "direction": 0},
                  "pole": {"name": "small-electric-pole", "position": {"x": 11.5, "y": -63.5}, "direction": 0}}
        if bound:
            record.update(pole_unit=3, arm_unit=4)
        fake = SimpleNamespace(game=SimpleNamespace(query=Mock(return_value={})))
        _survey(fake, burner, [{"parent": "coal-parent", "belt": belt, "unit_number": 2}], record if pending else None)
        body = fake.game.query.call_args.args[0]
        script = SHADOW.replace("OPTIONS", json.dumps(json.dumps(options))) + body
        with patch.object(deterministic_game, "_HELPERS", ""):
            return self.live.query(script)

    def test_uncovered_valid_arm_offers_normal_pole_planning_without_power_claim(self):
        result = self.survey(pending=False)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["options"])
        self.assertEqual(len(result["uncovered"]), 1)
        self.assertNotIn("pole_unit", result["uncovered"][0])
        self.assertEqual(result["new_pole_reach"], 2)

    def test_partial_builds_have_real_units_only_and_no_premature_power(self):
        for arm, pole in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(arm=arm, pole=pole):
                result = self.survey(arm_present=arm, pole_present=pole)
                self.assertEqual(len(result["options"]), 1, result)
                row = result["options"][0]
                self.assertEqual(row.get("arm_unit"), 4 if arm else None)
                self.assertEqual(row.get("pole_unit"), 3 if pole else None)
                self.assertEqual(row["powered"], arm and pole)

    def test_bound_identity_foreign_force_facing_world_and_material_changes_reject(self):
        for changes in ({"pole_present": False}, {"arm_present": False}, {"pole_unit": 55}, {"arm_unit": 55},
                        {"pole_direction": 4}, {"arm_direction": 4}, {"belt_direction": 4},
                        {"foreign_pole": True}, {"foreign_belt": True}, {"world": "other"},
                        {"burner_unit": 55}, {"cargo": "iron-plate"}):
            with self.subTest(changes=changes):
                options = {"arm_present": True, "pole_present": True, **changes}
                self.assertFalse(self.survey(bound=True, **options).get("options"))

    def test_disconnected_or_empty_energy_cannot_be_reported_powered(self):
        for changes in ({"active": False}, {"connected": False}, {"arm_energy": 0}):
            with self.subTest(changes=changes):
                result = self.survey(bound=True, arm_present=True, pole_present=True, **changes)
                self.assertEqual(len(result["options"]), 1)
                self.assertFalse(result["options"][0]["powered"])


if __name__ == "__main__":
    unittest.main()
