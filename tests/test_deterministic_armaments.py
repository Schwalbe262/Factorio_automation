from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_armaments import Armaments
from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory


def ready(**evidence):
    return {"status": "succeeded", "evidence": evidence}


class ArmamentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), backend="assisted",
                                    query=Mock(return_value={"ok": True, "blocked": []}))
        recipe = {"name": "firearm-magazine", "ingredients": [{"name": "iron-plate", "amount": 4}],
                  "products": [{"name": "firearm-magazine", "amount": 1}]}
        self.catalog = SimpleNamespace(fingerprint="prototype-a", entities={}, technologies={}, recipes={"firearm-magazine": recipe},
                                       recipe_for_product=lambda item: recipe if item == "firearm-magazine" else None)
        self.bootstrap = Mock()
        self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.builder.ensure_plan = Mock(return_value=ready())
        self.builder.can_place = Mock(return_value={"ok": True})
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.factory.ensure_power_connection = Mock(return_value=ready())
        self.factory.ensure_product = Mock(side_effect=lambda obs, item, **kw: ready(ports=[
            {"kind": "item", "item": item, "direction": "output", "position": {"x": 20.5, "y": .5}, "facing": 4}]))
        self.factory.request_recipe_unlock = Mock(return_value={"status": "waiting", "reason": "queued mining research"})
        self.armaments = Armaments(self.game, self.bootstrap, self.builder, self.factory, self.catalog)
        self.turret = {"name": "gun-turret", "unit_number": 5, "position": {"x": 0, "y": 0}, "direction": 0,
                       "inventory": {"firearm-magazine": 20}}
        self.obs = {"world_id": "one", "tick": 100, "entities": [self.turret], "inventory": {},
                    "technologies": {"automation": True}, "enabled_recipes": {"electric-mining-drill": True}}
        self.factory._sync(self.obs)
        self.armaments._sync(self.obs)

    def test_no_turrets_or_pre_automation_does_not_preempt_science(self):
        self.obs["entities"] = []
        self.assertIsNone(self.armaments.next_action(self.obs))
        self.obs["entities"] = [self.turret]
        self.obs["technologies"] = {}
        self.assertIsNone(self.armaments.next_action(self.obs))
        self.assertFalse(self.armaments.manages_turret(self.turret))
        self.factory.ensure_product.assert_not_called()

    def test_existing_defense_stock_receives_no_additional_seed(self):
        self.obs["inventory"]["firearm-magazine"] = 50
        self.obs["enabled_recipes"] = {}
        result = self.armaments.next_action(self.obs)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(self.armaments.state["turrets"][self.armaments._key(self.turret)]["seed_remaining"], 0)
        self.bootstrap.ensure_item.assert_not_called()

    def test_seed_actions_are_bounded_even_if_all_ammo_is_consumed_between_frames(self):
        self.turret["inventory"] = {}
        self.obs["inventory"]["firearm-magazine"] = 5
        self.obs["enabled_recipes"] = {}
        first = self.armaments.next_action(self.obs)
        self.obs["tick"] += 1
        second = self.armaments.next_action(self.obs)
        self.obs["tick"] += 1
        third = self.armaments.next_action(self.obs)
        self.assertEqual(first["count"] + second["count"], 10)
        self.assertEqual(first["inventory"], "turret_ammo")
        self.assertNotIn("type", third)
        self.bootstrap.ensure_item.assert_not_called()

    def test_unlocked_mining_capacity_precedes_ammo_assembler_to_avoid_starving_its_science(self):
        self.obs["enabled_recipes"] = {}
        self.armaments.next_action(self.obs)
        self.factory.request_recipe_unlock.assert_called_once_with(self.obs, "electric-mining-drill")
        self.factory.ensure_product.assert_not_called()
        self.assertTrue(self.armaments.manages_turret(self.turret))
        self.assertFalse(self.armaments.owns_automated_turret(self.turret))

    def test_iron_capacity_includes_current_science_and_ammunition_demand(self):
        self.factory.state["capacity_science"] = ["automation-science-pack"]
        self.factory.graph = Mock()
        self.factory.graph._continuous_rates.return_value = ({"iron-plate": 60}, {})
        self.factory.ensure_product.return_value = {"type": "build", "name": "electric-mining-drill"}
        self.factory.ensure_product.side_effect = None
        result = self.armaments.next_action(self.obs)
        self.assertEqual(result["type"], "build")
        self.factory.ensure_product.assert_called_once_with(self.obs, "iron-plate", rate_per_minute=76)

    def test_intake_plan_uses_normal_entities_and_reserves_turret_and_belt_port(self):
        candidate = next(self.armaments._intake_candidates(self.turret))
        self.assertEqual({e["name"] for e in candidate["entities"]}, {"gun-turret", "inserter", "transport-belt", "small-electric-pole"})
        port = candidate["ports"][0]
        self.assertEqual((port["item"], port["direction"]), ("firearm-magazine", "input"))
        arm = next(e for e in candidate["entities"] if e["name"] == "inserter")
        self.assertEqual(arm["position"], {"x": -1.5, "y": .5})
        self.assertEqual(arm["direction"], 12)
        self.assertTrue(all(e["position"]["x"] % 1 == .5 for e in candidate["entities"] if e["name"] != "gun-turret"))

    def proof_row(self):
        row = self.armaments._track(self.turret)
        row["plan"] = next(self.armaments._intake_candidates(self.turret))
        self.armaments._supply_observation = Mock(return_value={"ok": True, "powered": True, "ammo": 20, "held": 0,
                                                             "producer_finished": 10, "tick": 100})
        return row

    def test_static_seed_and_construction_do_not_prove_automatic_supply(self):
        row = self.proof_row()
        self.assertEqual(self.armaments._verify_supply(self.obs, self.turret, row)["status"], "waiting")
        self.obs["production"] = {"firearm-magazine": {"produced": 1000}}
        self.armaments._supply_observation.return_value.update(tick=200, ammo=30)
        result = self.armaments._verify_supply(self.obs, self.turret, row)
        self.assertEqual(result["status"], "waiting")
        self.assertFalse(self.armaments.owns_automated_turret(self.turret))

    def test_ownership_requires_actual_assembler_output_and_turret_transfer(self):
        row = self.proof_row()
        self.armaments._verify_supply(self.obs, self.turret, row)
        self.armaments._supply_observation.return_value.update(tick=200, ammo=21, producer_finished=11)
        result = self.armaments._verify_supply(self.obs, self.turret, row)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(self.armaments.owns_automated_turret(self.turret))
        self.assertFalse(result["evidence"]["input_handcarry"])

    def test_observed_inserter_drop_proves_refill_even_when_combat_reduces_stock(self):
        row = self.proof_row()
        self.armaments._supply_observation.return_value["held"] = 1
        self.armaments._verify_supply(self.obs, self.turret, row)
        self.armaments._supply_observation.return_value.update(tick=200, ammo=19, held=0, producer_finished=11)
        self.assertEqual(self.armaments._verify_supply(self.obs, self.turret, row)["status"], "succeeded")

    def test_power_loss_or_empty_ammo_revokes_proven_ownership(self):
        row = self.proof_row()
        self.armaments._verify_supply(self.obs, self.turret, row)
        self.armaments._supply_observation.return_value.update(tick=200, ammo=21, producer_finished=11)
        self.armaments._verify_supply(self.obs, self.turret, row)
        self.armaments._supply_observation.return_value.update(tick=300, ammo=0)
        self.assertEqual(self.armaments._verify_supply(self.obs, self.turret, row)["status"], "waiting")
        self.assertFalse(self.armaments.owns_automated_turret(self.turret))
        row["owned"] = True
        self.armaments._supply_observation.return_value["powered"] = False
        self.armaments._verify_supply(self.obs, self.turret, row)
        self.assertFalse(self.armaments.owns_automated_turret(self.turret))
        self.assertNotIn("sample", row)

    def test_destroyed_and_rebuilt_producer_invalidates_old_cumulative_sample(self):
        row = self.proof_row()
        self.armaments._verify_supply(self.obs, self.turret, row)
        row["owned"] = True
        self.armaments._supply_observation.return_value.update(tick=200, producer_finished=1)
        self.assertEqual(self.armaments._verify_supply(self.obs, self.turret, row)["status"], "waiting")
        self.assertFalse(self.armaments.owns_automated_turret(self.turret))

    def test_missing_or_rotated_route_never_counts_as_rebuilt(self):
        row = self.proof_row()
        key = "armaments:" + self.armaments._key(self.turret)
        link = {"name": "transport-belt", "position": {"x": 10.5, "y": .5}, "direction": 4}
        self.factory.state["links"][key] = {"entities": [link]}
        self.obs["entities"] = deepcopy(row["plan"]["entities"]) + [deepcopy(link)]
        self.assertTrue(self.armaments._route_present(self.obs, key, row))
        self.obs["entities"][-1]["direction"] = 8
        self.assertFalse(self.armaments._route_present(self.obs, key, row))
        self.obs["entities"].pop()
        self.assertFalse(self.armaments._route_present(self.obs, key, row))

    def test_checkpoint_resume_keeps_seed_debit_but_rollback_discards_flow_proof(self):
        row = self.proof_row()
        row.update(owned=True, seed_remaining=0, sample={"tick": 100, "ammo": 20, "producer_finished": 10})
        self.armaments._save()
        resumed = Armaments(self.game, self.bootstrap, self.builder, self.factory, self.catalog)
        self.assertTrue(resumed.owns_automated_turret(self.turret))
        self.obs["tick"] = 10
        resumed._sync(self.obs)
        self.assertFalse(resumed.owns_automated_turret(self.turret))
        self.assertEqual(resumed.state["turrets"][resumed._key(self.turret)]["seed_remaining"], 0)

    def test_world_or_turret_identity_change_cannot_reuse_prior_ownership(self):
        row = self.proof_row()
        row["owned"] = True
        replacement = deepcopy(self.turret)
        replacement["unit_number"] = 99
        self.assertFalse(self.armaments.owns_automated_turret(replacement))
        self.obs["world_id"] = "two"
        self.armaments._sync(self.obs)
        self.assertFalse(self.armaments.manages_turret(self.turret))


if __name__ == "__main__":
    unittest.main()
