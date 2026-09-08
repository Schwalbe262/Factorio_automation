from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_consumer_entry import validate_consumer_entry
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_input_links import ensure_input_dependencies
from factorio_ai.factory_templates import build_template


def belt(x, y, facing=4):
    return {"name": "transport-belt", "position": {"x": x, "y": y}, "direction": facing}


class ConsumerEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temporary.name)),
                                    query=Mock(return_value={"ok": True, "consumer_entry_verified": True}))
        catalog = SimpleNamespace(fingerprint="one", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(self.game, Mock(), catalog)
        self.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
        self.factory = DeterministicFactory(self.game, Mock(), self.builder, catalog)
        self.factory.ensure_power_connection = Mock(return_value={"status": "succeeded"})
        self.owner = build_template("labs_row", anchor={"x": .5, "y": .5},
                                    inputs=["automation-science-pack", "logistic-science-pack"])
        self.canonical = deepcopy(self.owner["ports"][0])
        self.entry = {**self.canonical, "position": {"x": -.5, "y": -2.5}}
        self.provenance = {"owner_key": "labs", "entry_port": self.entry}
        self.obs = {"world_id": "one", "tick": 1, "entities": [
            {**deepcopy(row), "unit_number": index + 1} for index, row in enumerate(self.owner["entities"])]}
        self.factory._sync(self.obs)
        self.factory.state["blocks"]["labs"] = self.owner
        self.source = {"kind": "item", "item": "automation-science-pack", "direction": "output",
                       "position": {"x": -5.5, "y": -2.5}, "facing": 4}
        rows = [belt(x + .5, -2.5) for x in range(-6, 0)]
        rows[-1]["direction"] = 8
        self.plan = {"ok": True, "entities": rows, "source_port": self.source,
                     "consumer_port": self.canonical, "consumer_entry": self.provenance}
        self.factory.state["links"]["science"] = self.plan

    def validate(self):
        return validate_consumer_entry(self.factory, self.obs, self.canonical, self.provenance)

    def dependencies(self):
        return ensure_input_dependencies(self.factory, self.obs, self.source, "science")

    def test_only_declared_one_forward_belt_is_admitted_inside_multimaterial_block(self):
        before = deepcopy((self.owner, self.provenance, self.canonical))
        result = self.validate()
        self.assertEqual(result, {"ok": True, "entry_port": self.entry,
                                  "canonical_approach": {"x": -.5, "y": -4.5}})
        self.assertEqual((self.owner, self.provenance, self.canonical), before)
        self.game.query.assert_called_once()
        query = self.game.query.call_args.args[0]
        self.assertIn("owned consumer continuation proof", query)
        self.assertIn("consumer entry intake geometry changed", query)
        self.assertIn("consumer entry carries another material", query)

    def test_malformed_owner_or_changed_entry_contract_fails_before_query(self):
        original = deepcopy(self.provenance)
        variants = [None, {}, {**original, "owner_key": "missing"}, {**original, "extra": True}]
        for changes in ({"position": {"x": -.5, "y": -1.5}}, {"facing": 4},
                        {"item": "logistic-science-pack"}, {"direction": "output"}, {"machine_index": 1}):
            variants.append({**original, "entry_port": {**self.entry, **changes}})
        for value in variants:
            with self.subTest(value=value):
                self.provenance = value
                self.assertFalse(self.validate()["ok"])
        self.game.query.assert_not_called()

    def test_missing_or_reoriented_owned_belts_cannot_authorize_continuation(self):
        original = deepcopy(self.owner["entities"])
        for position in (self.canonical["position"], self.entry["position"]):
            for missing in (True, False):
                with self.subTest(position=position, missing=missing):
                    self.owner["entities"] = deepcopy(original)
                    if missing:
                        self.owner["entities"] = [row for row in self.owner["entities"] if row["position"] != position]
                    else:
                        next(row for row in self.owner["entities"] if row["position"] == position)["direction"] = 4
                    self.assertFalse(self.validate()["ok"])
        self.game.query.assert_not_called()

    def test_indexed_machine_and_normal_intake_are_required(self):
        original = deepcopy(self.owner)
        for mode in ("missing_arm", "wrong_arm_facing", "wrong_machine", "unsupported_template"):
            with self.subTest(mode=mode):
                self.owner.clear()
                self.owner.update(deepcopy(original))
                if mode == "missing_arm":
                    self.owner["entities"] = [row for row in self.owner["entities"]
                                              if row["position"] != {"x": -.5, "y": -1.5}]
                elif mode == "wrong_arm_facing":
                    next(row for row in self.owner["entities"]
                         if row["position"] == {"x": -.5, "y": -1.5})["direction"] = 4
                elif mode == "wrong_machine":
                    self.owner["entities"][0]["position"] = {"x": 20.5, "y": .5}
                else:
                    self.owner["template"] = "unrelated_plan"
                self.assertFalse(self.validate()["ok"])
        self.game.query.assert_not_called()

    def test_another_port_cannot_share_the_canonical_clearance(self):
        self.factory.state["blocks"]["other"] = {"ports": [{"kind": "item", "item": "coal",
            "direction": "output", "facing": 4, "position": {"x": -1.5, "y": -4.5}}]}
        self.assertFalse(self.validate()["ok"])
        self.game.query.assert_not_called()

    def test_another_material_input_cannot_reach_the_entry(self):
        next(row for row in self.owner["entities"]
             if row["position"] == {"x": .5, "y": -2.5})["direction"] = 12
        self.assertFalse(self.validate()["ok"])
        self.game.query.assert_not_called()

    def test_foreign_material_link_cannot_reserve_or_feed_the_entry(self):
        for foreign_belt in (belt(-.5, -2.5, 8), belt(-1.5, -2.5, 4)):
            with self.subTest(foreign_belt=foreign_belt):
                self.factory.state["links"]["foreign"] = {"source_port": {"item": "coal"},
                    "consumer_port": {"item": "coal"}, "entities": [foreign_belt]}
                self.assertFalse(self.validate()["ok"])
        self.game.query.assert_not_called()

    def test_all_four_observed_entities_require_unchanged_identity_and_world(self):
        original = deepcopy(self.obs)
        for position in (self.canonical["position"], self.entry["position"],
                         {"x": -.5, "y": -1.5}, {"x": .5, "y": .5}):
            with self.subTest(position=position):
                self.obs = deepcopy(original)
                next(row for row in self.obs["entities"] if row["position"] == position).pop("unit_number")
                self.assertFalse(self.validate()["ok"])
        self.obs = deepcopy(original)
        self.obs["world_id"] = "other"
        self.assertFalse(self.validate()["ok"])
        self.game.query.assert_not_called()

    def test_fresh_identity_material_and_geometry_failures_remain_explicit(self):
        for reason in ("consumer entry identity changed", "consumer entry carries another material",
                       "consumer entry intake geometry changed", "consumer entry receiver recipe changed"):
            with self.subTest(reason=reason):
                self.game.query.return_value = {"ok": False, "reason": reason}
                self.assertEqual(self.validate(), {"ok": False, "reason": reason})
        self.game.query.return_value = {"ok": True}
        self.assertFalse(self.validate()["ok"])

    def test_direct_dependency_resolves_actual_entry_and_revalidates_after_reload(self):
        self.assertEqual(self.dependencies()["status"], "succeeded")
        self.game.query.assert_called_once()
        self.factory.state = json.loads(json.dumps(self.factory.state))
        self.game.query.reset_mock()
        self.assertEqual(self.dependencies()["status"], "succeeded")
        self.game.query.assert_called_once()
        self.assertEqual(self.factory.state["links"]["science"]["consumer_port"], self.canonical)
        self.builder.ensure_plan.assert_not_called()

    def test_direct_fast_path_cannot_skip_entry_validation_or_connected_path(self):
        self.plan["consumer_entry"]["owner_key"] = "missing"
        self.assertEqual(self.dependencies()["status"], "blocked")
        self.game.query.assert_not_called()
        self.plan["consumer_entry"]["owner_key"] = "labs"
        self.plan["entities"] = self.plan["entities"][:-1]
        self.assertEqual(self.dependencies()["status"], "blocked")
        self.builder.ensure_plan.assert_not_called()

    def test_branched_dependency_targets_entry_instead_of_unreachable_canonical_belt(self):
        self.source["position"] = {"x": -5.5, "y": -4.5}
        parent_belt = belt(-5.5, -4.5)
        parent_consumer = {**self.source, "direction": "input", "position": {"x": -4.5, "y": -4.5}}
        self.factory.state["links"]["parent"] = {"ok": True, "source_port": self.source,
            "consumer_port": parent_consumer, "entities": [parent_belt, belt(-4.5, -4.5)]}
        self.plan["upstream_tap"] = {"link_key": "parent", "belt": parent_belt}
        self.plan["entities"][:0] = [
            {"name": "inserter", "position": {"x": -5.5, "y": -3.5}, "direction": 0},
            {"name": "small-electric-pole", "position": {"x": -7.5, "y": -3.5}, "direction": 0}]
        result = self.dependencies()
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["evidence"]["upstream_links"], ["parent"])
        self.assertEqual(self.builder.ensure_plan.call_args.args[1]["entities"], [parent_belt])
        self.game.query.assert_called_once()


if __name__ == "__main__":
    unittest.main()
