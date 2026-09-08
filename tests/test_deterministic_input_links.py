from copy import deepcopy
import json
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_input_links import ensure_input_dependencies
from factorio_ai.deterministic_factory import DeterministicFactory


def entity(name, x, y, direction=0):
    return {"name": name, "position": {"x": x, "y": y}, "direction": direction}


def port(x=.5, y=.5, item="coal", direction="output", facing=4):
    return {"kind": "item", "item": item, "direction": direction,
            "position": {"x": x, "y": y}, "facing": facing}


def ready():
    return {"status": "succeeded", "evidence": {}}


def line(start, end, y=.5, direction=4):
    return [entity("transport-belt", x + .5, y, direction) for x in range(start, end + 1)]


def route(entities, source=None):
    last = next(row for row in reversed(entities) if row["name"] == "transport-belt")
    return {"ok": True, "entities": entities, "source_port": source or port(),
            "consumer_port": port(**last["position"], direction="input", facing=last["direction"]), "ports": []}


def tap(x=5, y=.5, end=9):
    return route([entity("inserter", x + .5, y + 1, 0),
                  entity("small-electric-pole", x - .5, y + 1), *line(x, end, y + 2)])


class InputDependencyTests(unittest.TestCase):
    def setUp(self):
        self.factory = SimpleNamespace(state={"links": {}, "blocks": {}}, _save=Mock(),
                                       builder=SimpleNamespace(ensure_plan=Mock(return_value=ready())),
                                       ensure_power_connection=Mock(return_value=ready()))
        self.obs = {"tick": 1}
        self.links = self.factory.state["links"]
        self.links["old"] = route(line(0, 10))
        self.links["new"] = tap()

    def ensure(self, key="new"):
        return ensure_input_dependencies(self.factory, self.obs, port(), key)

    def provenance(self, child="new", parent="old", index=5):
        self.links[child]["upstream_tap"] = {"link_key": parent,
                                           "belt": deepcopy(self.links[parent]["entities"][index])}

    def assert_blocked_without_actions(self, result):
        self.assertEqual(result["status"], "blocked", result)
        self.factory.builder.ensure_plan.assert_not_called()
        self.factory.ensure_power_connection.assert_not_called()
        self.factory._save.assert_not_called()

    def test_direct_primary_link_needs_no_extra_construction(self):
        self.assertEqual(self.ensure("old")["status"], "succeeded")
        self.factory.builder.ensure_plan.assert_not_called()
        self.factory._save.assert_not_called()

    def test_missing_tap_drop_is_not_misclassified_as_direct_primary(self):
        self.links["new"]["entities"] = self.links["new"]["entities"][:2] + self.links["new"]["entities"][3:]
        self.assert_blocked_without_actions(self.ensure())

    def test_canonical_source_facing_is_preserved(self):
        self.links["old"]["entities"][0]["direction"] = 0
        self.assert_blocked_without_actions(self.ensure())

    def test_legacy_tap_recovers_unique_owner_and_only_builds_prefix(self):
        before = deepcopy(self.links["old"])
        result = self.ensure()
        self.assertEqual(result["status"], "succeeded", result)
        self.assertFalse(result["evidence"]["flow_verified"])
        self.assertEqual(self.links["new"]["upstream_tap"]["link_key"], "old")
        prefix = self.factory.builder.ensure_plan.call_args.args[1]
        self.assertEqual(prefix["entities"], line(0, 5))
        self.assertEqual(self.links["old"], before)
        self.factory._save.assert_called_once()

    def test_saved_provenance_survives_restart_and_reobserves_missing_prefix(self):
        self.assertEqual(self.ensure()["status"], "succeeded")
        self.factory.state = json.loads(json.dumps(self.factory.state))
        self.factory._save.reset_mock()
        action = {"type": "build", "name": "transport-belt", "position": {"x": 3.5, "y": .5}}
        self.factory.builder.ensure_plan.return_value = action
        self.assertEqual(self.ensure(), action)
        self.factory._save.assert_not_called()

    def test_partial_ancestor_returns_normal_builder_action_before_downstream(self):
        self.provenance()
        self.factory.builder.ensure_plan.side_effect = lambda obs, plan: {
            "type": "build", **next(row for row in plan["entities"] if row["position"]["x"] >= 3.5)}
        result = self.ensure()
        self.assertEqual(result["position"], {"x": 3.5, "y": .5})
        self.assertEqual(self.factory.builder.ensure_plan.call_count, 1)
        self.factory.ensure_power_connection.assert_not_called()

    def test_nested_taps_build_oldest_first_and_power_only_used_ancestor(self):
        self.links["last"] = tap(x=7, y=2.5, end=10)
        result = self.ensure("last")
        self.assertEqual(result["status"], "succeeded", result)
        calls = self.factory.builder.ensure_plan.call_args_list
        self.assertEqual(calls[0].args[1]["entities"], line(0, 5))
        used = calls[1].args[1]["entities"]
        self.assertEqual([row for row in used if row["name"] == "transport-belt"], line(5, 7, 2.5))
        self.assertTrue(any(row["name"] == "inserter" for row in used))
        self.assertTrue(any(row["name"] == "small-electric-pole" for row in used))
        self.factory.ensure_power_connection.assert_called_once()
        self.assertEqual(self.factory.ensure_power_connection.call_args.args[1], "tap:new")
        self.assertEqual(result["evidence"]["upstream_links"], ["old", "new"])

    def test_upstream_power_wait_blocks_ready_result(self):
        self.links["last"] = tap(x=7, y=2.5, end=10)
        waiting = {"status": "waiting", "reason": "awaiting generator"}
        self.factory.ensure_power_connection.return_value = waiting
        self.assertEqual(self.ensure("last"), waiting)

    def test_ambiguous_legacy_owner_fails_closed(self):
        self.links["duplicate"] = deepcopy(self.links["old"])
        self.assert_blocked_without_actions(self.ensure())

    def test_explicit_owner_resolves_shared_geometry(self):
        self.links["duplicate"] = deepcopy(self.links["old"])
        self.provenance()
        self.assertEqual(self.ensure()["status"], "succeeded")

    def test_foreign_source_or_material_cannot_be_inferred(self):
        for field, value in (("item", "iron-plate"), ("kind", "fluid"),
                             ("position", {"x": 20.5, "y": .5}), ("facing", 12)):
            with self.subTest(field=field):
                self.links["old"]["source_port"] = {**port(), field: value}
                self.assert_blocked_without_actions(self.ensure())

    def test_wrong_consumer_item_invalidates_parent_even_with_same_source(self):
        self.links["old"]["consumer_port"]["item"] = "copper-plate"
        self.assert_blocked_without_actions(self.ensure())

    def test_saved_pickup_direction_change_is_rejected(self):
        self.provenance()
        self.links["old"]["entities"][5]["direction"] = 0
        self.assert_blocked_without_actions(self.ensure())

    def test_saved_pickup_geometry_change_is_rejected(self):
        self.provenance()
        self.links["new"]["entities"][0]["direction"] = 8
        self.assert_blocked_without_actions(self.ensure())

    def test_gap_or_head_on_belt_before_tap_is_rejected(self):
        original = deepcopy(self.links["old"])
        for broken in ("gap", "head-on"):
            with self.subTest(broken=broken):
                self.links["old"] = deepcopy(original)
                if broken == "gap":
                    self.links["old"]["entities"].pop(3)
                else:
                    self.links["old"]["entities"][3]["direction"] = 12
                self.assert_blocked_without_actions(self.ensure())

    def test_broken_old_suffix_after_tap_is_irrelevant(self):
        self.links["old"]["entities"][8]["direction"] = 12
        self.assertEqual(self.ensure()["status"], "succeeded")

    def test_crossing_inserter_and_its_pole_are_in_upstream_prefix(self):
        self.links["old"] = route([*line(0, 2), entity("long-handed-inserter", 4.5, .5, 12),
                                   entity("small-electric-pole", 4.5, 2.5), *line(6, 10)])
        self.links["new"] = tap(x=8, end=12)
        result = self.ensure()
        self.assertEqual(result["status"], "succeeded", result)
        prefix = self.factory.builder.ensure_plan.call_args.args[1]["entities"]
        self.assertEqual([row for row in prefix if row["name"] == "transport-belt"], line(0, 2) + line(6, 8))
        self.assertTrue(any(row["name"] == "long-handed-inserter" for row in prefix))
        self.factory.ensure_power_connection.assert_called_once()

    def test_missing_upstream_covering_pole_is_rejected_before_construction(self):
        self.links["last"] = tap(x=7, y=2.5, end=10)
        self.links["new"]["entities"] = [row for row in self.links["new"]["entities"]
                                           if row["name"] != "small-electric-pole"]
        self.assert_blocked_without_actions(self.ensure("last"))

    def test_distant_upstream_poles_progress_through_distinct_cached_power_routes(self):
        self.links["new"] = route([entity("inserter", 5.5, 1.5),
            entity("small-electric-pole", 4.5, 1.5), *line(5, 15, 2.5),
            entity("long-handed-inserter", 17.5, 2.5, 12),
            entity("small-electric-pole", 17.5, 4.5), *line(19, 23, 2.5)])
        self.links["last"] = tap(x=21, y=2.5, end=25)
        self.factory.state["power_links"] = {}
        self.factory._sync = Mock()
        connected, pending = set(), {}

        def query(body):
            encoded = body.split("local wanted=helpers.json_to_table(", 1)[1].split(");local networks", 1)[0]
            positions = json.loads(json.loads(encoded))
            return {"ok": True, "connected": sum((p["x"], p["y"]) in connected for p in positions),
                    "live": [{"x": -20.5, "y": .5}]}

        def power_route(source, target):
            relay = {"x": target["x"], "y": target["y"] + 3}
            pending[relay["x"], relay["y"]] = target["x"], target["y"]
            return {"ok": True, "path": [relay, target]}

        def build(obs, plan):
            if all(row["name"] == "small-electric-pole" for row in plan["entities"]):
                target = plan["entities"][-1]["position"]
                if (target["x"], target["y"]) not in connected:
                    return {"type": "build", **plan["entities"][0]}
            return ready()

        self.factory.game = SimpleNamespace(query=query)
        self.factory._power_route = power_route
        self.factory.builder.can_place = Mock(return_value={"ok": True})
        self.factory.builder.ensure_plan.side_effect = build
        self.factory._fingerprint = "catalog"
        self.factory.catalog = SimpleNamespace()
        self.factory._power_observation = None
        self.factory._power_context = None
        self.factory._power_grids = {}
        self.factory._power_grid = MethodType(DeterministicFactory._power_grid, self.factory)
        self.factory.ensure_power_connection = MethodType(DeterministicFactory.ensure_power_connection, self.factory)
        for _ in range(2):
            result = self.ensure("last")
            self.assertEqual(result.get("type"), "build", result)
            position = result["position"]
            connected.add(pending[position["x"], position["y"]])
        self.assertEqual(connected, {(4.5, 1.5), (17.5, 4.5)})
        self.assertEqual(len(self.factory.state["power_links"]), 2)
        self.assertEqual(self.ensure("last")["status"], "succeeded")

    def test_cycle_is_rejected_before_any_build_or_metadata_save(self):
        self.links["old"] = route([entity("inserter", 7.5, 1.5, 8),
                                   entity("small-electric-pole", 8.5, 1.5), *line(5, 7, direction=12)])
        self.links["new"]["upstream_tap"] = {"link_key": "old", "belt": deepcopy(self.links["old"]["entities"][2])}
        self.links["old"]["upstream_tap"] = {"link_key": "new", "belt": deepcopy(self.links["new"]["entities"][4])}
        result = self.ensure()
        self.assert_blocked_without_actions(result)
        self.assertIn("cycle", result["evidence"]["dependency_error"])

    def test_dependency_depth_is_bounded(self):
        previous = "old"
        for index in range(33):
            key = f"tap:{index}"
            self.links[key] = tap(x=5, y=.5 + index * 2, end=9)
            self.links[key]["upstream_tap"] = {"link_key": previous,
                "belt": deepcopy(next(row for row in self.links[previous]["entities"]
                                      if row["name"] == "transport-belt" and row["position"]["x"] == 5.5))}
            previous = key
        result = self.ensure(previous)
        self.assert_blocked_without_actions(result)
        self.assertIn("depth", result["evidence"]["dependency_error"])

    def direct_intake(self):
        belt = deepcopy(self.links["old"]["entities"][5])
        arm = entity("inserter", 5.5, 1.5, 0)
        receiver = entity("stone-furnace", 6, 3)
        direct = route([belt])
        direct["upstream_tap"] = {"link_key": "old", "belt": deepcopy(belt), "intake": {
            "block_key": "fuel:furnace", "inserter": deepcopy(arm), "receiver": receiver}}
        self.factory.state["blocks"]["fuel:furnace"] = {
            "entities": [arm, entity("small-electric-pole", 4.5, 1.5)], "ports": [direct["consumer_port"]]}
        self.links["direct"] = direct
        return direct

    def test_direct_intake_uses_explicit_reserved_geometry_and_only_parent_prefix(self):
        self.direct_intake()
        result = self.ensure("direct")
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(self.factory.builder.ensure_plan.call_args.args[1]["entities"], line(0, 5))
        self.factory.ensure_power_connection.assert_not_called()

    def test_direct_intake_rejects_unreserved_arm_wrong_receiver_and_wrong_facing(self):
        for broken in ("arm", "receiver", "facing"):
            with self.subTest(broken=broken):
                direct = self.direct_intake()
                if broken == "arm":
                    direct["upstream_tap"]["intake"]["inserter"]["position"]["x"] += 1
                elif broken == "receiver":
                    direct["upstream_tap"]["intake"]["receiver"]["position"]["x"] += 10
                else:
                    direct["consumer_port"]["facing"] = 0
                self.assert_blocked_without_actions(self.ensure("direct"))


if __name__ == "__main__":
    unittest.main()
