from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_fuel_intake import reserve_adjacent_fuel_intake, validate_adjacent_fuel_intake, _survey


class AdjacentFuelIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), backend="character", query=Mock())
        self.bootstrap = Mock()
        catalog = SimpleNamespace(fingerprint="catalog", entities={}, recipes={}, technologies={})
        self.builder = FactoryBuilder(self.game, self.bootstrap, catalog)
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, catalog)
        self.burner = {"name": "stone-furnace", "position": {"x": 10, "y": -62}}
        self.belt = {"name": "transport-belt", "position": {"x": 9.5, "y": -64.5}, "direction": 12}
        self.arm = {"name": "inserter", "position": {"x": 9.5, "y": -63.5}, "direction": 0}
        self.pole = {"name": "small-electric-pole", "position": {"x": 11.5, "y": -63.5}, "direction": 0}
        self.source = {"kind": "item", "item": "coal", "direction": "output", "position": {"x": .5, "y": .5}, "facing": 4}
        self.obs = {"world_id": "one", "tick": 100, "inventory": {}, "entities": [
            {**deepcopy(self.belt), "unit_number": 2, "belt_inventory": {}}]}
        self.factory._sync(self.obs)
        self.parent = "fuel:burner-mining-drill:10,-60"
        self.key = "fuel:stone-furnace:10,-62"
        self.factory.state["links"][self.parent] = {"entities": [self.belt], "source_port": deepcopy(self.source)}
        self.row = {"parent": self.parent, "belt": deepcopy(self.belt), "belt_unit": 2, "burner_unit": 1,
                    "arm": deepcopy(self.arm), "pole": deepcopy(self.pole), "pole_unit": 3, "coal_items": 0}
        self.proof = {"ok": True, "world_id": "one", "options": [self.row]}
        self.game.query.return_value = self.proof

    def reserve(self):
        return reserve_adjacent_fuel_intake(self.factory, self.obs, self.burner, self.source, self.key)

    def validate(self):
        return validate_adjacent_fuel_intake(self.factory, self.obs, self.burner, self.source, self.key)

    def test_preserves_existing_belt_facing_and_registers_explicit_direct_dependency(self):
        self.assertTrue(self.reserve())
        block, link = self.factory.state["blocks"][self.key], self.factory.state["links"][self.key]
        self.assertEqual(block["entities"], [self.arm, self.pole])
        self.assertEqual(link["entities"], [self.belt])
        self.assertEqual(link["consumer_port"]["facing"], 12)
        self.assertEqual(link["upstream_tap"], {"link_key": self.parent, "belt": self.belt,
            "intake": {"block_key": self.key, "inserter": self.arm, "receiver": self.burner}})
        self.assertEqual(block["adjacent_fuel_intake"]["belt_unit"], 2)
        self.assertIsNone(self.validate())
        self.assertEqual(self.factory.state["automated_burners"] if "automated_burners" in self.factory.state else [], [])

    def test_wrong_material_conflicting_parent_and_reserved_arm_are_rejected(self):
        before = deepcopy(self.factory.state)
        for defect in ("item", "direction", "content", "shared", "reserved", "retired"):
            with self.subTest(defect=defect):
                self.factory.state = deepcopy(before)
                self.obs["entities"][0].update(direction=12, belt_inventory={})
                if defect == "item":
                    self.factory.state["links"][self.parent]["source_port"]["item"] = "iron-plate"
                elif defect == "direction":
                    self.obs["entities"][0]["direction"] = 4
                elif defect == "content":
                    self.obs["entities"][0]["belt_inventory"] = {"iron-plate": 1}
                elif defect == "shared":
                    self.factory.state["links"]["other"] = deepcopy(self.factory.state["links"][self.parent])
                elif defect == "reserved":
                    self.factory.state["blocks"]["other"] = {"entities": [self.arm]}
                else:
                    self.factory.state["blocks"][self.parent] = {"retired_for_upgrade": "coal", "entities": []}
                self.assertFalse(self.reserve())
                self.assertNotIn(self.key, self.factory.state["blocks"])

    def test_resume_rejects_replaced_belt_world_conflicts_and_missing_live_power(self):
        self.assertTrue(self.reserve())
        self.obs["entities"][0]["unit_number"] = 99
        self.assertEqual(self.validate()["status"], "blocked")
        self.obs["entities"][0]["unit_number"] = 2
        self.obs["world_id"] = "other"
        self.assertEqual(self.validate()["status"], "blocked")
        self.obs["world_id"] = "one"
        self.proof["options"] = []
        self.assertEqual(self.validate()["status"], "blocked")
        self.proof["options"] = [self.row]
        self.factory.state["blocks"]["other"] = {"entities": [self.arm]}
        self.assertEqual(self.validate()["status"], "blocked")

    def test_upstream_work_prevents_automatic_ownership_even_when_intake_is_constructed(self):
        self.assertTrue(self.reserve())
        self.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
        self.factory.ensure_power_connection = Mock(return_value={"status": "succeeded"})
        self.factory.connect_input = Mock(return_value={"status": "running", "reason": "complete upstream prefix"})
        result = self.factory._fuel_burner(self.obs, self.burner, self.source)
        self.assertEqual(result["status"], "running")
        self.assertNotIn(self.factory._entity_key(self.burner), self.factory.state.get("automated_burners", []))
        self.factory.connect_input.return_value = {"status": "succeeded"}
        self.assertEqual(self.factory._fuel_burner(self.obs, self.burner, self.source)["status"], "succeeded")

    def test_exact_proof_and_dependency_survive_restart(self):
        self.assertTrue(self.reserve())
        self.row["arm_unit"] = 4
        self.assertIsNone(self.validate())
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.factory.catalog)
        self.assertEqual(self.factory.state["blocks"][self.key]["adjacent_fuel_intake"]["arm_unit"], 4)
        self.assertIsNone(self.validate())


@unittest.skipUnless(os.environ.get("FACTORIO_CHARACTER_INPUT_LIVE_TEST") == "1", "requires isolated QA RCON")
class AdjacentFuelIntakeLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from factorio_ai.deterministic_game import DeterministicGame, run_config
        cls.game = DeterministicGame(run_config(runtime=Path("runtime/deterministic/character-navigation-qa"),
                                                server_port=34216, rcon_port=27031))

    def survey(self, defect=None):
        fake = SimpleNamespace(game=SimpleNamespace(query=Mock(return_value={})))
        burner = {"name": "stone-furnace", "position": {"x": 10, "y": -62}}
        options = [{"parent": "coal-parent", "belt": {"name": "transport-belt", "position": {"x": 9.5, "y": -64.5},
                    "direction": 12}, "unit_number": 2}]
        _survey(fake, burner, options)
        body = fake.game.query.call_args.args[0]
        fixture = '''
local f={recipes={inserter={enabled=true}}};local d={world_id="fixture"}
local furnace={name="stone-furnace",force=f,position={x=10,y=-62},unit_number=1,burner={},
 prototype=prototypes.entity["stone-furnace"]}
local belt={name="transport-belt",force=f,position={x=9.5,y=-64.5},direction=12,unit_number=2,
 prototype=prototypes.entity["transport-belt"]}
belt.get_transport_line=function() return {get_contents=function() return {{name="''' + ("iron-plate" if defect == "content" else "coal") + '''",count=1}} end} end
local pole={name="small-electric-pole",position={x=11.5,y=-63.5},unit_number=3,quality="normal",
 prototype=prototypes.entity["small-electric-pole"]}
''' + ({"foreign": "belt.force={}", "receiver": "furnace.force={}", "unit": "belt.unit_number=99",
        "direction": "belt.direction=4"}.get(defect, "")) + '''
local function target(p,name)
 if name=="stone-furnace" then return furnace end
 if name=="transport-belt" then return belt end
end
local s={find_entities_filtered=function(spec)
 if spec.type=="generator" then return ''' + ("{}" if defect == "power" else "{{energy=1,electric_network_id=7}}") + ''' end
 if spec.type=="electric-pole" then pole.electric_network_id=7;return {pole} end
 return {}
end,can_place_entity=function() return true end}
'''
        return self.game.query(fixture + body)

    def test_live_prototypes_pick_existing_belt_and_drop_inside_owned_furnace(self):
        result = self.survey()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["options"]), 1)
        row = result["options"][0]
        self.assertEqual(row["arm"], {"name": "inserter", "position": {"x": 9.5, "y": -63.5}, "direction": 0})
        self.assertEqual((row["belt_unit"], row["burner_unit"], row["pole_unit"]), (2, 1, 3))
        self.assertEqual(row["coal_items"], 2)

    def test_wrong_content_force_unit_facing_or_power_never_qualifies(self):
        for defect in ("content", "foreign", "receiver", "unit", "direction", "power"):
            with self.subTest(defect=defect):
                result = self.survey(defect)
                self.assertFalse(result.get("options"), result)
