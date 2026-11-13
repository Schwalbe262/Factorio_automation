from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.defense_ammo_policy import DefenseAmmoPolicy
from factorio_agent.post_ammo_transfer import RED, POLICY_FILE, execute_upgrade_post_ammunition
import test_v2_post_ammo_transfer as fixtures


@unittest.skipUnless(fixtures.LuaRuntime, "optional executable native Lua fixture")
class DefenseAmmoPolicyTests(fixtures.PostAmmoFixture):
    def setUp(self):
        fixtures.NativePostAmmoTests.setUp(self)
        (self.root / POLICY_FILE).unlink()
        self.obs = dict(ok=True, world_id="w", actor_unit_number=11, tick=20)
        self.factory = SimpleNamespace(
            commissioned=True,
            proof=Mock(return_value={"actual_machine_production": True}),
            ensure_full_magazines=Mock(return_value={"type": "take_factory_magazines", "count": 10}),
        )
        self.policy = DefenseAmmoPolicy(
            self.game,
            SimpleNamespace(fingerprint="c"),
            SimpleNamespace(path=self.root / "defense-perimeter.json"),
            self.factory,
        )

    def test_prepare_readonly_and_hardening_front_precedes_other_recent_damage(self):
        self.lua.execute("rear_a.health=200")
        hardening = dict(
            world_id="w",
            catalog_fingerprint="c",
            phase="verified",
            posts=[deepcopy(self.posts[0])],
            unit_build_receipts={"7": {"historical_yellow_birth": "unchanged"}},
        )
        (self.root / "defense-hardening.json").write_text(json.dumps(hardening))
        before = {p.name: p.read_bytes() for p in self.root.glob("*.json")}
        preview = self.policy.prepare(self.obs)
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(preview["candidates"][0]["post"]["name"], "front")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.glob("*.json")})
        action = self.policy.step(self.obs)
        self.assertEqual(action["type"], "upgrade_post_ammunition")
        self.assertEqual(hardening, json.loads((self.root / "defense-hardening.json").read_text()))
        self.assertEqual(self.perimeter, json.loads((self.root / "defense-perimeter.json").read_text()))

    def test_factory_wait_does_not_block_ordinary_upkeep_or_craft_red(self):
        self.lua.execute("main[1].count=1;main[1].ammo=3")
        self.factory.ensure_full_magazines.return_value = {"status": "waiting", "reason": "real_factory_output_empty"}
        self.assertIsNone(self.policy.step(self.obs))
        self.factory.ensure_full_magazines.assert_called_once_with(self.obs, 10)
        self.assertFalse((self.root / POLICY_FILE).exists())
        self.factory.ensure_full_magazines.return_value = {"type": "take_factory_magazines", "count": 10}
        self.assertEqual(self.policy.step(self.obs)["type"], "take_factory_magazines")
        self.assertFalse((self.root / POLICY_FILE).exists())

    def test_real_action_restart_reconciles_native_receipt_without_repaying_or_rewriting_birth(self):
        action = self.policy.step(self.obs)
        self.assertEqual(action["count"], 10)
        result = execute_upgrade_post_ammunition(self.game, action)
        self.assertTrue(result["ok"], result)
        # Fire can consume the new ammunition before the controller observes it.
        self.lua.execute("front.get_inventory(1)[1].ammo=2")
        restarted = DefenseAmmoPolicy(self.game, SimpleNamespace(fingerprint="c"), None, self.factory)
        self.assertIsNone(restarted.step(self.obs))
        state = json.loads((self.root / POLICY_FILE).read_text())
        txn = state["transactions"][action["transaction_id"]]
        self.assertEqual(txn["phase"], "complete")
        self.assertEqual(txn["receipt"]["loaded_rounds"], 100)
        self.assertEqual(state["conversions"]["front"], [action["transaction_id"]])
        self.assertEqual(state["preferences"]["front"]["item"], RED)
        self.assertEqual(self.perimeter, json.loads((self.root / "defense-perimeter.json").read_text()))

    def test_unknown_native_intent_blocks_and_never_adopts_or_cancels_effects(self):
        action = self.policy.step(self.obs)
        self.lua.execute(
            "d.defense_ammo_upgrades={["
            + json.dumps(action["transaction_id"])
            + ']={phase="intent",world_id="w",actor_unit=11,unit_number=7}}'
        )
        self.assertEqual(self.policy.step(self.obs)["reason"], "post_ammunition_uncertain_or_changed_native_receipt")
        state = json.loads((self.root / POLICY_FILE).read_text())
        self.assertEqual(state["transactions"][action["transaction_id"]]["phase"], "pending")

    def test_changed_receipt_totals_never_credit_a_conversion(self):
        action = self.policy.step(self.obs)
        self.assertTrue(execute_upgrade_post_ammunition(self.game, action)["ok"])
        self.lua.execute(
            "d.defense_ammo_upgrades["
            + json.dumps(action["transaction_id"])
            + '].receipt.after_post["piercing-rounds-magazine"].rounds=101'
        )
        self.assertEqual(self.policy.step(self.obs)["reason"], "post_ammunition_uncertain_or_changed_native_receipt")
        self.assertFalse(json.loads((self.root / POLICY_FILE).read_text())["conversions"])

    def test_unexecuted_changed_actor_intent_is_append_only_cancelled_not_retargeted(self):
        first = self.policy.step(self.obs)
        self.lua.execute("a.unit_number=12")
        obs = {**self.obs, "actor_unit_number": 12}
        self.assertIsNone(self.policy.step(obs))
        state = json.loads((self.root / POLICY_FILE).read_text())
        old = state["transactions"][first["transaction_id"]]
        self.assertEqual(old["phase"], "cancelled_unexecuted")
        self.assertEqual(old["payload"]["action"]["expected_actor_unit"], 11)
        second = self.policy.step(obs)
        self.assertEqual(second["expected_actor_unit"], 12)
        self.assertNotEqual(first["transaction_id"], second["transaction_id"])
        self.assertEqual(len(json.loads((self.root / POLICY_FILE).read_text())["transactions"]), 2)

    def test_lost_support_or_consumed_prepayment_never_starves_normal_recovery(self):
        action = self.policy.step(self.obs)
        self.lua.execute("rear_a.valid=false")
        self.assertIsNone(self.policy.step(self.obs))
        state = json.loads((self.root / POLICY_FILE).read_text())
        self.assertEqual(state["transactions"][action["transaction_id"]]["phase"], "cancelled_unexecuted")
        self.assertIsNone(self.policy.step(self.obs))
        self.lua.execute("rear_a.valid=true")
        action = self.policy.step(self.obs)
        self.lua.execute("main[1].count=1")
        self.factory.ensure_full_magazines.return_value = {"status": "waiting", "reason": "actual_output_empty"}
        self.assertIsNone(self.policy.step(self.obs))
        self.factory.ensure_full_magazines.assert_called_with(self.obs, 10)

    def test_active_raid_reserve_changes_collection_target_but_verified_raid_does_not(self):
        raid = dict(
            world_id="w",
            catalog_fingerprint="c",
            phase="stock",
            ammo_item=RED,
            id="raid",
            requirements={RED: 200},
            wave={},
        )
        (self.root / "nest-sweep.json").write_text(json.dumps(raid))
        self.assertEqual(self.policy.step(self.obs)["type"], "take_factory_magazines")
        self.factory.ensure_full_magazines.assert_called_with(self.obs, 210)
        raid["phase"] = "verified"
        (self.root / "nest-sweep.json").write_text(json.dumps(raid))
        self.assertEqual(self.policy.step(self.obs)["type"], "upgrade_post_ammunition")

    def test_no_support_no_unknown_unit_no_factory_and_no_after_every_shot_topoff(self):
        self.factory.commissioned = False
        self.assertIsNone(self.policy.step(self.obs))
        self.assertFalse((self.root / POLICY_FILE).exists())
        self.factory.commissioned = True
        self.lua.execute("rear_a.health=1;rear_b.health=1")
        self.assertIsNone(self.policy.step(self.obs))
        self.assertFalse((self.root / POLICY_FILE).exists())
        self.lua.execute("rear_a.health=400;rear_b.health=400;front.unit_number=70")
        self.assertEqual(self.policy.step(self.obs)["status"], "blocked")
        self.lua.execute(
            'front.unit_number=7;for _,g in pairs(world)do g.get_inventory(1)[1]=slot("piercing-rounds-magazine",6,7)end'
        )
        self.assertIsNone(self.policy.step(self.obs))
        self.assertFalse((self.root / POLICY_FILE).exists())


if __name__ == "__main__":
    unittest.main()
