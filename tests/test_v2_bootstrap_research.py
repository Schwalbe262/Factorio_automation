"""Finite early machine science, without game calls or native research setters."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from factorio_agent.board import startup_science_cell
from factorio_agent.industry import BootstrapLogistics, validate_bootstrap_research
from factorio_agent.jobs import CAPABILITIES, Ledger, Request, StarterExecutors, run_slice, submit


class BootstrapResearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(side_effect=AssertionError("no native query"))
        )
        self.plan = startup_science_cell(10.5, 10.5)
        self.plan["reserved_area"] = [[2, 2], [18, 18]]
        self.board = dict(world_id="w", catalog_fingerprint="catalog", blocks={"startup_science": deepcopy(self.plan)})
        (self.root / "pcb-board.json").write_text(json.dumps(self.board), encoding="utf-8")
        self.recipes = {
            "automation-science-pack": dict(
                name="automation-science-pack",
                products=[dict(name="automation-science-pack", amount=1)],
                ingredients=[
                    dict(name="copper-plate", type="item", amount=1),
                    dict(name="iron-gear-wheel", type="item", amount=1),
                ],
            ),
            "iron-gear-wheel": dict(
                name="iron-gear-wheel",
                products=[dict(name="iron-gear-wheel", amount=1)],
                ingredients=[dict(name="iron-plate", type="item", amount=2)],
            ),
        }
        technologies = {
            name: dict(
                prerequisites=["automation-science-pack"],
                enabled=True,
                unit_count=count,
                ingredients=[dict(name="automation-science-pack", type="item", amount=1)],
            )
            for name, count in [
                ("logistics", 20),
                ("gun-turret", 10),
                ("stone-wall", 10),
                ("electric-mining-drill", 25),
                ("fast-inserter", 30),
                ("logistic-science-pack", 75),
                ("steel-processing", 50),
                ("lamp", 10),
                ("military", 10),
                ("radar", 20),
                ("repair-pack", 25),
            ]
        }
        self.catalog = SimpleNamespace(
            fingerprint="catalog", technologies=technologies, recipe_for_product=lambda name: self.recipes[name]
        )
        self.bootstrap = SimpleNamespace(
            ensure_item=Mock(
                side_effect=lambda obs, item, count: dict(
                    type="take", item=item, count=count, reason="normally collect real raw plates"
                )
            )
        )
        self.builder = SimpleNamespace(
            ensure_plan=Mock(return_value={"status": "succeeded"}),
            ensure_power=Mock(return_value={"status": "succeeded"}),
            _sync=Mock(),
        )
        entities = []
        for i, spec in enumerate(self.plan["entities"], 1):
            entities.append(
                dict(
                    deepcopy(spec),
                    unit_number=i,
                    electric_network_connected=True,
                    energy=1000,
                    products_finished=100,
                    inventory={},
                )
            )
        entities.append(
            dict(
                name="lab",
                unit_number=50,
                position={"x": 0.5, "y": 0.5},
                electric_network_connected=True,
                energy=1000,
                inventory={},
            )
        )
        self.obs = dict(
            ok=True,
            world_id="w",
            tick=1000,
            entities=entities,
            inventory={"iron-plate": 200, "copper-plate": 100},
            production={"automation-science-pack": {"produced": 100, "consumed": 30}},
            research=None,
            technologies={"automation": True, "automation-science-pack": True, "logistics": True},
            enabled_recipes={
                name: True
                for name in ("assembling-machine-1", "inserter", "lab", "automation-science-pack", "iron-gear-wheel")
            },
        )
        self.controller = self.make()

    def make(self, target="gun-turret"):
        return BootstrapLogistics(self.game, self.bootstrap, self.builder, self.catalog, technology=target)

    def fund(self, controller=None):
        controller = controller or self.controller
        for item in ("iron-plate", "copper-plate"):
            action = controller.step(self.obs)
            self.assertEqual((action["type"], action["item"]), ("insert", item))
            controller.record_action(action, {"ok": True, "moved": action["count"]})
        return controller

    def red(self):
        return next(e for e in self.obs["entities"] if e.get("recipe") == "automation-science-pack")

    def test_target_counts_and_raw_inputs_are_derived_and_separate_from_legacy_allowance(self):
        expected = {
            "gun-turret": (10, {"iron-plate": 20, "copper-plate": 10}),
            "stone-wall": (10, {"iron-plate": 20, "copper-plate": 10}),
            "electric-mining-drill": (25, {"iron-plate": 50, "copper-plate": 25}),
        }
        for name, requirements in expected.items():
            controller = self.make(name)
            self.assertEqual(controller.requirements(), requirements)
            self.assertEqual(controller.path.name, "bootstrap-research-" + name + ".json")
            self.assertEqual(controller.state, {})
        legacy = BootstrapLogistics(self.game, self.bootstrap, self.builder, self.catalog)
        self.assertEqual(legacy.path.name, "bootstrap-logistics.json")
        self.assertEqual(legacy.requirements(), (20, {"iron-plate": 40, "copper-plate": 20}))
        self.assertFalse(any(self.root.glob("bootstrap-research-*.json")))

    def test_actual_recipe_yields_change_finite_raw_budget(self):
        self.recipes["iron-gear-wheel"]["products"][0]["amount"] = 2
        self.assertEqual(self.controller.requirements(), (10, {"iron-plate": 10, "copper-plate": 10}))

    def test_general_red_only_targets_use_live_recipe_budgets_and_separate_paths(self):
        for target, count in [
            ("fast-inserter", 30),
            ("logistic-science-pack", 75),
            ("steel-processing", 50),
            ("lamp", 10),
            ("military", 10),
            ("radar", 20),
            ("repair-pack", 25),
        ]:
            with self.subTest(target=target):
                controller = self.make(target)
                self.assertEqual(controller.requirements(), (count, {"iron-plate": count * 2, "copper-plate": count}))
                self.assertEqual(controller.path.name, "bootstrap-research-" + target + ".json")
                action = controller.step(self.obs)
                self.assertEqual((action["type"], action["item"], action["count"]), ("insert", "iron-plate", count * 2))
                self.assertEqual(controller.state["allocated_inputs"], {})
                controller.record_action(action, dict(ok=True, moved=action["count"]))
                self.assertEqual(self.make(target).state["allocated_inputs"], {"iron-plate": count * 2})

    def test_new_target_prerequisites_multicolor_infinite_and_unknown_catalog_fail_before_cost(self):
        for target, update in [
            ("fast-inserter", {"prerequisites": ["missing-native-tech"]}),
            (
                "logistic-science-pack",
                {
                    "ingredients": [
                        dict(name="automation-science-pack", type="item", amount=1),
                        dict(name="logistic-science-pack", type="item", amount=1),
                    ]
                },
            ),
            ("steel-processing", {"unit_count": 101}),
            ("radar", {"count_formula": "L*20"}),
            ("repair-pack", {"research_trigger": {"type": "craft-item", "item": "repair-pack", "count": 1}}),
        ]:
            self.catalog.technologies[target].update(update)
            result = self.make(target).step(self.obs)
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(self.make(target).path.exists())
        unknown = self.make("not-installed")
        self.assertEqual(unknown.step(self.obs)["reason"], "bootstrap_research_rules_unsupported")
        self.builder.ensure_plan.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()
        self.game.query.assert_not_called()

    def test_new_target_keeps_native_new_machine_proof_and_acknowledged_budget(self):
        controller = self.make("fast-inserter")
        self.fund(controller)
        old = deepcopy(controller.state)
        self.obs["technologies"]["fast-inserter"] = True
        self.assertIsNone(controller.proof(self.obs))
        self.red()["products_finished"] += 30
        self.obs["production"]["automation-science-pack"]["consumed"] += 30
        proof = controller.proof(self.obs)
        self.assertEqual(proof["normally_assembled_packs"], 30)
        self.assertEqual(proof["allocated_normal_inputs"], {"iron-plate": 60, "copper-plate": 30})
        self.assertEqual(controller.state["baseline_products_finished"], old["baseline_products_finished"])

    def test_legacy_logistics_state_and_completion_behavior_are_not_migrated(self):
        old = dict(
            world_id="w",
            required_packs=20,
            allocated_inputs={"iron-plate": 40, "copper-plate": 20},
            baseline_produced=0,
            baseline_consumed=10,
        )
        legacy_path = self.root / "bootstrap-logistics.json"
        legacy_path.write_text(json.dumps(old), encoding="utf-8")
        before = legacy_path.read_bytes()
        legacy = BootstrapLogistics(self.game, self.bootstrap, self.builder, self.catalog)
        result = legacy.step(self.obs)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["evidence"]["technology"], "logistics")
        self.controller.step(self.obs)
        self.assertEqual(self.controller.state["allocated_inputs"], {})
        self.assertEqual(legacy_path.read_bytes(), before)

    def test_native_prerequisites_and_recipe_unlocks_gate_before_any_new_state_or_builder(self):
        for field, name in [
            ("technologies", "automation"),
            ("technologies", "automation-science-pack"),
            ("enabled_recipes", "automation-science-pack"),
        ]:
            with self.subTest(field=field, name=name):
                value = self.obs[field].pop(name)
                result = self.controller.step(self.obs)
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(self.controller.path.exists())
                self.obs[field][name] = value
        self.builder.ensure_plan.assert_not_called()
        self.catalog.technologies["gun-turret"]["prerequisites"].append("military")
        self.assertIn("military", self.controller.step(self.obs)["evidence"]["missing_technologies"])

    def test_non_red_trigger_formula_and_unbounded_count_rules_fail_closed(self):
        original = deepcopy(self.catalog.technologies["gun-turret"])
        for update in (
            {"unit_count": 0},
            {"unit_count": 101},
            {"unit_count": True},
            {"count_formula": "L*2"},
            {"research_trigger": {"type": "craft-item"}},
            {"ingredients": [dict(name="logistic-science-pack", type="item", amount=1)]},
        ):
            with self.subTest(update=update):
                self.catalog.technologies["gun-turret"] = {**original, **update}
                self.assertEqual(self.controller.step(self.obs)["reason"], "bootstrap_research_rules_unsupported")
                self.assertFalse(self.controller.path.exists())
        self.catalog.technologies["gun-turret"] = original

    def test_failed_native_reservation_blocks_and_unrelated_research_is_preserved(self):
        self.obs["research"] = "automation"
        self.assertEqual(self.controller.step(self.obs)["reason"], "another_research_is_running")
        self.obs["research"] = None
        self.controller.board.state["blocks"].clear()
        self.controller.board.reserve_startup_science = Mock(
            return_value={"ok": False, "reason": "no_energized_source_network"}
        )
        self.assertEqual(self.controller.step(self.obs)["reason"], "no_energized_source_network")
        self.game.query.assert_not_called()

    def test_gun_bridge_reuses_the_existing_cell_before_logistics_is_researched(self):
        self.obs["technologies"].pop("logistics")
        self.controller.board.reserve_startup_science = Mock(side_effect=AssertionError("do not move an existing cell"))
        action = self.controller.step(self.obs)
        self.assertEqual((action["type"], action["item"], action["count"]), ("insert", "iron-plate", 20))
        self.controller.board.reserve_startup_science.assert_not_called()

    def test_missing_cell_gets_normal_reservation_and_paid_construction_only_after_automation(self):
        self.controller.board.state["blocks"].clear()

        def reserve(obs):
            self.controller.board.state["blocks"]["startup_science"] = deepcopy(self.plan)
            return deepcopy(self.plan)

        self.controller.board.reserve_startup_science = Mock(side_effect=reserve)
        self.builder.ensure_plan.return_value = {
            "type": "build",
            "name": "assembling-machine-1",
            "position": {"x": 10.5, "y": 10.5},
        }
        self.obs["technologies"].pop("automation")
        self.assertEqual(self.controller.step(self.obs)["reason"], "bootstrap_research_native_prerequisites_missing")
        self.controller.board.reserve_startup_science.assert_not_called()
        self.obs["technologies"]["automation"] = True
        self.assertEqual(self.controller.step(self.obs)["type"], "build")
        self.controller.board.reserve_startup_science.assert_called_once_with(self.obs)
        self.assertFalse(self.controller.path.exists())
        self.bootstrap.ensure_item.assert_not_called()

    def test_disconnected_or_empty_machine_and_lab_power_blocks_funding_and_proof(self):
        lab = next(e for e in self.obs["entities"] if e["name"] == "lab")
        for entity, reason in ((self.red(), "science_cell_power_not_connected"), (lab, "powered_research_lab_missing")):
            for field, value in (("electric_network_connected", False), ("energy", 0)):
                with self.subTest(entity=entity["name"], field=field):
                    old = entity[field]
                    entity[field] = value
                    self.assertEqual(self.controller.step(self.obs)["reason"], reason)
                    self.assertFalse(self.controller.path.exists())
                    entity[field] = old
        self.fund()
        self.obs["technologies"]["gun-turret"] = True
        self.red()["products_finished"] += 10
        self.obs["production"]["automation-science-pack"]["consumed"] += 10
        self.red()["energy"] = 0
        self.assertIsNone(self.controller.proof(self.obs))

    def test_baseline_is_saved_before_funding_and_partial_receipts_cannot_reset_budget(self):
        first = self.controller.step(self.obs)
        saved = json.loads(self.controller.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["red_machine"]["unit_number"], self.red()["unit_number"])
        self.assertEqual(saved["baseline_products_finished"], 100)
        self.assertEqual(saved["allocated_inputs"], {})
        self.assertEqual(saved["pending_transfer"]["action"], first)
        self.controller.record_action(first, {"ok": True, "moved": 7})
        controller = self.make()
        remainder = controller.step(self.obs)
        self.assertEqual(remainder["count"], 13)
        controller.record_action(remainder, {"ok": True, "moved": 13})
        action = controller.step(self.obs)
        self.assertEqual(action["item"], "copper-plate")
        controller.record_action(action, {"ok": True, "moved": 10})
        self.assertEqual(controller.state["allocated_inputs"], {"iron-plate": 20, "copper-plate": 10})
        self.assertEqual(self.make().step(self.obs)["type"], "research")

    def test_unknown_or_invalid_transfer_receipt_blocks_replay_and_zero_is_explicit(self):
        action = self.controller.step(self.obs)
        before = self.controller.path.read_bytes()
        for receipt in ({"ok": True}, {"ok": True, "moved": 21}, {"ok": False, "reason": "timeout"}):
            with self.assertRaisesRegex(ValueError, "outcome unknown"):
                self.controller.record_action(action, receipt)
            self.assertEqual(self.controller.path.read_bytes(), before)
        self.assertEqual(
            self.make().step(self.obs)["reason"], "bootstrap_research_transfer_outcome_requires_reconciliation"
        )
        self.controller.record_action(action, {"ok": True, "moved": 0})
        self.assertEqual(self.controller.step(self.obs)["reason"], "bootstrap_research_normal_transfer_moved_zero")

    def test_machine_identity_world_catalog_and_original_plan_remain_pinned(self):
        self.fund()
        before = self.controller.path.read_bytes()
        self.red()["unit_number"] += 100
        self.assertEqual(self.controller.step(self.obs)["reason"], "bootstrap_research_red_machine_unit_changed")
        self.assertIsNone(self.controller.proof(self.obs))
        self.red()["unit_number"] -= 100
        self.obs["world_id"] = "other"
        self.assertEqual(self.controller.step(self.obs)["reason"], "bootstrap_research_world_changed")
        self.obs["world_id"] = "w"
        self.catalog.fingerprint = "other"
        self.assertEqual(self.controller.step(self.obs)["reason"], "bootstrap_research_catalog_changed")
        self.catalog.fingerprint = "catalog"
        self.controller.board.state["blocks"]["startup_science"]["ports"][0]["item"] = "stone"
        self.assertEqual(self.controller.step(self.obs)["reason"], "bootstrap_research_startup_plan_changed")
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_preexisting_machine_products_do_not_satisfy_new_target_and_science_consumption_required(self):
        self.fund()
        self.obs["technologies"]["gun-turret"] = True
        self.obs["production"]["automation-science-pack"]["consumed"] += 10
        self.assertIsNone(self.controller.proof(self.obs))
        self.assertEqual(self.controller.step(self.obs)["status"], "waiting")
        self.red()["products_finished"] += 10
        self.obs["production"]["automation-science-pack"]["consumed"] = 30
        self.assertIsNone(self.controller.proof(self.obs))
        self.obs["production"]["automation-science-pack"]["consumed"] = 40
        proof = self.controller.proof(self.obs)
        self.assertEqual(proof["normally_assembled_packs"], 10)
        self.assertEqual(proof["research_science_consumed"], 10)
        self.assertEqual(self.controller.step(self.obs)["status"], "succeeded")

    def test_machine_and_science_counter_rollback_or_nonfinite_values_cannot_prove_completion(self):
        self.fund()
        self.obs["technologies"]["gun-turret"] = True
        for value in (99, None, float("nan")):
            self.red()["products_finished"] = value
            self.assertEqual(self.controller.step(self.obs)["reason"], "bootstrap_research_machine_counter_rollback")
            self.assertIsNone(self.controller.proof(self.obs))
        self.red()["products_finished"] = 110
        for value in (29, None, float("nan")):
            self.obs["production"]["automation-science-pack"]["consumed"] = value
            self.assertEqual(self.controller.step(self.obs)["reason"], "bootstrap_research_science_counter_rollback")
            self.assertIsNone(self.controller.proof(self.obs))

    def test_target_finishing_mid_funding_still_gets_only_remaining_finite_machine_inputs(self):
        action = self.controller.step(self.obs)
        self.controller.record_action(action, {"ok": True, "moved": 20})
        self.obs["technologies"]["gun-turret"] = True
        action = self.controller.step(self.obs)
        self.assertEqual((action["type"], action["item"], action["count"]), ("insert", "copper-plate", 10))
        self.controller.record_action(action, {"ok": True, "moved": 10})
        self.assertEqual(self.controller.step(self.obs)["status"], "waiting")

    def test_already_researched_without_a_new_state_has_an_honest_no_funding_proof(self):
        self.obs["technologies"]["gun-turret"] = True
        self.assertEqual(self.controller.step(self.obs)["evidence"]["already_researched"], True)
        self.assertFalse(self.controller.path.exists())
        self.builder.ensure_plan.assert_not_called()

    def test_actor_science_is_not_used_until_collected_from_the_owned_machine_output(self):
        self.fund()
        self.obs["research"] = "gun-turret"
        self.obs["inventory"]["automation-science-pack"] = 100
        self.assertEqual(self.controller.step(self.obs)["status"], "waiting")
        output = next(
            e
            for e in self.obs["entities"]
            if e["name"] == "wooden-chest" and e["position"] == self.plan["ports"][-1]["position"]
        )
        output["inventory"]["automation-science-pack"] = 6
        action = self.controller.step(self.obs)
        self.assertEqual((action["type"], action["count"]), ("take", 6))
        self.controller.record_action(action, {"ok": True, "moved": 6})
        action = self.controller.step(self.obs)
        self.assertEqual((action["type"], action["name"], action["count"]), ("insert", "lab", 6))
        self.controller.record_action(action, {"ok": True, "moved": 6})
        self.assertEqual(self.controller.state["supplied_packs"], 6)

    def test_raw_shortage_collects_only_recipe_derived_raw_items_not_handcrafted_science(self):
        self.obs["inventory"] = {}
        action = self.controller.step(self.obs)
        self.assertEqual((action["type"], action["item"], action["count"]), ("take", "iron-plate", 20))
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "iron-plate", 20)

    def test_job_acknowledges_actual_moved_count_before_clearing_intent_and_preserves_unknown(self):
        self.obs["technologies"].pop("logistics")
        for moved in (7, None):
            with self.subTest(moved=moved), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                action = (
                    self.controller.step(self.obs)
                    if not self.controller.state
                    else deepcopy(self.controller.state["pending_transfer"]["action"])
                )
                game = SimpleNamespace(observe=lambda: deepcopy(self.obs), save=Mock())

                def act(result):
                    (root / "stop.json").write_text("{}", encoding="utf-8")
                    return {"ok": True, "moved": moved} if moved is not None else {"ok": True}

                game.act = act
                ledger = Ledger(root)
                ledger.data.update(world_id="w", last_tick=self.obs["tick"])
                ledger.data["jobs"]["power"] = dict(
                    key="power", world_id="w", capability="power", parameters={}, status="succeeded"
                )
                ledger.save()
                submit(
                    root,
                    game,
                    Request("target", "w", self.obs["tick"], "bootstrap_research", {"technology": "gun-turret"}),
                )
                controller = self.controller
                executor = SimpleNamespace(
                    step=lambda *args: deepcopy(action),
                    record_action=lambda cap, obs, result, receipt, params: controller.record_action(result, receipt),
                )
                if moved is None:
                    with self.assertRaisesRegex(ValueError, "outcome unknown"):
                        run_slice(root, game, executor, "target", seconds=0.1)
                    job = Ledger(root).data["jobs"]["target"]
                    self.assertEqual(job["pending_action"], action)
                    self.assertEqual(job["reason"], "action_outcome_unknown_reconcile_required")
                    self.assertEqual(self.controller.state["allocated_inputs"], {"iron-plate": 7})
                else:
                    run_slice(root, game, executor, "target", seconds=0.1)
                    self.assertEqual(self.controller.state["allocated_inputs"], {"iron-plate": 7})
                    self.assertNotIn("pending_action", Ledger(root).data["jobs"]["target"])
                    # Reserve only the finite remainder for the unknown case.
                    self.controller.step(self.obs)


class BootstrapResearchDispatchTests(unittest.TestCase):
    def test_request_schema_is_finite_targeted_and_requires_only_verified_power(self):
        self.assertEqual(CAPABILITIES["bootstrap_research"]["requires"], ["power"])
        for name in (
            "gun-turret",
            "stone-wall",
            "electric-mining-drill",
            "fast-inserter",
            "logistic-science-pack",
            "steel-processing",
            "military",
        ):
            Request("x", "w", 1, "bootstrap_research", {"technology": name}).validate()
        for parameters in (
            {},
            {"technology": "../automation"},
            {"technology": "x;game.speed=2"},
            {"technology": ""},
            {"technology": []},
            {"technology": None},
            {"technology": "A" * 81},
            {"technology": "gun-turret", "count": 200},
        ):
            with self.assertRaises(ValueError):
                Request("x", "w", 1, "bootstrap_research", parameters).validate()

    def test_bridge_does_not_run_remote_power_or_starter_repairs_before_local_science(self):
        ex = StarterExecutors.__new__(StarterExecutors)
        ex.builder = SimpleNamespace(_sync=Mock(), ensure_power=Mock(return_value={"status": "succeeded"}))
        ex.starter_observation = Mock(side_effect=AssertionError("remote starter upkeep must not run"))
        ex.starter_upkeep = Mock(side_effect=AssertionError("remote chest/drill repair must not block research"))
        ex._continue_background_construction = Mock(return_value=None)
        controller = SimpleNamespace(
            step=Mock(return_value={"type": "research", "technology": "gun-turret"}),
            proof=Mock(return_value={"technology": "gun-turret"}),
            record_action=Mock(),
        )
        ex.bootstrap_research_targets = {"gun-turret": controller}
        with patch("factorio_agent.upgrades.next_upgrade") as upgrade:
            result = ex.step("bootstrap_research", {}, {"technology": "gun-turret"})
        self.assertEqual(result["technology"], "gun-turret")
        upgrade.assert_not_called()
        ex.builder.ensure_power.assert_not_called()
        ex.starter_upkeep.assert_not_called()
        ex.starter_observation.assert_not_called()
        self.assertEqual(
            ex.proof("bootstrap_research", {}, result, {"technology": "gun-turret"}), {"technology": "gun-turret"}
        )
        ex.record_action(
            "bootstrap_research", {}, {"type": "insert"}, {"ok": True, "moved": 1}, {"technology": "gun-turret"}
        )
        controller.record_action.assert_called_once()


if __name__ == "__main__":
    unittest.main()
