from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory


class ConsumerDropBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), query=Mock())
        self.catalog = SimpleNamespace(fingerprint="one", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(self.game, Mock(), self.catalog)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.factory = DeterministicFactory(self.game, Mock(), self.builder, self.catalog)
        self.source = {"x": .5, "y": 6.5}
        self.consumer = {"kind": "item", "item": "coal", "direction": "input",
                         "position": {"x": 6.5, "y": .5}, "facing": 12}
        self.belt = {"name": "transport-belt", "position": self.consumer["position"], "direction": 12, "unit_number": 7}
        self.pole = {"name": "small-electric-pole", "position": {"x": 6.5, "y": 3.5}, "direction": 0, "unit_number": 8}
        self.obs = {"world_id": "one", "tick": 100, "entities": [deepcopy(self.belt), deepcopy(self.pole)]}
        self.factory._sync(self.obs)
        self.factory.state["blocks"]["consumer"] = {"ports": [self.consumer], "entities": [
            self.belt, self.pole, {"name": "lab", "position": {"x": 8.5, "y": 1.5}},
            {"name": "transport-belt", "position": {"x": 5.5, "y": .5}, "direction": 12},
            {"name": "small-electric-pole", "position": {"x": 6.5, "y": -.5}, "direction": 0},
            {"name": "small-electric-pole", "position": {"x": 5.5, "y": 2.5}, "direction": 0}]}
        self.option = {"arm": {"name": "long-handed-inserter", "position": {"x": 6.5, "y": 2.5}, "direction": 8},
            "pickup": {"x": 6.5, "y": 4.5}, "pickup_position": {"x": 6.5, "y": 4.5},
            "drop_position": {"x": 6.5, "y": .3}, "poles": [self.pole]}
        self.game.query.side_effect = lambda body: ({"ok": True, "candidates": [deepcopy(self.option)], "new_pole_reach": 2.5}
            if "owned_consumer_long_arm_drop" in body else {"ok": True, "blocked": []})

    def route(self, reserved=None, *, owned_plan_key=None, allow_upstream_bridge=False):
        return self.factory._consumer_drop_bridge_route(self.obs, self.source, self.consumer,
            self.factory._reserved() if reserved is None else reserved, start_direction=4,
            owned_plan_key=owned_plan_key, allow_upstream_bridge=allow_upstream_bridge)

    def test_ordinary_drop_does_not_enable_upstream_crossing_search(self):
        self.factory._material_route = Mock(wraps=self.factory._material_route)
        self.assertTrue(self.route()["ok"])
        self.assertTrue(all(not call.kwargs["allow_bridge"]
                            for call in self.factory._material_route.call_args_list))

    def test_explicit_upstream_crossing_is_bounded_across_drop_options_and_poles(self):
        self.option["poles"] = [self.pole] * 3
        self.game.query.side_effect = None
        self.game.query.return_value = {"ok": True, "candidates": [self.option] * 4}
        self.factory._material_route = Mock(return_value={"ok": False, "reason": "no route within bounds"})
        self.assertFalse(self.route(allow_upstream_bridge=True)["ok"])
        calls = self.factory._material_route.call_args_list
        self.assertEqual(len(calls), 12)
        self.assertEqual(sum(call.kwargs["allow_bridge"] for call in calls), 1)

    def test_upstream_crossing_equipment_is_preserved_and_combined_placement_required(self):
        upstream = [
            {"name": "transport-belt", "position": self.source, "direction": 4},
            {"name": "long-handed-inserter", "position": {"x": 2.5, "y": 6.5}, "direction": 12},
            {"name": "small-electric-pole", "position": {"x": 2.5, "y": 8.5}, "direction": 0},
            {"name": "transport-belt", "position": self.option["pickup"], "direction": 0}]
        self.factory._material_route = Mock(return_value={"ok": True, "segments": upstream})
        result = self.route(allow_upstream_bridge=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][:len(upstream)], upstream)
        self.assertEqual(sum(e["name"] == "long-handed-inserter" for e in result["segments"]), 2)
        self.assertEqual(sum(e["name"] == "small-electric-pole" for e in result["segments"]), 2)
        self.builder.can_place.assert_called_with(result["segments"])
        self.builder.can_place.side_effect = [{"ok": True}, {"ok": False}]
        self.assertFalse(self.route(allow_upstream_bridge=True)["ok"])

    def test_enclosed_input_accepts_live_offset_drop_over_pole_without_rotating_belt(self):
        result = self.route()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][-1], {"name": "transport-belt", "position": self.consumer["position"], "direction": 12})
        self.assertEqual(result["consumer_drop"]["drop_position"], {"x": 6.5, "y": .3})
        self.assertEqual(result["consumer_drop"]["pole_unit_number"], 8)
        arm = next(e for e in result["segments"] if e["name"] == "long-handed-inserter")
        self.assertEqual(arm, self.option["arm"])
        self.assertFalse(any(e["name"] == "transport-belt" and e["position"] == self.pole["position"] for e in result["segments"]))
        self.assertFalse(result["flow_verified"])
        self.builder.can_place.assert_called_with(result["segments"])

    def test_unowned_missing_changed_or_other_world_input_is_rejected_before_query(self):
        original = deepcopy(self.obs)
        for mode in ("ownership", "missing", "unit", "facing", "world"):
            with self.subTest(mode=mode):
                self.obs = deepcopy(original)
                self.factory.state["blocks"]["consumer"]["ports"] = [] if mode == "ownership" else [self.consumer]
                if mode == "missing": self.obs["entities"] = []
                if mode == "unit": self.obs["entities"][0].pop("unit_number")
                if mode == "facing": self.obs["entities"][0]["direction"] = 4
                if mode == "world": self.obs["world_id"] = "another-world"
                self.assertFalse(self.route()["ok"])
        self.game.query.assert_not_called()

    def test_fresh_identity_material_and_recipe_failures_remain_explicit(self):
        self.game.query.side_effect = None
        for reason in ("long-arm input belt identity changed", "long-arm input belt carries another material", "long inserter recipe is locked"):
            with self.subTest(reason=reason):
                self.game.query.return_value = {"ok": False, "reason": reason}
                self.assertEqual(self.route(), {"ok": False, "reason": reason})
        self.builder.can_place.assert_not_called()

    def test_no_live_prototype_drop_candidate_cannot_claim_connection(self):
        self.game.query.side_effect = None
        self.game.query.return_value = {"ok": True, "candidates": []}
        self.assertFalse(self.route()["ok"])
        self.builder.can_place.assert_not_called()

    def test_complete_route_placement_is_rechecked_after_equipment_fits(self):
        self.builder.can_place.side_effect = [{"ok": True}, {"ok": False, "reason": "route changed"}]
        self.assertFalse(self.route()["ok"])
        self.assertEqual(self.builder.can_place.call_count, 2)

    def test_missing_live_power_adds_a_paid_covering_pole_dependency(self):
        self.option["poles"] = []
        result = self.route()
        self.assertTrue(result["ok"], result)
        pole = next(e for e in result["segments"] if e["name"] == "small-electric-pole")
        self.assertNotEqual(pole["position"], self.pole["position"])
        self.assertLessEqual(max(abs(pole["position"][axis] - self.option["arm"]["position"][axis]) for axis in ("x", "y")), 2.5)
        self.assertIsNone(result["consumer_drop"]["pole_unit_number"])
        self.assertFalse(result["flow_verified"])

    def test_foreign_material_reservation_cannot_share_exact_input_geometry(self):
        self.factory.state["links"]["foreign"] = {"entities": [deepcopy(self.belt)],
            "source_port": {"item": "iron-plate"}, "consumer_port": {"item": "iron-plate"}}
        self.assertFalse(self.route()["ok"])
        self.builder.can_place.assert_not_called()

    def test_coincident_planned_pickup_keeps_the_source_facing(self):
        self.source = deepcopy(self.option["pickup"])
        source_belt = {"name": "transport-belt", "position": self.source, "direction": 4}
        result = self.route(self.factory._reserved() + [source_belt])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][0], source_belt)
        self.assertEqual(result["segments"][-1]["direction"], 12)

    def test_primary_source_front_is_available_but_other_port_clearances_are_reserved(self):
        self.factory.state["blocks"]["source"] = {"ports": [
            {"kind": "item", "item": "coal", "direction": "output", "position": self.source, "facing": 4},
            {"kind": "item", "item": "iron-plate", "direction": "output", "position": {"x": 3.5, "y": 6.5}, "facing": 4}],
            "entities": [{"name": "transport-belt", "position": self.source, "direction": 4}]}
        result = self.route()
        self.assertTrue(result["ok"], result)
        positions = [e["position"] for e in result["segments"] if e["name"] == "transport-belt"]
        self.assertEqual(positions[1], {"x": 1.5, "y": 6.5})
        self.assertNotIn({"x": 4.5, "y": 6.5}, positions)
        blocked = self.factory._reserved() + [{"name": "small-electric-pole", "position": {"x": 1.5, "y": 6.5}, "direction": 0}]
        self.assertFalse(self.route(blocked)["ok"])

    def test_dedicated_saved_transit_belt_accepts_explicit_owner_only(self):
        owner = self.factory.state["blocks"]["consumer"]
        owner["ports"] = [{**self.consumer, "direction": "output", "position": {"x": 20.5, "y": .5}}]
        self.assertFalse(self.route()["ok"])
        result = self.route(owned_plan_key="consumer")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][-1]["position"], self.consumer["position"])
        self.assertEqual(result["segments"][-1]["direction"], self.consumer["facing"])

    def test_transit_owner_must_exist_and_have_only_the_requested_item_ports(self):
        owner = self.factory.state["blocks"]["consumer"]
        for ports in ([], [{**self.consumer, "item": "iron-plate"}],
                      [self.consumer, {**self.consumer, "item": "iron-plate"}],
                      [{"kind": "power", "item": "electricity"}]):
            with self.subTest(ports=ports):
                owner["ports"] = ports
                self.assertFalse(self.route(owned_plan_key="consumer")["ok"])
        owner["ports"] = [self.consumer]
        self.assertFalse(self.route(owned_plan_key="missing")["ok"])
        self.game.query.assert_not_called()

    def test_transit_owner_requires_exact_unambiguous_reserved_belt_facing(self):
        owner = self.factory.state["blocks"]["consumer"]
        original = deepcopy(owner["entities"])
        wrong = {**self.belt, "direction": 4}
        for entities in ([e for e in original if e["position"] != self.consumer["position"]],
                         [wrong], original + [wrong]):
            owner["entities"] = entities
            self.assertFalse(self.route(owned_plan_key="consumer")["ok"])
        self.game.query.assert_not_called()

    def test_transit_owner_retains_live_identity_and_material_guards(self):
        self.game.query.side_effect = None
        for reason in ("long-arm input belt identity changed", "long-arm input belt carries another material"):
            self.game.query.return_value = {"ok": False, "reason": reason}
            self.assertEqual(self.route(owned_plan_key="consumer"), {"ok": False, "reason": reason})
        self.builder.can_place.assert_not_called()

    def test_transit_owner_does_not_override_foreign_material_reservations(self):
        self.factory.state["links"]["foreign"] = {"entities": [deepcopy(self.belt)],
            "source_port": {"item": "iron-plate"}, "consumer_port": {"item": "iron-plate"}}
        self.assertFalse(self.route(owned_plan_key="consumer")["ok"])
        self.builder.can_place.assert_not_called()


if __name__ == "__main__":
    unittest.main()
