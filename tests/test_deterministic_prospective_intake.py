from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_prospective_intake import prospective_intake
from tests import test_deterministic_armaments as fixture


class ProspectiveIntakeTests(unittest.TestCase):
    def setUp(self):
        fixture.ArmamentsTests.setUp(self)
        self.key = "armaments:" + self.armaments._key(self.turret)
        plan = next(p for p in self.armaments._intake_candidates(self.turret)
                    if p["ports"][0]["position"] == {"x": -3.5, "y": -.5})
        self.plan = self.factory.register_plan(self.key, plan, self.obs)
        self.consumer = self.plan["ports"][0]
        self.source = {"kind": "item", "item": "firearm-magazine", "direction": "output",
                       "position": {"x": -10.5, "y": -.5}, "facing": 4}
        self.catalog.entities = {name: {"type": kind} for name, kind in (
            ("transport-belt", "transport-belt"), ("underground-belt", "underground-belt"),
            ("long-handed-inserter", "inserter"), ("inserter", "inserter"), ("small-electric-pole", "electric-pole"))}
        self.game.query.reset_mock()
        self.game.query.return_value = {"ok": True, "receiver_verified": True,
                                       "prospective_intake_verified": True, "tick": 100}

    def allow(self):
        return prospective_intake(self.factory, self.obs, self.consumer)

    def test_exact_unbuilt_chain_reobserves_receiver_and_empty_contacts_without_state_mutation(self):
        before = deepcopy(self.factory.state)
        self.assertIs(self.allow(), self.plan)
        self.assertEqual(self.game.query.call_count, 2)
        self.assertEqual(self.factory.state, before)
        self.builder.can_place.assert_called_with(self.plan["entities"])
        again = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.assertEqual(prospective_intake(again, self.obs, self.consumer), self.plan)

    def test_unique_canonical_owner_and_exact_outer_to_pickup_chain_are_required(self):
        original = deepcopy(self.plan)
        cases = [lambda p: p.pop("key"), lambda p: p["existing_receiver"].update(unit_number=99),
                 lambda p: p["entities"].append(deepcopy(p["entities"][0])),
                 lambda p: p["entities"][1]["position"].update(x=91),
                 lambda p: p["entities"][2].update(direction=12),
                 lambda p: p["entities"][0].update(direction=True)]
        for change in cases:
            with self.subTest(change=change):
                broken = deepcopy(original)
                change(broken)
                self.factory.state["blocks"][self.key] = broken
                self.assertIsNone(self.allow())
        self.factory.state["blocks"][self.key] = original
        self.factory.state["blocks"]["other-owner"] = deepcopy(original)
        self.assertIsNone(self.allow())
        self.game.query.assert_not_called()

    def test_foreign_world_catalog_rollback_and_existing_paid_link_do_not_enable_new_path(self):
        baseline = deepcopy(self.factory.state)
        for change in ({"world_id": "other"}, {"catalog_fingerprint": "other"}, {"last_tick": 101}):
            with self.subTest(change=change):
                self.factory.state = {**deepcopy(baseline), **change}
                self.assertIsNone(self.allow())
        self.factory.state = deepcopy(baseline)
        self.factory.state["links"][self.key] = {"entities": [], "keep": True}
        before = deepcopy(self.factory.state)
        self.assertIsNone(self.allow())
        self.assertEqual(self.factory.state, before)
        self.game.query.assert_not_called()

    def test_other_reserved_endpoint_is_not_an_owned_empty_cell(self):
        other = deepcopy(self.plan["entities"][2])
        self.factory.state["links"]["iron-line"] = {"entities": [other]}
        self.assertIsNone(self.allow())
        self.game.query.assert_not_called()

    def test_reserved_discharge_and_long_arm_specs_reach_the_fresh_contact_guard(self):
        rows = [{"name": "transport-belt", "position": {"x": -3.5, "y": -1.5}, "direction": 8},
                {"name": "underground-belt", "position": {"x": -3.5, "y": .5}, "direction": 0, "belt_to_ground_type": "output"},
                {"name": "long-handed-inserter", "position": {"x": 40.5, "y": 40.5}, "direction": 4}]
        self.factory.state["links"]["other"] = {"entities": rows}
        self.game.query.side_effect = [{"ok": True, "receiver_verified": True, "tick": 100}, {"ok": False}]
        self.assertIsNone(self.allow())
        body = self.game.query.call_args.args[0]
        encoded = body.split("helpers.json_to_table(", 1)[1].split(");local r=", 1)[0]
        self.assertEqual(json.loads(json.loads(encoded))["reserved"], rows)

    def test_missing_stale_or_negative_contact_proof_never_authorizes_underground(self):
        for reply in ({}, {"ok": False}, {"ok": True, "prospective_intake_verified": True, "tick": 99},
                      {"ok": True, "prospective_intake_verified": True, "tick": True}):
            with self.subTest(reply=reply):
                self.game.query.side_effect = [{"ok": True, "receiver_verified": True, "tick": 100}, reply]
                self.assertIsNone(self.allow())
        self.game.query.side_effect = None
        self.builder.can_place.return_value = {"ok": False}
        self.assertIsNone(self.allow())

    def test_only_direct_facing_route_gets_permission_without_enabling_side_drop(self):
        self.factory._material_route = Mock(return_value={"ok": False, "reason": "no route within bounds"})
        self.factory._consumer_drop_bridge_route = Mock()
        result = self.factory._consumer_material_route(self.obs, self.source["position"], self.consumer,
            self.factory._reserved(), start_direction=4)
        self.assertFalse(result["ok"])
        self.assertTrue(self.factory._material_route.call_args.kwargs["allow_underground"])
        self.assertEqual(self.factory._material_route.call_args.kwargs["end_direction"], self.consumer["facing"])
        self.factory._consumer_drop_bridge_route.assert_not_called()
        self.game.query.return_value = {"ok": False}
        self.factory._consumer_material_route(self.obs, self.source["position"], self.consumer,
            self.factory._reserved(), start_direction=4)
        self.assertFalse(self.factory._material_route.call_args.kwargs["allow_underground"])

    def test_combined_route_rejection_cannot_persist_a_link(self):
        inlet = {"name": "underground-belt", "position": {"x": -9.5, "y": -.5}, "direction": 4, "belt_to_ground_type": "input"}
        outlet = {**inlet, "position": {"x": -4.5, "y": -.5}, "belt_to_ground_type": "output"}
        route = {"ok": True, "segments": [inlet, outlet, {"name": "transport-belt", "position": self.consumer["position"], "direction": 4}],
                 "underground_pairs": [{"input": inlet, "output": outlet, "max_distance": 5}]}
        self.factory._consumer_material_route = Mock(return_value=route)
        self.builder.can_place.return_value = {"ok": False}
        before = deepcopy(self.factory.state)
        result = self.factory.connect_input(self.obs, self.source, self.consumer, self.key)
        self.assertEqual(result["reason"], "combined underground input placement changed")
        self.assertEqual(self.factory.state, before)
        checked = self.builder.can_place.call_args.args[0]
        self.assertTrue(all(e in checked for e in self.plan["entities"]))

    def test_prospective_route_retains_owner_and_rejects_alias_connection_key(self):
        inlet = {"name": "underground-belt", "position": {"x": -9.5, "y": -.5}, "direction": 4, "belt_to_ground_type": "input"}
        outlet = {**inlet, "position": {"x": -4.5, "y": -.5}, "belt_to_ground_type": "output"}
        route = {"ok": True, "segments": [inlet, outlet, self.plan["entities"][2]],
                 "underground_pairs": [{"input": inlet, "output": outlet, "max_distance": 5}]}
        self.factory._material_route = Mock(return_value=route)
        before = deepcopy(self.factory.state)
        result = self.factory.connect_input(self.obs, self.source, self.consumer, "alias-link")
        self.assertEqual(result["reason"], "prospective intake differs from connection owner")
        self.assertEqual(self.factory.state, before)
        with patch("factorio_ai.deterministic_input_links.ensure_input_dependencies", return_value=fixture.ready()):
            self.assertEqual(self.factory.connect_input(self.obs, self.source, self.consumer, self.key)["status"], "succeeded")
        saved = deepcopy(self.factory.state["links"][self.key])
        self.assertEqual(saved["underground_pairs"], route["underground_pairs"])
        self.assertNotIn("prospective_intake_owner", saved)
        self.assertTrue(all(e in self.builder.can_place.call_args.args[0] for e in self.plan["entities"]))
        reloaded = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.assertEqual(reloaded.state["links"][self.key], saved)


# This executes emitted Lua only with local table shadows in an explicitly
# selected existing test server. It performs no surface or storage mutations.
SHADOW = '''
local options=helpers.json_to_table(OPTIONS);local game={tick=options.tick or 100}
local d={world_id=options.world or "one"};local f={};local s={}
local function pos(p) return {x=p.x,y=p.y} end
local t={unit_number=options.unit or 5,position={x=0,y=0},force=f,surface=s,health=400,max_health=400,
 quality={name="normal"},bounding_box={left_top={x=-1,y=-1},right_bottom={x=1,y=1}}}
if options.damage then t.health=399 end;if options.quality then t.quality.name=options.quality end
if options.foreign then t.force={} end
t.get_inventory=function() return {get_contents=function() return options.ammo or {} end} end
local function target() if not options.missing then return t end end
s.can_place_entity=function() return not options.blocked end
s.find_entities_filtered=function(args)
 if args.position then return options.occupied and {{type="transport-belt"}} or {} end
 return options.contacts or {}
end
'''


@unittest.skipUnless(os.environ.get("FACTORIO_PROSPECTIVE_INTAKE_LUA_TEST") == "1", "requires explicit inert Lua test opt-in")
class ProspectiveIntakeLuaTests(unittest.TestCase):
    def survey(self, **changes):
        from factorio_ai import deterministic_game
        from factorio_ai.deterministic_game import DeterministicGame, run_config
        case = ProspectiveIntakeTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        reserved = changes.pop("reserved", None)
        if reserved:
            case.factory.state["links"]["foreign"] = {"entities": reserved}
        self.assertIsNotNone(case.allow())
        body = case.game.query.call_args.args[0]
        live = DeterministicGame(run_config(runtime=Path("runtime/deterministic/factory-fixture"), server_port=34214,
            rcon_port=int(os.environ.get("FACTORIO_PROSPECTIVE_INTAKE_RCON_PORT", "27029"))))
        with patch.object(deterministic_game, "_HELPERS", ""):
            return live.query(SHADOW.replace("OPTIONS", json.dumps(json.dumps(changes))) + body)

    def test_live_empty_healthy_normal_receiver_and_bad_identity_or_presence(self):
        self.assertTrue(self.survey()["ok"])
        for change in ({"damage": True}, {"foreign": True}, {"quality": "uncommon"}, {"occupied": True},
                       {"blocked": True}, {"unit": 99}, {"world": "other"}, {"tick": 99}, {"missing": True},
                       {"ammo": [{"name": "piercing-rounds-magazine", "count": 1, "quality": "normal"}]}):
            with self.subTest(change=change):
                self.assertFalse(self.survey(**change)["ok"])

    def test_observed_and_reserved_foreign_discharge_or_inserter_contact(self):
        contacts = [
            {"name": "transport-belt", "type": "transport-belt", "position": {"x": -3.5, "y": -1.5}, "direction": 8},
            {"name": "underground-belt", "type": "underground-belt", "position": {"x": -3.5, "y": .5}, "direction": 0, "belt_to_ground_type": "output"},
            {"name": "long-handed-inserter", "type": "inserter", "position": {"x": -3.5, "y": 1.5}, "direction": 0,
             "pickup_position": {"x": -3.5, "y": -.5}, "drop_position": {"x": -3.5, "y": 3.5}},
            {"name": "long-handed-inserter", "type": "inserter", "position": {"x": -3.5, "y": -2.5}, "direction": 0,
             "pickup_position": {"x": -3.5, "y": -4.5}, "drop_position": {"x": -3.5, "y": -.5}}]
        for contact in contacts:
            with self.subTest(contact=contact):
                self.assertFalse(self.survey(contacts=[contact])["ok"])
                self.assertFalse(self.survey(reserved=[{k: v for k, v in contact.items()
                    if k not in {"type", "pickup_position", "drop_position"}}])["ok"])


if __name__ == "__main__":
    unittest.main()
