from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory


class ConsumerSideFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), query=Mock())
        self.game.query.side_effect = lambda body: ({"ok": True, "input_belt_verified": True}
            if "input_belt_verified" in body else {"ok": True, "blocked": []})
        self.catalog = SimpleNamespace(fingerprint="one", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(self.game, Mock(), self.catalog)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
        self.factory = DeterministicFactory(self.game, Mock(), self.builder, self.catalog)
        self.source = {"x": .5, "y": 4.5}
        self.consumer = {"kind": "item", "item": "coal", "direction": "input",
                         "position": {"x": 6.5, "y": .5}, "facing": 12}
        self.belt = {"name": "transport-belt", "position": deepcopy(self.consumer["position"]),
                     "direction": 12, "unit_number": 7}
        self.lab = {"name": "lab", "position": {"x": 8.5, "y": .5}, "_width": 3, "_height": 3}
        self.obs = {"world_id": "one", "tick": 100, "entities": [deepcopy(self.belt)]}
        self.factory._sync(self.obs)
        self.factory.state["blocks"]["consumer"] = {"entities": [self.belt, self.lab], "ports": [self.consumer]}

    def route(self):
        return self.factory._consumer_material_route(self.obs, self.source, self.consumer,
            self.factory._reserved(), start_direction=4)

    def test_blocked_rear_uses_side_without_rotating_input_or_crossing_front(self):
        result = self.route()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["side_feed"])
        self.assertEqual(result["segments"][-1]["direction"], 12)
        self.assertEqual(self.belt["direction"], 12)
        before = result["segments"][-2]["position"]
        self.assertEqual(before["x"], 6.5)
        self.assertIn(before["y"], [-.5, 1.5])
        self.assertNotIn({"x": 5.5, "y": .5}, [e["position"] for e in result["segments"]])
        self.builder.can_place.assert_called_once()

    def test_all_entries_obstructed_does_not_claim_connection(self):
        self.factory.state["blocks"]["wall"] = {"entities": [
            {"name": "stone-wall", "position": {"x": 6.5, "y": y}} for y in [-.5, 1.5]]}
        self.assertFalse(self.route()["ok"])
        self.builder.can_place.assert_not_called()

    def test_nonrouting_failure_does_not_enable_drop_fallback(self):
        self.factory._consumer_drop_bridge_route = Mock()
        for reason in ("RCON disconnected", "route endpoint is occupied"):
            with self.subTest(reason=reason):
                failure = {"ok": False, "reason": reason}
                self.factory._material_route = Mock(return_value=failure)
                self.assertIs(self.route(), failure)
        self.factory._consumer_drop_bridge_route.assert_not_called()

    def test_verified_enclosed_input_explicitly_allows_upstream_crossing(self):
        result = {"ok": True, "segments": [deepcopy(self.belt)], "flow_verified": False}
        self.factory._consumer_drop_bridge_route = Mock(return_value=result)
        for reason in ("no route within bounds", "route search budget exhausted"):
            with self.subTest(reason=reason):
                self.factory._material_route = Mock(return_value={"ok": False, "reason": reason})
                self.factory._consumer_drop_bridge_route.reset_mock()
                self.assertIs(self.route(), result)
                self.factory._consumer_drop_bridge_route.assert_called_once_with(
                    self.obs, self.source, self.consumer, self.factory._reserved(),
                    start_direction=4, allow_upstream_bridge=True, allow_underground=True)

    def test_enclosed_input_tries_only_its_one_forward_owned_continuation(self):
        self.factory._material_route = Mock(return_value={"ok": False, "reason": "no route within bounds"})
        result = {"ok": True, "segments": []}
        self.factory._consumer_drop_bridge_route = Mock(side_effect=[
            {"ok": False, "reason": "no clear powered long-arm drop into owned input belt"}, result])
        canonical = deepcopy(self.consumer)
        self.assertIs(self.route(), result)
        self.assertEqual(self.consumer, canonical)
        calls = self.factory._consumer_drop_bridge_route.call_args_list
        self.assertEqual(len(calls), 2)
        entry = deepcopy(canonical)
        entry["position"] = {"x": canonical["position"]["x"] - 1, "y": canonical["position"]["y"]}
        self.assertEqual(calls[1].kwargs, {"start_direction": 4, "allow_upstream_bridge": True, "allow_underground": True,
            "consumer_entry": {"owner_key": "consumer", "entry_port": entry}})

    def test_live_drop_identity_or_material_failure_does_not_authorize_continuation(self):
        self.factory._material_route = Mock(return_value={"ok": False, "reason": "no route within bounds"})
        for reason in ("long-arm input belt identity changed", "long-arm input belt carries another material"):
            with self.subTest(reason=reason):
                failure = {"ok": False, "reason": reason}
                self.factory._consumer_drop_bridge_route = Mock(return_value=failure)
                self.assertIs(self.route(), failure)
                self.factory._consumer_drop_bridge_route.assert_called_once()

    def test_missing_or_reoriented_observed_belt_cannot_be_side_fed(self):
        self.factory._consumer_drop_bridge_route = Mock()
        for mode in ["missing", "reoriented", "no_identity"]:
            with self.subTest(mode=mode):
                self.obs["entities"] = [] if mode == "missing" else [deepcopy(self.belt)]
                if mode == "reoriented":
                    self.obs["entities"][0]["direction"] = 4
                if mode == "no_identity":
                    self.obs["entities"][0].pop("unit_number")
                self.assertFalse(self.route()["ok"])
        self.game.query.assert_not_called()
        self.factory._consumer_drop_bridge_route.assert_not_called()

    def test_changed_identity_and_foreign_material_fail_closed(self):
        self.factory._consumer_drop_bridge_route = Mock()
        for reason in ["input belt identity changed", "input belt carries another material"]:
            with self.subTest(reason=reason):
                self.game.query.side_effect = None
                self.game.query.return_value = {"ok": False, "reason": reason}
                self.assertEqual(self.route(), {"ok": False, "reason": reason})
        self.builder.can_place.assert_not_called()
        self.factory._consumer_drop_bridge_route.assert_not_called()

    def test_unowned_input_declaration_does_not_authorize_side_feed(self):
        self.factory._consumer_drop_bridge_route = Mock()
        self.factory.state["blocks"]["consumer"]["ports"] = []
        self.assertFalse(self.route()["ok"])
        self.game.query.assert_not_called()
        self.factory._consumer_drop_bridge_route.assert_not_called()

    def test_port_declaration_without_its_reserved_belt_cannot_adopt_a_live_belt(self):
        self.factory._consumer_drop_bridge_route = Mock()
        self.factory.state["blocks"]["consumer"]["entities"] = [self.lab]
        self.assertFalse(self.route()["ok"])
        self.game.query.assert_not_called()
        self.factory._consumer_drop_bridge_route.assert_not_called()

    def test_straight_input_preserves_fast_path(self):
        self.factory.state["blocks"]["consumer"]["entities"] = [self.belt]
        result = self.route()
        self.assertTrue(result["ok"], result)
        self.assertNotIn("side_feed", result)
        self.assertFalse(any("input_belt_verified" in c.args[0] for c in self.game.query.call_args_list))

    def test_connection_persists_paid_plan_with_original_input_facing(self):
        source = {"kind": "item", "item": "coal", "direction": "output",
                  "position": self.source, "facing": 4}
        result = self.factory.connect_input(self.obs, source, self.consumer, "coal-consumer")
        self.assertEqual(result["status"], "succeeded", result)
        plan = self.factory.state["links"]["coal-consumer"]
        self.assertEqual(plan["entities"][-1]["direction"], 12)
        self.builder.ensure_plan.assert_called_once_with(self.obs, plan)
        self.assertFalse(result["evidence"]["flow_verified"])

    def test_connection_persists_actual_entry_without_changing_canonical_consumer(self):
        source = {"kind": "item", "item": "coal", "direction": "output",
                  "position": self.source, "facing": 4}
        entry = {**deepcopy(self.consumer), "position": {"x": 5.5, "y": .5}}
        provenance = {"owner_key": "consumer", "entry_port": entry}
        canonical = deepcopy(self.consumer)
        self.factory._consumer_material_route = Mock(return_value={"ok": True,
            "segments": [{"name": "transport-belt", "position": self.source, "direction": 4},
                         {"name": "transport-belt", "position": entry["position"], "direction": 12}],
            "consumer_entry": provenance})
        with patch("factorio_ai.deterministic_input_links.ensure_input_dependencies", return_value={"status": "succeeded"}):
            result = self.factory.connect_input(self.obs, source, self.consumer, "coal-consumer")
        self.assertEqual(result["status"], "succeeded")
        restored = DeterministicFactory(self.game, Mock(), self.builder, self.catalog)
        saved = restored.state["links"]["coal-consumer"]
        self.assertEqual(saved["consumer_port"], canonical)
        self.assertEqual(saved["consumer_entry"], provenance)
        self.assertEqual(self.consumer, canonical)

    def capacity_sites(self):
        self.game.query.side_effect = None
        self.game.query.return_value = {"ok": True, "sites": [{"x": 0, "y": 0}, {"x": 20, "y": 0}]}

    def test_new_raw_site_rejects_obstructed_input_and_output_approaches(self):
        self.capacity_sites()
        for x, y in [(4.5, -2.5), (-4.5, -1.5)]:
            with self.subTest(approach=(x, y)):
                self.factory.state["blocks"] = {"obstacle": {"entities": [
                    {"name": "stone-wall", "position": {"x": x, "y": y}}]}}
                result = self.factory._raw_capacity_site(self.obs, "iron-plate", "new")
                self.assertTrue(result["ok"], result)
                drill = next(e for e in result["entities"] if e["name"] == "electric-mining-drill")
                self.assertEqual(drill["position"]["x"], 20.5)

    def test_new_raw_site_preserves_existing_port_clearance(self):
        self.capacity_sites()
        existing = {"kind": "item", "item": "coal", "direction": "output",
                    "position": {"x": -1.5, "y": .5}, "facing": 4}
        self.factory.state["blocks"] = {"existing": {"entities": [
            {"name": "transport-belt", "position": existing["position"], "direction": 4}], "ports": [existing]}}
        result = self.factory._raw_capacity_site(self.obs, "iron-plate", "new")
        drill = next(e for e in result["entities"] if e["name"] == "electric-mining-drill")
        self.assertEqual(drill["position"]["x"], 20.5)

    def test_existing_cell_is_preserved_for_connection_recovery(self):
        saved = self.factory._electric_source_plan("iron-plate", 0, 0)
        self.factory.state["blocks"]["existing-cell"] = saved
        self.assertIs(self.factory._raw_capacity_site(self.obs, "iron-plate", "existing-cell"), saved)
        self.game.query.assert_not_called()

    def test_new_raw_approach_cannot_share_another_material_clearance(self):
        self.capacity_sites()
        other = {"kind": "item", "item": "iron-plate", "direction": "output",
                 "position": {"x": 5.5, "y": -2.5}, "facing": 12}
        self.factory.state["blocks"] = {"other": {"entities": [
            {"name": "transport-belt", "position": other["position"], "direction": 12}], "ports": [other]}}
        result = self.factory._raw_capacity_site(self.obs, "iron-plate", "new")
        drill = next(e for e in result["entities"] if e["name"] == "electric-mining-drill")
        self.assertEqual(drill["position"]["x"], 20.5)


if __name__ == "__main__":
    unittest.main()
