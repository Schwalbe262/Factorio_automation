import unittest
from unittest.mock import patch

from factorio_agent.bastion_maintenance import BastionMaintenance
from factorio_agent.defense_hardening import _key
import test_v2_defense_hardening as fixtures


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        fixtures.HardeningControllerTests.setUp(self)
        fixtures.HardeningControllerTests.existing_guns(self, stocks=(20, 20, 20), walls=True)
        self.c.state["phase"] = "verified"
        self.c._save()
        self.game.expected_world = "w"
        self.game.backend = "assisted"
        self.obs["actor_unit_number"] = 10915
        self.m = BastionMaintenance(self.game, self.bootstrap, self.catalog, self.perimeter, self.c)

    install = fixtures.HardeningControllerTests.install
    receipt = fixtures.HardeningControllerTests.receipt

    def lose(self, count):
        guns = [e for e in self.obs["entities"] if e["name"] == "gun-turret"][:count]
        for e in guns:
            self.obs["entities"].remove(e)
        return guns

    def test_all_three_missing_uses_one_group_with_all_materials_prepaid(self):
        self.lose(3)
        action = self.m.step(self.obs)
        self.assertEqual(action["type"], "build_and_arm_bastion_group")
        self.assertEqual(len(action["deployments"]), 3)
        self.assertTrue(all(q["ammo_item"] == "firearm-magazine" for q in action["deployments"]))
        self.assertFalse(
            any(
                p.get("unit_number")
                for p in self.perimeter.state["posts"]
                if p.get("coverage_scope") == self.c.state["scope_id"]
            )
        )

    def test_two_missing_prepays_both_before_single_and_never_retargets_another_scope(self):
        self.lose(2)
        self.obs["inventory"]["gun-turret"] = 1
        self.assertEqual(self.m.step(self.obs), {"type": "craft", "recipe": "gun-turret", "count": 2})
        self.obs["inventory"]["gun-turret"] = 2
        self.actor_full = 20
        self.assertEqual(self.m.step(self.obs)["type"], "take_full_magazines")
        self.assertEqual(self.perimeter._ensure_magazines.call_args.args[2], 40)
        self.actor_full = 40
        action = self.m.step(self.obs)
        self.assertEqual(action["type"], "build_and_arm_bastion_post")
        self.assertIn(action["post_name"], [p["name"] for p in self.c.state["posts"]])

    def test_healthy_cluster_returns_none_and_ignores_unrelated_missing_post(self):
        for p in self.c.state["posts"]:
            self.c.state.setdefault("stocked_units", {})[p["name"]] = self.c.state["bindings"][
                _key(dict(p, name="gun-turret"))
            ]
        self.c._save()
        with patch.object(self.c, "_receipts") as read:
            self.assertIsNone(self.m.step(self.obs))
            read.assert_not_called()

    def test_unproven_or_foreign_replacement_never_gets_atomic_deployment(self):
        self.lose(1)
        self.c.state["unit_build_receipts"] = {}
        self.c._save()
        (self.root / "job-events.jsonl").unlink()
        self.assertIn("paid_build_proof", self.m.step(self.obs)["reason"])

    def test_next_observation_binds_paid_group_and_credits_delivery_consumed_in_combat(self):
        self.lose(3)
        group = self.m.step(self.obs)
        pairs = []
        for i, post in enumerate(self.c.state["posts"]):
            action, result = fixtures.HardeningControllerTests.atomic_birth(self, post, 900 + i, actor=10915)
            pairs.append((action, result))
            self.obs["entities"].append(dict(name="gun-turret", position=post["position"], unit_number=900 + i))
            self.full[900 + i] = 1
        self.receipt(group, dict(paid_this_call=3, deployments=[r for a, r in pairs]))
        self.assertIsNone(self.m.step(self.obs))
        for i, post in enumerate(self.c.state["posts"]):
            self.assertEqual(self.c.state["bindings"][_key(dict(post, name="gun-turret"))], 900 + i)
            self.assertEqual(self.c.state["stocked_units"][post["name"]], 900 + i)
            self.assertEqual(
                sum(v["moved"] for v in self.c.state["stock_delivery_receipts"][str(900 + i)].values()), 20
            )

    def test_unverified_or_changed_world_is_not_built(self):
        self.c.state["phase"] = "walls"
        self.c._save()
        self.assertIsNone(self.m.step(self.obs))
        self.c.state["phase"] = "verified"
        self.c._save()
        self.obs["world_id"] = "foreign"
        self.assertEqual(self.m.step(self.obs)["reason"], "bastion_maintenance_world_or_catalog_changed")


if __name__ == "__main__":
    unittest.main()
