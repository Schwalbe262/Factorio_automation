from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_game import DeterministicGame, run_config, validate_mine_guard
from factorio_ai.deterministic_lab_migration import PRIMARY, REPLACEMENT, ensure_lab_migration
from factorio_ai.factory_templates import build_template


READY = {"status": "succeeded", "reason": "observed", "evidence": {}}


class LabMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), backend="character", query=Mock())
        self.bootstrap = Mock()
        self.catalog = SimpleNamespace(fingerprint="catalog", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.obs = {"world_id": "world", "tick": 100, "inventory": {}, "entities": [],
                    "technologies": {"automation": True, "electric-mining-drill": True}}
        self.factory._sync(self.obs)
        self.packs = ["automation-science-pack", "logistic-science-pack"]
        self.old = build_template("labs_row", inputs=self.packs, anchor={"x": -27.5, "y": -37.5})
        self.old["key"] = PRIMARY
        self.new = build_template("labs_row", inputs=self.packs, anchor={"x": -5.5, "y": 7.5})
        self.new.update(key=REPLACEMENT, production_area=[[-10, 0], [0, 12]])
        self.factory.state["blocks"][PRIMARY] = deepcopy(self.old)
        self.factory.reserve_site = Mock(side_effect=self.reserve)
        self.builder.ensure_plan = Mock(return_value={"type": "craft", "recipe": "lab", "count": 1})
        self.factory.ensure_power_connection = Mock(return_value=READY)
        self.live = {"ok": True, "world_id": "world", "tick": 100, "research": None, "recoverable": True,
                     "old": {"present": True, "owned": True, "unit_number": 567, "covered": 9,
                             "empty": True, "unconnected": True},
                     "replacement": {"present": True, "ready": True, "powered": True, "unit_number": 700}}
        self.game.query.side_effect = lambda body: deepcopy(self.live)

    def reserve(self, origin, key, obs, reference=None):
        self.factory.state["blocks"].setdefault(key, deepcopy(self.new))
        self.factory._save()
        return self.factory.state["blocks"][key]

    def call(self):
        return ensure_lab_migration(self.factory, self.obs)

    def prepared(self):
        self.builder.ensure_plan.return_value = READY
        return self.call()

    def test_original_reserved_until_normal_replacement_materials_and_power_complete(self):
        self.assertEqual(self.call(), {"type": "craft", "recipe": "lab", "count": 1})
        self.assertEqual(self.factory.state["blocks"][PRIMARY], self.old)
        self.assertIn(REPLACEMENT, self.factory.state["blocks"])
        record = self.factory.state["lab_migration"]
        self.assertEqual((record["old_unit_number"], record["state"]), (567, "building"))
        self.builder.ensure_plan.assert_called_once_with(self.obs, self.new)
        self.factory.ensure_power_connection.assert_not_called()
        self.builder.ensure_plan.return_value = READY
        self.factory.ensure_power_connection.return_value = {"type": "build", "name": "small-electric-pole"}
        self.assertEqual(self.call()["name"], "small-electric-pole")
        self.assertEqual(self.factory.state["blocks"][PRIMARY], self.old)

    def test_startup_and_nonoverlapping_old_labs_are_unchanged(self):
        self.obs["technologies"]["electric-mining-drill"] = False
        self.assertIsNone(self.call())
        self.game.query.assert_not_called()
        self.obs["technologies"]["electric-mining-drill"] = True
        self.live["old"]["covered"] = 0
        self.assertIsNone(self.call())
        self.factory.reserve_site.assert_not_called()

    def test_identity_idle_inventory_and_connection_guards_fail_before_reservation(self):
        original = deepcopy(self.live)
        for section, key, value in (("old", "owned", False), ("old", "unit_number", None),
                ("old", "empty", False), ("old", "unconnected", False),
                (None, "research", "logistics"), (None, "world_id", "other"), (None, "ok", False)):
            with self.subTest(section=section, key=key):
                self.live = deepcopy(original)
                (self.live[section] if section else self.live)[key] = value
                self.assertEqual(self.call()["status"], "blocked")
                self.assertNotIn("lab_migration", self.factory.state)
        self.factory.reserve_site.assert_not_called()

    def test_saved_input_link_is_never_abandoned_even_if_live_belts_are_empty(self):
        self.factory.state["links"]["lab:automation-science-pack"] = {"consumer_port": self.old["ports"][0]}
        self.assertEqual(self.call()["status"], "blocked")
        self.factory.reserve_site.assert_not_called()

    def test_restart_resumes_same_replacement_and_rejects_changed_old_unit(self):
        self.call()
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.factory.ensure_power_connection = Mock(return_value=READY)
        with patch.object(self.factory, "reserve_site", wraps=self.factory.reserve_site) as reserve:
            self.assertEqual(self.call()["type"], "craft")
            self.assertEqual(reserve.call_args.args[1], REPLACEMENT)
        self.live["old"]["unit_number"] = 568
        self.builder.ensure_plan.reset_mock()
        self.assertEqual(self.call()["status"], "blocked")
        self.builder.ensure_plan.assert_not_called()

    def test_atomic_swap_preserves_shared_infrastructure_and_mines_only_original_lab(self):
        shared = self.old["entities"][-1]
        self.factory.state["blocks"]["energy:feed:1"] = {"entities": [shared], "ports": []}
        self.factory.state["links"]["coal"] = {"entities": [shared]}
        self.factory.state["power_links"][PRIMARY] = {"entities": [shared]}
        self.factory.state["power_links"][REPLACEMENT + ":pole:1,2"] = {"entities": [self.new["entities"][-1]]}
        action = self.prepared()
        validate_mine_guard(action)
        self.assertEqual((action["type"], action["name"], action["expected_entity_unit"]), ("mine", "lab", 567))
        self.assertEqual(action["lab_replacement"]["unit_number"], 700)
        self.assertEqual(action["position"], self.old["entities"][0]["position"])
        record = self.factory.state["lab_migration"]
        retired = self.factory.state["blocks"][record["retired_key"]]
        self.assertEqual(retired["entities"], self.old["entities"])
        self.assertEqual(retired["ports"], [])
        self.assertEqual(record["old_plan"], self.old)
        self.assertNotIn(REPLACEMENT, self.factory.state["blocks"])
        self.assertIn(record["retired_key"], self.factory.state["power_links"])
        self.assertIn(PRIMARY + ":pole:1,2", self.factory.state["power_links"])
        self.assertEqual(self.factory.state["links"]["coal"]["entities"], [shared])
        recovered = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.assertEqual(recovered.state, self.factory.state)

    def test_readiness_race_or_full_inventory_cannot_retire_original(self):
        self.builder.ensure_plan.return_value = READY
        for key in ("ready", "powered", "unit_number"):
            with self.subTest(key=key):
                before = self.live["replacement"][key]
                self.live["replacement"][key] = False
                self.assertEqual(self.call()["status"], "blocked")
                self.assertEqual(self.factory.state["blocks"][PRIMARY], self.old)
                self.live["replacement"][key] = before
        self.live["recoverable"] = False
        self.assertEqual(self.call()["status"], "blocked")
        self.assertIn(self.old["entities"][0], self.factory._reserved())

    def test_changed_old_input_between_surveys_blocks_atomic_switch(self):
        self.builder.ensure_plan.return_value = READY
        changed = deepcopy(self.live)
        changed["old"]["empty"] = False
        self.game.query.side_effect = [deepcopy(self.live), changed]
        self.assertEqual(self.call()["status"], "blocked")
        self.assertEqual(self.factory.state["blocks"][PRIMARY], self.old)

    def test_observed_absence_releases_only_lab_footprint_and_rollback_rereserves_it(self):
        self.prepared()
        self.live["old"]["present"] = False
        self.assertIsNone(self.call())
        record = self.factory.state["lab_migration"]
        retired = self.factory.state["blocks"][record["retired_key"]]
        self.assertEqual(record["state"], "retired")
        self.assertEqual(retired["entities"], self.old["entities"][1:])
        self.assertNotIn("lab", retired["required_items"])
        self.assertNotIn(self.old["entities"][0], self.factory._reserved())
        self.obs["tick"] = 50
        self.factory._sync(self.obs)
        self.live["old"]["present"] = True
        self.builder.ensure_plan.return_value = {"type": "build", "name": "lab"}
        self.assertEqual(self.call()["type"], "build")
        self.assertEqual(record["state"], "retiring")
        self.assertIn(self.old["entities"][0], self.factory._reserved())
        self.builder.ensure_plan.return_value = READY
        self.assertEqual(self.call()["expected_entity_unit"], 567)

    def test_original_disappearing_during_partial_build_does_not_claim_replacement_ready(self):
        self.call()
        self.live["old"]["present"] = False
        self.assertEqual(self.call()["type"], "craft")
        self.assertEqual(self.factory.state["lab_migration"]["state"], "building")
        self.builder.ensure_plan.return_value = READY
        self.assertIsNone(self.call())
        self.assertEqual(self.factory.state["lab_migration"]["state"], "retired")

    def test_new_world_does_not_adopt_existing_lab_on_resource_patch(self):
        self.factory.state["blocks"].clear()
        self.factory.graph = Mock()
        self.factory.graph.for_first_rocket.return_value = {"bom": {"science_packs": {pack: 1 for pack in self.packs}}}
        self.obs["entities"] = [deepcopy(self.old["entities"][0])]
        with patch("factorio_ai.deterministic_lab_migration.lab_site_clear", return_value=False):
            self.factory._lab_plan(self.obs)
        self.assertEqual(self.factory.state["blocks"][PRIMARY]["entities"], self.new["entities"])

    def test_invalid_atomic_retirement_guard_is_rejected_before_any_game_query(self):
        action = self.prepared()
        game = DeterministicGame(run_config(runtime=Path(self.temp.name)))
        invalid = [dict(action, lab_replacement=value) for value in (None, {},
            {"unit_number": 567, "position": {"x": 1, "y": 1}},
            {"unit_number": True, "position": {"x": 1, "y": 1}},
            {"unit_number": 700, "position": {"x": float("nan"), "y": 1}})]
        invalid += [{k: v for k, v in action.items() if k == "lab_replacement" or not k.startswith("expected_entity")}]
        with patch.object(game, "query") as query:
            for candidate in invalid:
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    game.act(candidate)
            query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
