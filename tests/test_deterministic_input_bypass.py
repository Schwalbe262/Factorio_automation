from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_input_bypass import (
    _key, describe_bypass_entity, input_bypass_sync_error,
    resume_input_bypass, start_input_bypass,
)
from factorio_ai.deterministic_underground_bypass import propose_collinear_bypasses
from tests.test_deterministic_underground_bypass import chain


class InputBypassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.tmp.name)),
                                    backend="assisted", query=Mock(), act=Mock(side_effect=AssertionError("no actions")))
        self.catalog = SimpleNamespace(fingerprint="catalog", entities={})
        self.bootstrap = SimpleNamespace(ensure_item=Mock(return_value={"type": "craft", "recipe": "underground-belt", "count": 2}))
        self.builder = SimpleNamespace(construction_materials=None, ensure_plan=Mock(side_effect=AssertionError("no recursive builder")))
        self.builder.catalog = self.catalog
        self.builder._occupied_by_plan = lambda rows: FactoryBuilder._occupied_by_plan(self.builder, rows)
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.obs = {"world_id": "world", "tick": 100, "actor_unit_number": 58, "inventory": {}}
        self.factory._sync(self.obs)
        self.plan, path, _ = chain()
        self.proposal = propose_collinear_bypasses(self.plan, path, max_distance=5, other_plans=[])[0]
        self.factory.state["links"]["input"] = deepcopy(self.plan)
        self.factory._save()
        self.units = 200
        self.survey = {"ok": True, "world_id": "world", "tick": 100, "actor_unit_number": 58,
                       "max_distance": 5, "topology_clear": True,
                       "normal_inventory": {"transport-belt": 20, "underground-belt": 20},
                       "entry": self.live(self.proposal["entry"]["old"]), "exit": self.live(self.proposal["exit"]),
                       "retained_segment": [self.live(spec) for spec in self.proposal["retained_segment"]],
                       "new_entities": [{"present": False, "can_place": True} for _ in self.proposal["new_entities"]]}
        self.game.query.side_effect = lambda body: deepcopy(self.survey)

    def live(self, spec):
        self.units += 1
        return {**deepcopy(spec), "unit_number": self.units, "present": True, "owned": True,
                "healthy": True, "pure": True}

    @property
    def record(self):
        return self.factory.state["input_bypasses"]["input"]

    def reserve(self):
        result = start_input_bypass(self.factory, "input", self.proposal, self.obs)
        self.assertEqual(result["status"], "waiting", result)
        return result

    def advance(self):
        self.obs["tick"] += 1
        self.survey["tick"] = self.obs["tick"]

    def choose(self, critical=False):
        self.advance()
        return resume_input_bypass(self.factory, self.obs, critical_only=critical)

    def install(self, action):
        self.assertEqual(action["type"], "build", action)
        specs = self.proposal["new_entities"]
        index = next(i for i, spec in enumerate(specs) if spec["position"] == action["position"])
        self.survey["new_entities"][index] = self.live(specs[index])
        self.survey["normal_inventory"][action["item"]] -= 1
        self.pair_rows()

    def pair_rows(self):
        rows = {_key(spec): row for spec, row in zip(self.proposal["new_entities"], self.survey["new_entities"])}
        for pair in self.proposal["plan"]["underground_pairs"]:
            if _key(pair["input"]) not in rows:
                continue
            for side, opposite in (("input", "output"), ("output", "input")):
                row, other = rows[_key(pair[side])], rows[_key(pair[opposite])]
                if row["present"] and other["present"]:
                    row.update(underground_pair_verified=True, max_underground_distance=5,
                               underground_neighbour={**describe_bypass_entity(other), "reciprocal": True})

    def prepare(self):
        self.reserve()
        for _ in self.proposal["new_entities"]:
            self.install(self.choose())
        action = self.choose()
        self.assertEqual(action["type"], "mine", action)
        return action

    def reload(self):
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)

    def mine_and_build(self):
        self.survey["entry"] = {"present": False, "can_place": True}
        build = self.choose(critical=True)
        self.assertEqual(build["type"], "build", build)
        self.assertEqual(build["direction"], self.proposal["entry"]["new"]["direction"])
        self.survey["entry"] = self.live(self.proposal["entry"]["new"])
        return build

    def publish(self):
        self.prepare()
        self.mine_and_build()
        result = self.choose(critical=True)
        self.assertEqual(result["status"], "waiting", result)
        self.assertEqual(self.record["phase"], "published")
        return result

    def test_reservation_does_not_change_canonical_plan_and_reserves_all_pending_pieces(self):
        self.reserve()
        self.assertEqual(self.factory.state["links"]["input"], self.plan)
        for spec in self.proposal["new_entities"]:
            self.assertIn(spec, self.factory._reserved())
        self.assertIsNone(resume_input_bypass(self.factory, self.obs, critical_only=True))
        self.bootstrap.ensure_item.assert_not_called()
        self.game.act.assert_not_called()

    def test_finite_bill_includes_all_remaining_pieces_and_final_entry_before_any_build(self):
        self.reserve()
        self.survey["normal_inventory"] = {"transport-belt": 0, "underground-belt": 0}
        bill = Counter(spec["name"] for spec in self.proposal["new_entities"])
        action = self.choose()
        self.assertEqual(action["type"], "craft")
        self.assertEqual(self.bootstrap.ensure_item.call_args.args[1:], ("transport-belt", bill["transport-belt"] + 1))
        self.assertEqual(self.record["phase"], "preparing")
        self.survey["normal_inventory"]["transport-belt"] = bill["transport-belt"] + 1
        self.choose()
        self.assertEqual(self.bootstrap.ensure_item.call_args.args[1:], ("underground-belt", bill["underground-belt"]))

    def test_preparation_may_use_ordinary_material_bridge_but_keeps_old_plan(self):
        self.reserve()
        self.builder.construction_materials = SimpleNamespace(ensure=Mock(return_value={"type": "take", "count": 2}))
        self.survey["normal_inventory"]["transport-belt"] = 0
        self.assertEqual(self.choose()["type"], "take")
        self.builder.construction_materials.ensure.assert_called_once()
        self.assertEqual(self.factory.state["links"]["input"], self.plan)

    def test_later_survey_epoch_does_not_look_like_rollback_inside_material_procurement(self):
        self.reserve()
        self.survey["tick"] = self.obs["tick"] + 86
        self.survey["normal_inventory"]["transport-belt"] = 0
        def procurement(fresh, item, count):
            self.factory._sync(fresh)
            return {"type": "craft", "recipe": item, "count": count}
        self.bootstrap.ensure_item.side_effect = procurement
        result = resume_input_bypass(self.factory, self.obs)
        self.assertEqual(result["type"], "craft", result)
        self.assertEqual(self.factory.state["last_tick"], self.survey["tick"])

    def test_switching_intent_is_durable_before_guarded_normal_mine(self):
        action = self.prepare()
        stored = json.loads(self.factory.path.read_text())["input_bypasses"]["input"]
        self.assertEqual(stored["phase"], "switching")
        self.assertEqual(stored["entry_state"], "mine")
        self.assertEqual(action["expected_entity_unit"], self.survey["entry"]["unit_number"])
        guard = action["belt_route_replacement"]
        self.assertEqual(guard["entry_direction"], self.proposal["entry"]["old"]["direction"])
        self.assertNotIn(guard["exit"], guard["entities"])
        self.assertEqual(len(guard["entities"]), 6)
        self.assertEqual(len(guard["pairs"]), 2)
        self.assertEqual(self.factory.state["links"]["input"], self.plan)

    def test_existing_canonical_underground_pairs_are_preserved_but_not_added_to_mine_guard(self):
        old_input = {"name": "underground-belt", "position": {"x": 50.5, "y": .5},
                     "direction": 0, "belt_to_ground_type": "input"}
        old_output = {**old_input, "position": {"x": 50.5, "y": -2.5}, "belt_to_ground_type": "output"}
        old_pair = {"input": old_input, "output": old_output, "max_distance": 5}
        self.plan["entities"].extend((old_input, old_output))
        self.plan["underground_pairs"] = [old_pair]
        path = [self.proposal["entry"]["old"], *self.proposal["retained_segment"], self.proposal["exit"]]
        self.proposal = propose_collinear_bypasses(self.plan, path, max_distance=5, other_plans=[])[0]
        self.factory.state["links"]["input"] = deepcopy(self.plan)
        action = self.prepare()
        self.assertEqual(len(action["belt_route_replacement"]["pairs"]), 2)
        self.assertEqual(len(self.proposal["plan"]["underground_pairs"]), 3)
        self.assertIn(old_pair, self.proposal["plan"]["underground_pairs"])

    def test_restart_after_mine_and_after_build_never_calls_ancestor_builder_or_procurement(self):
        self.prepare()
        self.reload()
        self.bootstrap.ensure_item.side_effect = AssertionError("no critical procurement")
        self.builder.construction_materials = SimpleNamespace(ensure=Mock(side_effect=AssertionError("no critical material bridge")))
        self.mine_and_build()
        self.reload()
        result = self.choose(critical=True)
        self.assertEqual(result["status"], "waiting", result)
        self.assertEqual(self.factory.state["links"]["input"], self.proposal["plan"])
        self.assertEqual(self.record["new_entry_unit"], self.survey["entry"]["unit_number"])
        self.builder.ensure_plan.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()

    def test_publication_retains_every_old_piece_and_requires_reobservation(self):
        result = self.publish()
        self.assertFalse(result["evidence"]["flow_verified"])
        self.assertNotIn(self.record["pending_key"], self.factory.state["blocks"])
        for spec in self.plan["entities"]:
            if spec != self.proposal["entry"]["old"]:
                self.assertIn(spec, self.factory._reserved())
        self.assertIsNone(self.choose(critical=True))
        self.assertIsNone(self.choose())

    def test_world_catalog_and_actor_mismatch_preserve_checkpoint_before_sync(self):
        self.prepare()
        for field, value in (("world_id", "other"), ("actor_unit_number", 59), ("catalog", "other")):
            with self.subTest(field=field):
                obs, fingerprint = deepcopy(self.obs), self.factory._fingerprint
                if field == "catalog":
                    self.factory._fingerprint = value
                else:
                    obs[field] = value
                before = self.factory.path.read_bytes()
                self.assertEqual(resume_input_bypass(self.factory, obs, critical_only=True)["status"], "blocked")
                with self.assertRaises(ValueError):
                    self.factory._sync(obs)
                self.assertEqual(self.factory.path.read_bytes(), before)
                self.factory._fingerprint = fingerprint

    def test_critical_sync_cannot_reenter_ordinary_factory_planning(self):
        self.prepare()
        with self.assertRaises(ValueError):
            self.factory._sync(self.obs)
        self.assertIn("critical", input_bypass_sync_error(self.factory, self.obs))

    def test_rollback_before_mine_restores_old_canonical_and_missing_pending_reservations(self):
        self.prepare()
        self.obs["tick"] = self.survey["tick"] = 100
        self.survey["new_entities"] = [{"present": False, "can_place": True} for _ in self.proposal["new_entities"]]
        self.reload()
        result = resume_input_bypass(self.factory, self.obs, critical_only=True)
        self.assertEqual(result["status"], "waiting", result)
        self.assertEqual(self.record["phase"], "preparing")
        self.assertEqual(self.record["new_units"], {})
        self.assertEqual(self.factory.state["links"]["input"], self.plan)
        self.assertIsNone(input_bypass_sync_error(self.factory, self.obs))

    def test_rollback_after_publication_to_old_route_keeps_retained_and_pending_assets(self):
        old_entry = deepcopy(self.survey["entry"])
        self.publish()
        self.obs["tick"] = self.survey["tick"] = 100
        self.survey["entry"] = old_entry
        self.survey["new_entities"] = [{"present": False, "can_place": True} for _ in self.proposal["new_entities"]]
        result = resume_input_bypass(self.factory, self.obs, critical_only=True)
        self.assertEqual(result["status"], "waiting", result)
        self.assertEqual(self.factory.state["links"]["input"], self.plan)
        self.assertIn(self.record["retained_key"], self.factory.state["blocks"])
        self.assertIn(self.record["pending_key"], self.factory.state["blocks"])

    def test_rollback_to_mined_entry_builds_directly_and_never_recursively_recreates_old_facing(self):
        self.publish()
        self.obs["tick"] = self.survey["tick"] = self.record["switching_tick"]
        self.survey["entry"] = {"present": False, "can_place": True}
        result = resume_input_bypass(self.factory, self.obs, critical_only=True)
        self.assertEqual(result["type"], "build", result)
        self.assertEqual(result["direction"], self.proposal["entry"]["new"]["direction"])
        self.assertEqual(self.record["phase"], "switching")
        self.reload()
        self.survey["entry"] = self.live(self.proposal["entry"]["new"])
        self.assertEqual(self.choose(critical=True)["status"], "waiting")
        self.assertEqual(self.record["phase"], "published")

    def test_public_start_rechecks_reserved_machine_footprint_and_port_approach(self):
        point = self.proposal["new_entities"][0]["position"]
        self.catalog.entities["assembling-machine-1"] = {"tile_width": 3, "tile_height": 3}
        self.factory.state["blocks"]["machine"] = {"entities": [{"name": "assembling-machine-1",
            "position": {"x": point["x"]+1, "y": point["y"]+1}}]}
        self.assertEqual(start_input_bypass(self.factory, "input", self.proposal, self.obs)["status"], "blocked")
        self.factory.state["blocks"].pop("machine")
        self.factory._port_clearances = lambda: {(point["x"], point["y"])}
        self.assertEqual(start_input_bypass(self.factory, "input", self.proposal, self.obs)["status"], "blocked")
        self.assertNotIn("input_bypasses", self.factory.state)

    def test_changed_or_missing_existing_piece_purity_health_facing_or_pair_blocks(self):
        self.prepare()
        baseline = deepcopy(self.survey)
        for field, value in (("unit_number", 9999), ("direction", 12), ("owned", False),
                             ("pure", False), ("healthy", False), ("present", False)):
            with self.subTest(field=field):
                self.survey = deepcopy(baseline)
                self.survey["new_entities"][0][field] = value
                self.assertEqual(self.choose(critical=True)["status"], "blocked")
        self.survey = deepcopy(baseline)
        self.survey["new_entities"][1]["underground_neighbour"]["unit_number"] = 9999
        self.assertEqual(self.choose(critical=True)["status"], "blocked")

    def test_stale_malformed_survey_foreign_topology_and_missing_replacement_fail_closed(self):
        self.prepare()
        baseline = deepcopy(self.survey)
        for field, value in (("ok", False), ("world_id", "foreign"), ("topology_clear", False),
                             ("new_entities", []), ("retained_segment", [])):
            with self.subTest(field=field):
                self.survey = deepcopy(baseline)
                self.survey[field] = value
                self.assertEqual(self.choose(critical=True)["status"], "blocked")
        self.survey = deepcopy(baseline)
        self.obs["tick"] = self.survey["tick"] + 2
        self.assertEqual(resume_input_bypass(self.factory, self.obs, critical_only=True)["status"], "blocked")
        self.survey["normal_inventory"]["transport-belt"] = 0
        self.assertEqual(self.choose(critical=True)["status"], "blocked")
        self.bootstrap.ensure_item.assert_not_called()

    def test_new_piece_without_a_persisted_build_intent_cannot_be_adopted(self):
        self.reserve()
        self.survey["new_entities"][0] = self.live(self.proposal["new_entities"][0])
        self.assertEqual(self.choose()["status"], "blocked")

    def test_other_owner_appearing_during_preparation_or_canonical_change_blocks(self):
        self.reserve()
        before = self.factory.path.read_bytes()
        self.factory.state["links"]["other"] = {"entities": [self.proposal["retained_segment"][0]]}
        self.assertEqual(self.choose()["status"], "blocked")
        self.factory.state["links"].pop("other")
        self.factory.state["links"]["input"]["source_port"]["item"] = "iron-plate"
        self.assertEqual(self.choose()["status"], "blocked")
        self.assertEqual(self.factory.path.read_bytes(), before)

    def test_changed_live_old_entry_or_retained_unit_never_rebinds(self):
        self.reserve()
        self.survey["entry"]["unit_number"] += 1000
        self.assertEqual(self.choose()["status"], "blocked")
        self.survey["entry"]["unit_number"] -= 1000
        self.survey["retained_segment"][0]["unit_number"] += 1000
        self.assertEqual(self.choose()["status"], "blocked")

    def test_unowned_preexisting_piece_or_mutated_proposal_cannot_start(self):
        self.survey["new_entities"][0] = self.live(self.proposal["new_entities"][0])
        self.assertEqual(start_input_bypass(self.factory, "input", self.proposal, self.obs)["status"], "blocked")
        self.survey["new_entities"][0] = {"present": False, "can_place": True}
        self.proposal["plan"]["source_port"]["item"] = "iron-plate"
        self.assertEqual(start_input_bypass(self.factory, "input", self.proposal, self.obs)["status"], "blocked")
        self.assertNotIn("input_bypasses", self.factory.state)

    def test_duplicate_units_in_an_otherwise_well_shaped_survey_cannot_start(self):
        self.survey["exit"]["unit_number"] = self.survey["entry"]["unit_number"]
        self.assertEqual(start_input_bypass(self.factory, "input", self.proposal, self.obs)["status"], "blocked")
        self.assertNotIn("input_bypasses", self.factory.state)

    def test_published_receipt_cannot_be_overwritten_by_a_later_same_link_segment(self):
        self.publish()
        before = self.factory.path.read_bytes()
        self.assertEqual(start_input_bypass(self.factory, "input", self.proposal, self.obs)["status"], "blocked")
        self.assertEqual(self.factory.path.read_bytes(), before)

    def test_character_backend_and_malformed_checkpoint_fail_closed(self):
        self.game.backend = "character"
        self.assertEqual(start_input_bypass(self.factory, "input", self.proposal, self.obs)["status"], "blocked")
        self.game.query.assert_not_called()
        self.game.backend = "assisted"
        self.reserve()
        self.game.backend = "character"
        self.assertEqual(resume_input_bypass(self.factory, self.obs, critical_only=True)["status"], "blocked")
        self.factory.state["input_bypasses"] = []
        self.assertEqual(resume_input_bypass(self.factory, self.obs, critical_only=True)["status"], "blocked")

    def test_no_transaction_does_not_query_or_change_factory_sync(self):
        before = self.factory.path.read_bytes()
        self.assertIsNone(resume_input_bypass(self.factory, self.obs, critical_only=True))
        self.assertIsNone(resume_input_bypass(self.factory, self.obs))
        self.factory._sync(self.obs)
        self.game.query.assert_not_called()
        self.assertEqual(self.factory.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
