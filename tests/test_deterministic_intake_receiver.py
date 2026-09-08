from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_intake_receiver import support_entities, verify_receiver
from tests import test_deterministic_armaments as fixtures


class IntakeReceiverTests(unittest.TestCase):
    def setUp(self):
        fixtures.ArmamentsTests.setUp(self)
        self.key = "armaments:" + self.armaments._key(self.turret)
        self.row = self.armaments._track(self.turret)
        self.plan = next(self.armaments._intake_candidates(self.turret))
        self.source = {"kind": "item", "item": "firearm-magazine", "direction": "output",
                       "position": {"x": 20.5, "y": .5}, "facing": 4}
        self.factory.connect_input = Mock(return_value={"type": "build", "name": "transport-belt"})

    def call(self):
        return self.armaments._ensure_intake(self.obs, self.turret, self.row, self.source)

    def test_existing_receiver_overlap_does_not_release_old_iron_reservations(self):
        old = {"ok": True, "entities": [{"name": "transport-belt", "direction": 4,
                "position": {"x": x, "y": -.5}} for x in (-.5, .5)], "ports": []}
        self.factory.state["links"]["old-iron-output"] = deepcopy(old)
        self.assertEqual(self.call()["type"], "build")
        self.assertEqual(self.factory.state["links"]["old-iron-output"], old)
        self.assertEqual(len(self.row["plan"]["entities"]), 4)
        self.assertEqual(self.factory.state["blocks"][self.key], self.row["plan"])
        self.assertEqual(self.row["plan"]["existing_receiver"]["unit_number"], 5)

    def test_support_hardware_still_respects_foreign_reservations(self):
        obstruction = {**deepcopy(self.plan["entities"][0]), "name": "wooden-chest"}
        self.factory.state["blocks"]["other"] = {"entities": [obstruction]}
        self.armaments._intake_candidates = Mock(return_value=iter([self.plan]))
        self.assertEqual(self.call()["status"], "blocked")
        self.assertNotIn("plan", self.row)
        self.factory.connect_input.assert_not_called()

    def test_unknown_stale_live_receiver_never_registers_or_routes(self):
        for reply in ({}, None, {"ok": True, "tick": 100}, {"ok": True, "receiver_verified": True, "tick": 99},
                      {"ok": True, "receiver_verified": True, "tick": True}, {"ok": False}):
            with self.subTest(reply=reply):
                self.game.query.return_value = reply
                before = deepcopy((self.armaments.state, self.factory.state))
                self.assertEqual(self.call()["status"], "blocked")
                self.assertEqual((self.armaments.state, self.factory.state), before)
                self.factory.connect_input.assert_not_called()

    def test_saved_support_receiver_and_pickup_geometry_remain_bound(self):
        for defect in ("missing", "world", "unit", "position", "catalog", "arm", "pickup"):
            with self.subTest(defect=defect):
                plan = deepcopy(self.plan)
                if defect == "missing":
                    plan.pop("existing_receiver")
                elif defect in {"world", "unit", "catalog"}:
                    field = {"world": "world_id", "unit": "unit_number", "catalog": "catalog_fingerprint"}[defect]
                    plan["existing_receiver"][field] = "other"
                elif defect == "position":
                    plan["existing_receiver"]["position"]["x"] += 1
                elif defect == "arm":
                    plan["entities"][0]["direction"] = 4
                else:
                    plan["entities"][1]["direction"] = 0
                self.row["plan"] = self.factory.state["blocks"][self.key] = plan
                self.assertEqual(self.call()["status"], "blocked")
                self.factory.connect_input.assert_not_called()

    def test_legacy_receiver_comparison_is_exact_except_aim(self):
        legacy = deepcopy(self.plan)
        legacy.pop("existing_receiver")
        turret = {"name": "gun-turret", "position": deepcopy(self.turret["position"]),
                  "direction": 12, "_width": 2, "_height": 2}
        legacy["entities"].insert(0, turret)
        self.assertEqual(support_entities(legacy, self.turret), self.plan["entities"])
        for change in ({"_width": 3}, {"unit_number": 99}, {"position": {"x": 1, "y": 0}}):
            broken = deepcopy(legacy)
            broken["entities"][0].update(change)
            self.assertIsNone(support_entities(broken, self.turret))


SHADOW = '''
local storage=nil;local o=helpers.json_to_table(OPTIONS);local game={tick=o.tick}
local f={};local s={};local d={world_id=o.world}
local t={name=o.name,force=o.foreign_force and {} or f,surface=o.foreign_surface and {} or s,
 unit_number=o.unit,position={x=o.x,y=0},prototype={tile_width=o.width,tile_height=o.height},
 bounding_box={left_top={x=-1,y=-1},right_bottom={x=1,y=1}}}
local function target() if o.missing then return nil end;return t end
local actual=prototypes;local prototypes={entity={}}
prototypes.entity.inserter={inserter_pickup_position=o.bad_pickup and {0,-2} or actual.entity.inserter.inserter_pickup_position,
 inserter_drop_position=o.bad_drop and {0,0} or actual.entity.inserter.inserter_drop_position}
prototypes.entity["transport-belt"]={collision_box=actual.entity["transport-belt"].collision_box}
'''


@unittest.skipUnless(os.environ.get("FACTORIO_INTAKE_RECEIVER_LIVE_TEST") == "1", "requires opt-in existing RCON server")
class IntakeReceiverLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = DeterministicGame(run_config(runtime=Path("runtime/deterministic/factory-fixture"),
            server_port=34214, rcon_port=int(os.environ.get("FACTORIO_INTAKE_RECEIVER_RCON_PORT", "27029"))))

    def survey(self, **changes):
        case = IntakeReceiverTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        verify_receiver(case.armaments, case.obs, case.turret, case.plan)
        body = case.game.query.call_args.args[0]
        options = dict(tick=100, world="one", name="gun-turret", unit=5, x=0, width=2, height=2)
        options.update(changes)
        with patch.object(deterministic_game, "_HELPERS", ""):
            return self.live.query(SHADOW.replace("OPTIONS", json.dumps(json.dumps(options))) + body)

    def test_actual_prototype_rotations_pick_up_belt_and_drop_into_square_receiver(self):
        self.assertTrue(self.survey().get("receiver_verified"))

    def test_wrong_live_receiver_identity_and_footprint_fail_closed(self):
        for changes in ({"world": "other"}, {"tick": 99}, {"name": "assembling-machine-1"},
                        {"unit": 77}, {"x": 1}, {"width": 3}, {"height": 3},
                        {"foreign_force": True}, {"foreign_surface": True}, {"missing": True}):
            with self.subTest(changes=changes):
                self.assertFalse(self.survey(**changes).get("ok"))

    def test_changed_prototype_pickup_or_drop_geometry_is_rejected(self):
        for change in ({"bad_pickup": True}, {"bad_drop": True}):
            with self.subTest(change=change):
                self.assertFalse(self.survey(**change).get("ok"))


if __name__ == "__main__":
    unittest.main()
