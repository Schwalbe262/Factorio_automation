from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config
from tests import test_deterministic_armaments as fixtures


class AmmoSupplyBatchTests(unittest.TestCase):
    def setUp(self):
        fixtures.ArmamentsTests.setUp(self)
        other = deepcopy(self.turret)
        other.update(unit_number=6, position={"x": 10, "y": 0})
        self.turrets = [self.turret, other]
        self.obs["entities"].append(other)
        self.rows = []
        for index, turret in enumerate(self.turrets):
            row = self.armaments._track(turret)
            row["plan"] = next(self.armaments._intake_candidates(turret))
            arm = deepcopy(next(e for e in row["plan"]["entities"] if e["name"] == "inserter"))
            arm["unit_number"] = 50 + index
            self.obs["entities"].append(arm)
            self.rows.append(row)
        self.armaments._ensure_intake = Mock(return_value=fixtures.ready())
        self.game.query.reset_mock()
        self.reply = self.good_reply()
        self.game.query.side_effect = lambda body: deepcopy(self.reply)

    def good_reply(self):
        return {"ok": True, "world_id": "one", "tick": self.obs["tick"], "producer_finished": 10,
                "rows": {self.armaments._key(turret): {
                    "ok": True, "identity": self.armaments._supply_request(self.obs, turret, row),
                    "powered": True, "ammo": 20, "held": index}
                    for index, (turret, row) in enumerate(zip(self.turrets, self.rows))}}

    def test_one_fresh_query_per_call_preserves_both_actual_transfer_proofs(self):
        self.assertEqual(self.armaments.next_action(self.obs)["status"], "waiting")
        self.assertEqual(self.game.query.call_count, 1)
        self.assertEqual([row["sample"]["held"] for row in self.rows], [0, 1])
        self.obs["tick"] = self.reply["tick"] = 200
        self.reply["producer_finished"] = 11
        self.reply["rows"][self.armaments._key(self.turrets[0])]["ammo"] = 21
        self.reply["rows"][self.armaments._key(self.turrets[1])].update(ammo=19, held=0)
        self.assertEqual(self.armaments.next_action(self.obs)["status"], "succeeded")
        self.assertEqual(self.game.query.call_count, 2)
        self.assertTrue(all(row["owned"] for row in self.rows))
        self.assertEqual([row["proof"]["ammo"] for row in self.rows], [21, 19])
        self.assertTrue(all(row["proof"]["producer_cycles"] == 1 for row in self.rows))
        self.assertFalse(any("supply" in key for key in self.armaments.state))

    def test_invalid_envelope_never_supplies_any_proof(self):
        replies = [None, [], {}, {**self.reply, "world_id": "other"}, {**self.reply, "tick": 99},
                   {**self.reply, "tick": True}, {**self.reply, "producer_finished": -1},
                   {**self.reply, "producer_finished": True}, {**self.reply, "rows": {}},
                   {**self.reply, "rows": []}]
        for reply in replies:
            with self.subTest(reply=reply):
                self.reply = reply
                for row in self.rows:
                    row.update(owned=True, sample={"tick": 1, "ammo": 1, "held": 0, "producer_finished": 1})
                self.armaments._route_present = Mock(return_value=True)
                self.assertEqual(self.armaments.next_action(self.obs)["status"], "waiting")
                self.assertFalse(any(row["owned"] or "sample" in row for row in self.rows))

    def test_same_observation_tick_still_reads_fresh_and_counter_rollback_revokes_proof(self):
        self.armaments.next_action(self.obs)
        self.reply["tick"] = 200
        self.reply["producer_finished"] = 11
        for live in self.reply["rows"].values():
            live.update(ammo=21, held=0)
        self.assertEqual(self.armaments.next_action(self.obs)["status"], "succeeded")
        self.armaments._route_present = Mock(return_value=True)
        self.reply.update(tick=300, producer_finished=1)
        self.assertEqual(self.armaments.next_action(self.obs)["status"], "waiting")
        self.assertEqual(self.game.query.call_count, 3)
        self.assertFalse(any(row["owned"] for row in self.rows))
        self.assertTrue(all(row["sample"]["producer_finished"] == 1 for row in self.rows))

    def test_missing_replaced_or_malformed_rows_cannot_borrow_other_turret_proof(self):
        key = self.armaments._key(self.turret)
        changes = [{"ok": False}, {"identity": self.reply["rows"][self.armaments._key(self.turrets[1])]["identity"]},
                   {"ammo": True}, {"ammo": -1}, {"held": "1"}, {"powered": "yes"},
                   {"identity": {**self.reply["rows"][key]["identity"], "inserter_unit": 999}}]
        for change in changes:
            with self.subTest(change=change):
                self.reply = self.good_reply()
                self.reply["rows"][key].update(change)
                survey = self.armaments._supply_observations(self.obs, self.turrets)
                self.assertFalse(survey[key]["observation"]["ok"])
                self.assertTrue(survey[self.armaments._key(self.turrets[1])]["observation"]["ok"])

    def test_only_exact_observed_units_are_requested(self):
        original = deepcopy(self.obs)
        for defect in ("missing", "missing_unit", "rotated", "duplicate", "turret_replaced", "world", "catalog"):
            with self.subTest(defect=defect):
                self.obs = deepcopy(original)
                self.armaments.state["world_id"] = "one"
                self.armaments.state["catalog_fingerprint"] = "prototype-a"
                arm = self.obs["entities"][2]
                if defect == "missing":
                    self.obs["entities"].remove(arm)
                elif defect == "missing_unit":
                    arm.pop("unit_number")
                elif defect == "rotated":
                    arm["direction"] = (arm["direction"] + 4) % 16
                elif defect == "duplicate":
                    self.obs["entities"].append(deepcopy(arm))
                elif defect == "turret_replaced":
                    self.obs["entities"][0]["unit_number"] = 999
                elif defect == "world":
                    self.armaments.state["world_id"] = "other"
                else:
                    self.armaments.state["catalog_fingerprint"] = "other"
                self.assertIsNone(self.armaments._supply_request(self.obs, self.turret, self.rows[0]))

    def test_early_action_returns_without_survey_or_proof_for_unreached_turret(self):
        action = {"type": "build", "name": "inserter"}
        self.armaments._ensure_intake.return_value = action
        self.assertEqual(self.armaments.next_action(self.obs), action)
        self.game.query.assert_not_called()
        self.armaments._ensure_intake.side_effect = [fixtures.ready(), action]
        self.assertEqual(self.armaments.next_action(self.obs), action)
        self.assertEqual(self.game.query.call_count, 1)
        self.assertIn("sample", self.rows[0])
        self.assertNotIn("sample", self.rows[1])

    def test_later_intake_change_cannot_reuse_mutable_survey_identity(self):
        def ensure(obs, turret, row, source):
            if turret["unit_number"] == 6:
                row["plan"]["entities"][0]["position"]["x"] += 1
            return fixtures.ready()
        self.armaments._ensure_intake.side_effect = ensure
        self.assertEqual(self.armaments.next_action(self.obs)["status"], "waiting")
        self.assertEqual(self.game.query.call_count, 1)
        self.assertIn("sample", self.rows[0])
        self.assertNotIn("sample", self.rows[1])


SHADOW = '''
local storage=nil;local o=helpers.json_to_table(OPTIONS);local game={tick=o.tick}
local f={};local s={};local d={world_id=o.world};local scans=0
local function producer(name,n) return {get_recipe=function() return {name=name} end,products_finished=n} end
s.find_entities_filtered=function(filter)
 assert(filter.force==f and filter.type=="assembling-machine");scans=scans+1
 return {producer("firearm-magazine",3),producer("iron-gear-wheel",100),producer("firearm-magazine",7)}
end
local function target(p,name)
 local first=p.x<5
 if first and o.missing==name then return nil end
 local t={force=first and o.foreign_force==name and {} or f,surface=first and o.foreign_surface==name and {} or s,
  position={x=p.x+(first and o.shift==name and 1 or 0),y=p.y},direction=first and o.rotated and 4 or 12,
  unit_number=name=="gun-turret" and (first and 5 or 6) or (first and 50 or 51),energy=o.unpowered and 0 or 100,
  is_connected_to_electric_network=function() return not o.disconnected end,
  held_stack={valid_for_read=true,name=o.held_item or "firearm-magazine",count=first and 1 or 2},
  get_inventory=function(inventory)
   assert(inventory==defines.inventory.turret_ammo)
   return {get_item_count=function(item) assert(item=="firearm-magazine");return first and 20 or 30 end}
  end}
 if first and o.replaced==name then t.unit_number=999 end
 return t
end
'''


@unittest.skipUnless(os.environ.get("FACTORIO_AMMO_SURVEY_LIVE_TEST") == "1", "requires opt-in existing RCON server")
class AmmoSupplyBatchLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = DeterministicGame(run_config(runtime=Path("runtime/deterministic/factory-fixture"),
            server_port=34214, rcon_port=int(os.environ.get("FACTORIO_AMMO_SURVEY_RCON_PORT", "27029"))))

    def survey(self, **changes):
        case = AmmoSupplyBatchTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.armaments._supply_observations(case.obs, case.turrets)
        body = case.game.query.call_args.args[0]
        options = dict(tick=100, world="one")
        options.update(changes)
        with patch.object(deterministic_game, "_HELPERS", ""):
            return self.live.query(SHADOW.replace("OPTIONS", json.dumps(json.dumps(options)))
                                   + "local function survey() " + body + " end;local out=survey();out.scans=scans;return out")

    def test_actual_lua_reads_distinct_material_and_held_stacks_and_one_producer_sum(self):
        result = self.survey()
        self.assertEqual((result["scans"], result["producer_finished"]), (1, 10))
        rows = result["rows"]
        self.assertEqual((rows["gun-turret:0,0"]["ammo"], rows["gun-turret:10,0"]["ammo"]), (20, 30))
        self.assertEqual((rows["gun-turret:0,0"]["held"], rows["gun-turret:10,0"]["held"]), (1, 2))
        self.assertTrue(all(row["ok"] and row["powered"] for row in rows.values()))
        self.assertTrue(all(row["held"] == 0 for row in self.survey(held_item="iron-plate")["rows"].values()))

    def test_actual_lua_rejects_missing_foreign_and_replaced_units_without_cross_contamination(self):
        for field in ("missing", "foreign_force", "foreign_surface", "replaced", "shift"):
            for name in ("gun-turret", "inserter"):
                with self.subTest(field=field, name=name):
                    rows = self.survey(**{field: name})["rows"]
                    self.assertFalse(rows["gun-turret:0,0"]["ok"])
                    self.assertTrue(rows["gun-turret:10,0"]["ok"])
        self.assertFalse(self.survey(rotated=True)["rows"]["gun-turret:0,0"]["ok"])
        for change in ({"world": "other"}, {"tick": 99}):
            self.assertFalse(self.survey(**change)["ok"])
        for change in ({"unpowered": True}, {"disconnected": True}):
            self.assertTrue(all(not row["powered"] for row in self.survey(**change)["rows"].values()))


if __name__ == "__main__":
    unittest.main()
