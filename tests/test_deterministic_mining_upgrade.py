from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_mining_upgrade import ensure_source_upgrade


class MiningUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), backend="assisted", query=Mock())
        self.bootstrap = Mock()
        self.catalog = SimpleNamespace(fingerprint="catalog", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.builder.ensure_plan = Mock(return_value={"type": "build", "name": "electric-mining-drill"})
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.obs = {"world_id": "one", "tick": 100, "inventory": {}, "entities": [],
                    "enabled_recipes": {"electric-mining-drill": True}}
        self.factory._sync(self.obs)
        self.old = {"name": "burner-mining-drill", "position": {"x": 10, "y": 6}, "direction": 0}
        self.receiver = {"name": "wooden-chest", "position": {"x": 9.5, "y": 4.5}}
        self.drill = {"name": "electric-mining-drill", "position": {"x": 9.5, "y": 6.5}, "direction": 0,
                      "_width": 3, "_height": 3}
        self.association = {"drill": self.old, "receiver": self.receiver, "extraction_block": "source:coal"}
        self.factory.state["blocks"]["source:coal"] = {"ok": True, "entities": [], "ports": [],
                                                        "active_source": deepcopy(self.association)}
        self.factory.state["automated_burners"] = [self.factory._entity_key(self.old)]
        self.live_old = {"present": True, "owned": True, "exhausted": True, "remaining": 0,
                         "feeds_receiver": True, "unit_number": 123}
        self.row = {"drill": self.drill, "remaining": 9000, "mixed": False, "terrain_clear": True,
                    "blocked": False, "power": {"position": {"x": 11.5, "y": 7.5}}, "can_place": False}
        self.survey = {"ok": True, "world_id": "one", "tick": 100, "old": self.live_old, "candidates": [self.row]}
        self.game.query.return_value = self.survey
        self.bootstrap.ensure_item.return_value = {"type": "craft", "recipe": "electric-mining-drill", "count": 1}

    def call(self):
        return ensure_source_upgrade(self.factory, self.obs, "coal", "coal")

    def reserve(self):
        self.assertEqual(self.call()["type"], "craft")
        return self.factory.state["source_upgrades"]["coal"]

    def test_reserves_existing_receiver_and_full_footprint_before_ordinary_craft(self):
        saved = self.reserve()
        self.assertEqual(saved["receiver"], self.receiver)
        self.assertEqual(saved["old_unit_number"], 123)
        self.assertEqual(saved["entities"], [self.drill])
        self.assertIn(self.drill, self.factory._reserved())
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "electric-mining-drill", 1)
        self.builder.ensure_plan.assert_not_called()
        self.assertEqual(self.factory.state["blocks"]["source:coal"]["active_source"], self.association)

    def test_existing_power_drills_and_unowned_raw_drills_are_never_reserved(self):
        for protection in ("primary", "expansion", "unowned"):
            with self.subTest(protection=protection):
                self.builder.state = {}
                self.factory.state["automated_burners"] = [self.factory._entity_key(self.old)]
                self.factory.state["blocks"].pop("energy:feed:1", None)
                if protection == "primary":
                    self.builder.state["coal_plan"] = {"drill": self.old}
                elif protection == "expansion":
                    self.factory.state["blocks"]["energy:feed:1"] = {"entities": [self.old]}
                else:
                    self.factory.state["automated_burners"] = []
                self.assertIsNone(self.call())
                self.game.query.assert_not_called()

    def test_disabled_recipe_or_nonexhausted_drill_cannot_start_upgrade(self):
        self.obs["enabled_recipes"] = {}
        self.assertIsNone(self.call())
        self.game.query.assert_not_called()
        self.obs["enabled_recipes"]["electric-mining-drill"] = True
        for field, value in (("exhausted", False), ("remaining", 1), ("feeds_receiver", False), ("owned", False)):
            old_value = self.live_old[field]
            self.live_old[field] = value
            self.assertIsNone(self.call())
            self.live_old[field] = old_value
        self.assertNotIn("source_upgrades", self.factory.state)

    def test_mixed_ore_terrain_foreign_footprints_and_unpowered_candidates_are_rejected(self):
        for field, value in (("mixed", True), ("terrain_clear", False), ("blocked", True), ("power", None), ("remaining", 0)):
            with self.subTest(field=field):
                before = self.row[field]
                self.row[field] = value
                self.assertIsNone(self.call())
                self.assertNotIn("source_upgrades", self.factory.state)
                self.row[field] = before
        self.factory.state["blocks"]["reserved belt"] = {"entities": [
            {"name": "transport-belt", "position": {"x": 9.5, "y": 6.5}}]}
        self.assertIsNone(self.call())

    def test_only_crafted_replacement_allows_guarded_exhausted_unit_mining(self):
        self.reserve()
        self.obs["inventory"]["electric-mining-drill"] = 1
        action = self.call()
        self.assertEqual((action["type"], action["name"], action["count"]), ("mine", "burner-mining-drill", 1))
        self.assertEqual(action["expected_world_id"], "one")
        self.assertEqual(action["expected_unit_number"], 123)
        self.assertEqual(action["exhausted_source_receiver"], self.receiver)
        self.assertEqual(action["required_replacement_item"], "electric-mining-drill")
        self.builder.ensure_plan.assert_not_called()

    def test_changed_unit_live_resources_or_receiver_fail_closed_after_reservation(self):
        self.reserve()
        self.obs["inventory"]["electric-mining-drill"] = 1
        for field, value in (("unit_number", 456), ("remaining", 2), ("exhausted", False), ("feeds_receiver", False)):
            with self.subTest(field=field):
                before = self.live_old[field]
                self.live_old[field] = value
                self.assertEqual(self.call()["status"], "blocked")
                self.live_old[field] = before
        self.factory.state["blocks"]["source:coal"]["active_source"]["receiver"] = {"name": "wooden-chest", "position": {"x": 50, "y": 50}}
        self.assertEqual(self.call()["status"], "blocked")

    def test_fresh_normal_placement_is_required_after_old_drill_disappears(self):
        self.reserve()
        self.obs["inventory"]["electric-mining-drill"] = 1
        self.survey["old"] = {"present": False}
        result = self.call()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("normal placement", result["reason"])
        self.builder.ensure_plan.assert_not_called()
        self.row["can_place"] = True
        self.assertEqual(self.call()["type"], "build")
        self.builder.ensure_plan.assert_called_once()

    def test_spilled_drill_contents_are_collected_before_fresh_normal_placement(self):
        self.reserve()
        self.obs["inventory"]["electric-mining-drill"] = 1
        dropped = {"name": "item-on-ground", "position": {"x": 10.3, "y": 5.5},
                   "item": "coal", "quality": "uncommon", "count": 73}
        self.row["ground_items"] = [dropped]
        self.assertEqual(self.call()["type"], "mine")
        self.survey["old"] = {"present": False}
        action = self.call()
        self.assertEqual(action, {"type": "take", "name": "item-on-ground", "position": dropped["position"],
                                 "item": "coal", "quality": "uncommon", "count": 50,
                                 "reason": "collect conserved ground items obstructing the replacement drill footprint"})
        self.builder.ensure_plan.assert_not_called()
        self.row["ground_items"] = []
        self.assertEqual(self.call()["status"], "blocked")
        self.row["can_place"] = True
        self.assertEqual(self.call()["type"], "build")

    def test_ground_pickup_cannot_clear_another_structure_or_skip_power_and_terrain(self):
        self.reserve()
        self.obs["inventory"]["electric-mining-drill"] = 1
        self.survey["old"] = {"present": False}
        self.row["ground_items"] = [{"name": "item-on-ground", "position": {"x": 10.3, "y": 5.5},
                                     "item": "coal", "quality": "normal", "count": 1}]
        for field, value in (("blocked", True), ("terrain_clear", False), ("power", None)):
            before = self.row[field]
            self.row[field] = value
            self.assertEqual(self.call()["status"], "blocked")
            self.row[field] = before
        self.builder.ensure_plan.assert_not_called()

    def seed_fuel_retirement(self):
        self.reserve()
        self.obs["inventory"]["electric-mining-drill"] = 1
        key = "fuel:" + self.factory._entity_key(self.old)
        arm = {"name": "inserter", "position": {"x": 11.5, "y": 5.5}, "direction": 4}
        infrastructure = [{"name": "transport-belt", "position": {"x": 12.5, "y": 5.5}},
                          {"name": "small-electric-pole", "position": {"x": 11.5, "y": 7.5}}]
        self.factory.state["blocks"][key] = {"ok": True, "entities": [arm, *infrastructure], "ports": [{"item": "coal"}]}
        proof = {"ok": True, "world_id": "one", "inserters": [{"present": True, "unit_number": 777,
                 "owned": True, "direction": 4, "inserter": True, "feeds_old_drill": True, "powered": True}]}
        mock = patch("factorio_ai.deterministic_mining_upgrade._fuel_intake_survey", return_value=proof)
        self.addCleanup(mock.stop)
        mock.start()
        return key, arm, infrastructure, proof

    def test_only_owned_fuel_arm_retires_before_old_drill_and_preserves_belts_poles_ports(self):
        key, arm, infrastructure, proof = self.seed_fuel_retirement()
        action = self.call()
        self.assertEqual((action["type"], action["name"], action["position"]), ("mine", "inserter", arm["position"]))
        self.assertEqual((action["expected_entity_unit"], action["expected_entity_world_id"]), (777, "one"))
        self.assertNotIn("retired_for_upgrade", self.factory.state["blocks"][key])
        saved = self.factory.state["source_upgrades"]["coal"]["fuel_retirement"]["inserters"][0]
        self.assertEqual((saved["unit_number"], saved["powered"]), (777, True))
        proof["inserters"] = [{"present": False}]
        self.assertEqual(self.call()["name"], "burner-mining-drill")
        plan = self.factory.state["blocks"][key]
        self.assertEqual(plan["entities"], infrastructure)
        self.assertEqual(plan["ports"], [{"item": "coal"}])
        self.assertEqual(plan["retired_for_upgrade"], "coal")
        result = self.factory._fuel_burner(self.obs, self.old, {})
        self.assertEqual(result["status"], "blocked")
        self.builder.ensure_plan.assert_not_called()

    def test_obsolete_fuel_arm_retires_before_repeated_ground_pickup_after_old_mine(self):
        _, arm, _, proof = self.seed_fuel_retirement()
        self.survey["old"] = {"present": False}
        self.row["ground_items"] = [{"name": "item-on-ground", "position": {"x": 10.3, "y": 5.5},
                                     "item": "coal", "quality": "normal", "count": 1}]
        self.assertEqual(self.call()["name"], "inserter")
        proof["inserters"] = [{"present": False}]
        self.assertEqual(self.call()["type"], "take")

    def test_fuel_retirement_rejects_changed_unit_world_foreign_force_or_extractor_drop(self):
        _, _, _, proof = self.seed_fuel_retirement()
        self.call()
        row = proof["inserters"][0]
        for field, value in (("unit_number", 778), ("owned", False), ("feeds_old_drill", False),
                             ("inserter", False), ("direction", 12)):
            before = row[field]
            row[field] = value
            self.assertEqual(self.call()["status"], "blocked")
            row[field] = before
        proof["world_id"] = "another"
        self.assertEqual(self.call()["status"], "blocked")

    def test_fuel_retirement_rejects_multiple_arms_and_shared_energy_or_extraction_ownership(self):
        key, arm, _, _ = self.seed_fuel_retirement()
        plan = self.factory.state["blocks"][key]
        plan["entities"].append({**arm, "position": {"x": 15.5, "y": 5.5}})
        self.assertEqual(self.call()["status"], "blocked")
        plan["entities"].pop()
        for other_key in ("energy:feed:1", "source:coal:extractor"):
            self.factory.state["blocks"][other_key] = {"entities": [arm]}
            self.assertEqual(self.call()["status"], "blocked")
            del self.factory.state["blocks"][other_key]

    def test_retired_fuel_arm_is_reconciled_after_restart_or_rollback_by_exact_unit(self):
        key, _, _, proof = self.seed_fuel_retirement()
        self.call()
        row = deepcopy(proof["inserters"][0])
        proof["inserters"] = [{"present": False}]
        self.call()
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        proof["inserters"] = [row]
        self.obs["tick"] = 50
        self.factory._sync(self.obs)
        self.assertEqual(self.call()["expected_entity_unit"], 777)
        row["unit_number"] = 778
        self.assertEqual(self.call()["status"], "blocked")

    def test_build_acknowledgement_cannot_claim_power_or_receiver_success(self):
        self.reserve()
        self.obs["inventory"]["electric-mining-drill"] = 1
        self.survey["old"] = {"present": False}
        self.row["can_place"] = True
        self.builder.ensure_plan.return_value = {"status": "succeeded", "evidence": {}}
        self.assertEqual(self.call()["status"], "waiting")
        self.row["actual"] = {"unit_number": 456, "owned": True, "direction": 0, "feeds_receiver": True, "powered": False}
        self.assertEqual(self.call()["status"], "waiting")
        self.row["actual"]["powered"] = True
        result = self.call()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["evidence"]["receiver"], self.receiver)
        self.assertFalse(result["evidence"]["flow_verified"])
        self.row["actual"]["feeds_receiver"] = False
        self.assertEqual(self.call()["status"], "blocked")

    def test_reservation_survives_restart_and_reconciles_rollback_without_budget_reset(self):
        self.reserve()
        self.factory.state["startup_research"] = {"electric-mining-drill": {"issued": 25}}
        self.factory._save()
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.assertEqual(self.call()["type"], "craft")
        self.obs["inventory"]["electric-mining-drill"] = 1
        self.assertEqual(self.call()["type"], "mine")
        self.factory.state["source_upgrades"]["coal"].update(state="observed", observed_tick=200, observed_unit_number=456)
        self.obs["tick"] = 50
        self.factory._sync(self.obs)
        self.assertNotIn("observed_unit_number", self.factory.state["source_upgrades"]["coal"])
        self.assertEqual(self.factory.state["startup_research"]["electric-mining-drill"]["issued"], 25)
        self.assertEqual(self.call()["type"], "mine")
        self.obs["world_id"] = "another"
        self.factory._sync(self.obs)
        self.assertNotIn("source_upgrades", self.factory.state)
        self.assertIsNone(self.call())

    def test_reserved_upgrade_is_blocked_when_world_or_power_changes(self):
        self.reserve()
        self.survey["world_id"] = "another"
        self.assertEqual(self.call()["status"], "blocked")
        self.survey["world_id"] = "one"
        self.row["power"] = None
        self.assertEqual(self.call()["status"], "blocked")

    def test_later_exhaustion_releases_preference_when_another_source_takes_over(self):
        self.reserve()
        self.survey["old"] = {"present": False}
        self.row["actual"] = {"unit_number": 456, "owned": True, "direction": 0, "feeds_receiver": True, "powered": True}
        self.row["remaining"] = 0
        self.assertIsNone(self.call())
        self.assertEqual(self.factory.state["source_upgrades"]["coal"]["state"], "retired")
        self.factory.state["blocks"]["source:coal"]["active_source"] = {
            "drill": {"name": "electric-mining-drill", "position": {"x": 50, "y": 50}},
            "receiver": {"name": "wooden-chest", "position": {"x": 50.5, "y": 48.5}}, "extraction_block": "other"}
        self.assertIsNone(self.call())
        self.assertNotIn("coal", self.factory.state["source_upgrades"])

    def test_observed_upgrade_requests_its_persisted_receiver_from_discovery(self):
        self.factory.state["blocks"]["source:coal"]["source_receiver"] = self.receiver
        self.bootstrap.discover_cell.return_value = {"ok": True, "complete": True, "receiver": self.receiver,
                                                     "drill": self.drill, "electric": True}
        self.factory.ensure_power_connection = Mock(return_value={"status": "succeeded"})
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.factory._fuel_burner = Mock()
        with patch("factorio_ai.deterministic_factory.ensure_source_upgrade", return_value={
                "status": "succeeded", "evidence": {"receiver": self.receiver}}):
            result = self.factory._source_endpoint(self.obs, "coal")
        self.assertEqual(result["status"], "succeeded")
        self.bootstrap.discover_cell.assert_called_once_with("coal", "wooden-chest", preferred_receiver=self.receiver)
        self.factory._fuel_burner.assert_not_called()
