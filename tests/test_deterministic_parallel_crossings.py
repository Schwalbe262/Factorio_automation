from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory


def entity(name, x, y, direction=0):
    return {"name": name, "position": {"x": x, "y": y}, "direction": direction}


def leg(start, end, facing=0):
    points = [start] if start == end else [start, end]
    return {"ok": True, "path": deepcopy(points),
            "segments": [{"position": deepcopy(p), "direction": facing} for p in points]}


class ParallelCrossingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temporary.name)),
                                    query=Mock(return_value={"ok": True, "blocked": [], "belts": []}))
        self.catalog = SimpleNamespace(fingerprint="one", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(self.game, Mock(), self.catalog)
        self.factory = DeterministicFactory(self.game, Mock(), self.builder, self.catalog)
        self.factory._sync({"world_id": "one", "tick": 100})
        self.source, self.destination = {"x": .5, "y": 15.5}, {"x": .5, "y": .5}
        self.trunks = [entity("transport-belt", x + .5, y, 4)
                       for y in (10.5, 5.5) for x in range(-61, 62)]
        self.first_position, self.first_drop = {"x": .5, "y": 10.5}, {"x": .5, "y": 9.5}
        self.first_trial = [entity("long-handed-inserter", .5, 11.5, 8),
                            entity("transport-belt", .5, 13.5), entity("transport-belt", .5, 9.5),
                            entity("small-electric-pole", -1.5, 11.5)]
        self.first = leg(self.source, {"x": .5, "y": 13.5})

    def candidates(self, y=5.5):
        return [(0, 0, {"x": .5, "y": y}, {"x": .5, "y": y + 3}, {"x": .5, "y": y - 1})]

    def extend(self, reserved=None, candidates=None):
        return self.factory._parallel_belt_bridge_route(
            self.destination, self.trunks if reserved is None else reserved,
            self.candidates() if candidates is None else candidates, 0,
            self.first_position, self.first_drop, self.first_trial, self.first, end_direction=0)

    def test_two_parallel_trunks_have_a_complete_paid_route(self):
        before = deepcopy(self.trunks)
        result = self.factory._material_route(self.source, self.destination, self.trunks,
                                              start_direction=0, end_direction=0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(sum(e["name"] == "long-handed-inserter" for e in result["segments"]), 2)
        self.assertEqual(sum(e["name"] == "small-electric-pole" for e in result["segments"]), 2)
        self.assertEqual(result["segments"][0]["position"], self.source)
        self.assertEqual(result["segments"][-1]["position"], self.destination)
        self.assertEqual(result["segments"][-1]["direction"], 0)
        self.assertFalse(result["flow_verified"])
        self.assertEqual(self.trunks, before)
        trunks = {(e["position"]["x"], e["position"]["y"]) for e in self.trunks}
        self.assertTrue(all((e["position"]["x"], e["position"]["y"]) not in trunks
                            for e in result["segments"]))
        from factorio_ai.deterministic_input_links import _geometry, _path
        belts, edges, inlets = _geometry({"entities": result["segments"]})
        path = _path(belts, edges, (.5, 15.5), (.5, .5))
        self.assertIsNotNone(path)
        self.assertEqual(sum(e["name"] == "long-handed-inserter" for e in path), 2)
        self.assertEqual(inlets, [])

    def test_rotated_parallel_trunks_preserve_endpoint_facings(self):
        def rotate(p):
            return {"x": -p["y"], "y": p["x"]}
        trunks = [{**e, "position": rotate(e["position"]), "direction": 8} for e in self.trunks]
        source, destination = rotate(self.source), rotate(self.destination)
        result = self.factory._material_route(source, destination, trunks, start_direction=4, end_direction=4)
        self.assertTrue(result["ok"], result)
        arms = [e for e in result["segments"] if e["name"] == "long-handed-inserter"]
        self.assertEqual([e["direction"] for e in arms], [12, 12])
        self.assertEqual(result["segments"][0]["direction"], 4)
        self.assertEqual(result["segments"][-1]["direction"], 4)

    def test_single_crossing_keeps_existing_behavior(self):
        self.factory._parallel_belt_bridge_route = Mock(side_effect=AssertionError("second crossing unnecessary"))
        result = self.factory._material_route(self.source, self.destination, self.trunks[:123],
                                              start_direction=0, end_direction=0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(sum(e.get("name") == "long-handed-inserter" for e in result["segments"]), 1)

    def test_four_tile_spacing_shares_only_one_identical_belt(self):
        trunks = [e for e in self.trunks if e["position"]["y"] == 10.5]
        trunks += [entity("transport-belt", x + .5, 6.5, 4) for x in range(-61, 62)]
        result = self.extend(trunks, self.candidates(6.5))
        self.assertTrue(result["ok"], result)
        shared = [e for e in result["segments"] if e["position"] == self.first_drop]
        self.assertEqual(shared, [entity("transport-belt", .5, 9.5)])
        identities = [(e["name"], e["position"]["x"], e["position"]["y"]) for e in result["segments"]]
        self.assertEqual(len(identities), len(set(identities)))

    def test_reserved_second_arm_and_incomplete_legs_are_rejected(self):
        self.assertFalse(self.extend(self.trunks + [entity("small-electric-pole", .5, 6.5)])["ok"])
        self.builder.route = Mock(return_value={"ok": False, "reason": "no route within bounds"})
        self.assertFalse(self.extend()["ok"])
        self.assertEqual(self.builder.route.call_count, 1)

    def test_combined_placement_is_required_after_individual_equipment_fits(self):
        self.builder.can_place = Mock(side_effect=lambda rows: {"ok": len(rows) <= 4})
        self.assertFalse(self.extend()["ok"])
        self.assertTrue(any(len(call.args[0]) > 4 for call in self.builder.can_place.call_args_list))

    def test_second_crossing_search_never_tries_more_than_four_first_crossings(self):
        self.builder.route = Mock(side_effect=lambda source, destination, *args, **kwargs:
            leg(source, destination, kwargs.get("start_direction", 0)) if source == self.source
            else {"ok": False, "reason": "no route within bounds"})
        self.factory._parallel_belt_bridge_route = Mock(return_value={"ok": False})
        self.assertFalse(self.factory._belt_bridge_route(self.source, self.destination, self.trunks,
                                                        start_direction=0, end_direction=0)["ok"])
        self.assertEqual(self.factory._parallel_belt_bridge_route.call_count, 4)

    def test_second_crossings_are_bounded_and_must_advance_along_the_same_axis(self):
        rows = self.candidates(6.5) + self.candidates(5.5) + self.candidates(4.5)
        reserved = [entity("small-electric-pole", .5, 7.5), entity("small-electric-pole", .5, 6.5)]
        self.builder.can_place = Mock(return_value={"ok": True})
        self.assertFalse(self.extend(reserved, rows)["ok"])
        self.builder.can_place.assert_not_called()
        for candidates in (self.candidates(10.5), self.candidates(11.5),
                           [(0, 0, {"x": 1.5, "y": 5.5}, {"x": 1.5, "y": 8.5}, {"x": 1.5, "y": 4.5})]):
            self.assertFalse(self.extend([], candidates)["ok"])

    def test_connected_input_checks_both_crossing_power_poles(self):
        route = self.factory._material_route(self.source, self.destination, self.trunks,
                                             start_direction=0, end_direction=0)
        self.assertTrue(route["ok"], route)
        source = {"kind": "item", "item": "copper-plate", "direction": "output",
                  "position": self.source, "facing": 0}
        consumer = {**source, "direction": "input", "position": self.destination}
        self.factory.state["links"]["science:copper"] = {
            "ok": True, "entities": route["segments"], "source_port": source, "consumer_port": consumer}
        self.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
        action = {"type": "build", "name": "small-electric-pole"}
        self.factory.ensure_power_connection = Mock(side_effect=[{"status": "succeeded"}, action])
        result = self.factory.connect_input({"world_id": "one", "tick": 100}, source, consumer, "science:copper")
        self.assertEqual(result, action)
        calls = self.factory.ensure_power_connection.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0].args[1], calls[1].args[1])
        self.assertTrue(all(len(call.args[2]["entities"]) == 1 for call in calls))


if __name__ == "__main__":
    unittest.main()
