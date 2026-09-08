"""A partial powered cell must stop spilling before its receiver can be built."""
from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_game import DeterministicGame, run_config


class CellRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), backend="assisted", query=Mock())
        self.builder = FactoryBuilder(self.game, Mock(), SimpleNamespace(entities={}))
        self.drill = {"name": "electric-mining-drill", "position": {"x": 10.5, "y": 12.5}, "direction": 0}
        self.receiver = {"name": "stone-furnace", "position": {"x": 10, "y": 10}, "direction": 0}
        self.plan = {"ok": True, "resource_cell": True, "entities": [self.drill, self.receiver]}
        self.obs = {"world_id": "cell", "tick": 100, "entities": [], "inventory": {"electric-mining-drill": 1, "stone-furnace": 1}}
        self.ground = {"name": "item-on-ground", "position": {"x": 10.5, "y": 10.65}, "item": "iron-ore", "quality": "uncommon", "count": 3}

    def test_legacy_plan_builds_receiver_first_and_reconstructs_drill_last(self):
        original = deepcopy(self.plan)
        self.builder.can_place = Mock(return_value={"ok": True})
        action = self.builder.ensure_plan(self.obs, self.plan)
        self.assertEqual(action["name"], "stone-furnace")
        self.obs["entities"] = [self.receiver]
        self.assertEqual(self.builder.ensure_plan(self.obs, self.plan)["name"], "electric-mining-drill")
        self.assertEqual(self.plan, original)

    def test_partial_cell_mines_only_observed_emitter_then_conserves_spilled_stack(self):
        self.builder.can_place = Mock(return_value={"ok": False})
        self.obs["entities"] = [{**self.drill, "unit_number": 7}]
        self.game.query.return_value = {"ok": True, "world_id": "cell", "emitter": {"unit_number": 7}, "ground": [self.ground]}
        action = self.builder.ensure_plan(self.obs, self.plan)
        self.assertEqual((action["type"], action["name"], action["expected_entity_unit"], action["expected_entity_world_id"]),
                         ("mine", "electric-mining-drill", 7, "cell"))
        self.obs["entities"] = []
        self.game.query.return_value["emitter"] = None
        action = self.builder.ensure_plan(self.obs, self.plan)
        self.assertEqual((action["type"], action["count"], action["quality"]), ("take", 3, "uncommon"))
        self.assertEqual(self.ground["count"], 3)

    def test_changed_world_or_emitter_never_requests_mining_or_pickup(self):
        self.builder.can_place = Mock(return_value={"ok": False})
        self.obs["entities"] = [{**self.drill, "unit_number": 7}]
        for world, emitter in (("different", {"unit_number": 7}), ("cell", {"unit_number": 8}), ("cell", None)):
            with self.subTest(world=world, emitter=emitter):
                self.game.query.return_value = {"ok": True, "world_id": world, "emitter": emitter, "ground": [self.ground]}
                self.assertEqual(self.builder.ensure_plan(self.obs, self.plan)["status"], "blocked")

    def test_generic_or_multiple_drill_plans_cannot_request_emitter_retirement(self):
        self.builder.can_place = Mock(return_value={"ok": False})
        self.obs["entities"] = [self.drill]
        for plan in ({**self.plan, "resource_cell": False}, {**self.plan, "entities": [self.drill, self.drill, self.receiver]}):
            self.assertEqual(self.builder.ensure_plan(self.obs, plan)["status"], "blocked")
        self.game.query.assert_not_called()


@unittest.skipUnless(os.environ.get("FACTORIO_CHARACTER_INPUT_LIVE_TEST") == "1", "requires isolated fixture RCON")
class CellRecoveryLuaTests(unittest.TestCase):
    setUp = CellRecoveryTests.setUp

    def test_live_prototypes_reject_structures_wrong_drop_foreign_or_replaced_drills(self):
        live = DeterministicGame(run_config(runtime=Path("runtime/deterministic/factory-fixture"), server_port=34214, rcon_port=27029))
        self.obs["entities"] = [{**self.drill, "unit_number": 7}]
        for change, accepted in (({}, True), ({"foreign": True}, False), ({"drop": 15}, False),
                ({"structure": True}, False), ({"terrain": False}, False), ({"unit": 8}, False),
                ({"direction": 4}, False), ({"item_type": "ammo"}, False),
                ({"own_actor": True}, True), ({"foreign_actor": True}, False)):
            fixture = '''
local f={};local d={world_id="cell"};local a={type="character"}
local drill={force=''' + ("{}" if change.get("foreign") else "f") + ''',minable=true,
 direction=''' + str(change.get("direction", 0)) + ''',unit_number=''' + str(change.get("unit", 7)) + ''',
 drop_position={x=10.5,y=''' + str(change.get("drop", 10.65)) + '''}}
local function target(p,name) if name=="electric-mining-drill" then return drill end end
local s={find_entities_filtered=function() return {
 {type="resource"}, {name="item-on-ground",type="item-entity",position={x=10.5,y=10.65},
  stack={valid_for_read=true,name="iron-ore",count=3,quality={name="uncommon"},prototype={type="''' + change.get("item_type", "item") + '''"}}}
''' + (', {type="electric-pole"}' if change.get("structure") else '') + (', a' if change.get("own_actor") else '') + (
    ', {type="character"}' if change.get("foreign_actor") else '') + '''} end,
 can_place_entity=function() return ''' + ("false" if change.get("terrain") is False else "true") + ''' end}
'''
            self.game.query.side_effect = lambda body: live.query(fixture + body)
            with self.subTest(change=change):
                action = self.builder._recover_resource_cell_obstruction(self.obs, self.plan, self.receiver)
                self.assertEqual(action is not None, accepted)
                if accepted:
                    self.assertEqual((action["type"], action["expected_entity_unit"]), ("mine", 7))
