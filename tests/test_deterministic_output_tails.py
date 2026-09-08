from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory


def belt(x, direction=4):
    return {"name": "transport-belt", "position": {"x": x, "y": .5}, "direction": direction}


def port(x, y=.5, facing=4):
    return {"kind": "item", "item": "iron-plate", "direction": "output",
            "position": {"x": x, "y": y}, "facing": facing}


class PoweredOutputTailTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temporary.name)), query=Mock())
        catalog = SimpleNamespace(fingerprint="one", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(game, Mock(), catalog)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
        self.factory = DeterministicFactory(game, Mock(), self.builder, catalog)
        self.factory.ensure_power_connection = Mock(return_value={"status": "succeeded"})
        self.obs = {"world_id": "one", "tick": 1, "entities": []}
        self.factory._sync(self.obs)
        self.source, self.bus = port(.5, -1.5, 8), port(7.5)
        self.arm = {"name": "long-handed-inserter", "position": {"x": 4.5, "y": .5}, "direction": 12}
        self.pole = {"name": "small-electric-pole", "position": {"x": 4.5, "y": 2.5}, "direction": 0}
        self.entities = [belt(.5), belt(1.5), belt(2.5), self.arm, belt(6.5), belt(7.5), self.pole]
        self.factory.state["links"]["old-output"] = {"ok": True, "entities": self.entities,
            "source_port": port(.5), "consumer_port": self.bus}

    def entry(self):
        return next((tail for tail in self.factory._upstream_output_tails(self.obs, self.bus)
                     if tail["port"]["position"] == {"x": .5, "y": .5}), None)

    def test_merge_rebuilds_missing_crossing_and_power_before_reporting_connected(self):
        self.obs["entities"] = [deepcopy(belt(7.5))]
        original = deepcopy(self.factory.state["links"]["old-output"])
        def route(start, end, *args, **kwargs):
            if end == {"x": .5, "y": .5}:
                return {"ok": True, "segments": [
                    {"name": "transport-belt", "position": start, "direction": 8},
                    {"name": "transport-belt", "position": {"x": .5, "y": -.5}, "direction": 8},
                    {"name": "transport-belt", "position": end, "direction": 8}]}
            return {"ok": False, "reason": "no route within bounds"}
        self.factory._material_route = Mock(side_effect=route)
        waiting = {"status": "waiting", "reason": "crossing pole is disconnected"}
        self.factory.ensure_power_connection.return_value = waiting
        self.assertEqual(self.factory._merge_output(self.obs, self.source, self.bus, "new-output"), waiting)
        plan = self.factory.state["links"]["new-output"]
        self.assertIn(self.arm, plan["entities"])
        self.assertIn(self.pole, plan["entities"])
        self.assertEqual(plan["entities"][-1], belt(7.5))
        self.assertEqual(plan["consumer_port"], self.bus)
        self.assertEqual(self.factory.state["links"]["old-output"], original)
        self.builder.can_place.assert_called_once_with(plan["entities"])
        self.builder.ensure_plan.assert_called_once_with(self.obs, plan)
        self.factory.ensure_power_connection.assert_called_once_with(self.obs, "merge:new-output", plan)

    def test_changed_live_arm_or_foreign_tail_contents_reject_upstream_entry(self):
        for changed in ({**self.arm, "direction": 4}, {**belt(6.5), "belt_inventory": {"coal": 1}}):
            with self.subTest(changed=changed):
                self.obs["entities"] = [deepcopy(changed)]
                self.assertIsNone(self.entry())

    def test_missing_reserved_power_rejects_crossing_but_keeps_direct_suffix(self):
        self.entities.remove(self.pole)
        self.assertIsNone(self.entry())
        self.assertEqual([tail["port"]["position"] for tail in
                          self.factory._upstream_output_tails(self.obs, self.bus)], [{"x": 6.5, "y": .5}])

    def test_foreign_arm_reservation_cannot_share_identical_geometry(self):
        foreign = {**port(.5), "item": "coal"}
        self.factory.state["links"]["foreign"] = {"entities": [deepcopy(self.arm)],
            "source_port": foreign, "consumer_port": foreign}
        self.assertIsNone(self.entry())

    def test_identical_owned_same_item_dependencies_are_shared(self):
        self.factory.state["links"]["same-item"] = deepcopy(self.factory.state["links"]["old-output"])
        tail = self.entry()
        self.assertIsNotNone(tail)
        self.assertEqual(tail["entities"].count(self.arm), 1)
        self.assertEqual(tail["entities"].count(self.pole), 1)
        self.assertEqual(tail["entities"][-1], belt(7.5))

    def test_reversed_reserved_arm_does_not_create_upstream_path(self):
        self.arm["direction"] = 4
        self.assertIsNone(self.entry())

    def test_conflicting_reserved_arm_direction_rejects_path(self):
        self.entities.append({**self.arm, "direction": 4})
        self.assertIsNone(self.entry())


if __name__ == "__main__":
    unittest.main()
