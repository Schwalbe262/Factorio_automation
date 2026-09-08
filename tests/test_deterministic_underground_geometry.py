from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_input_links import _geometry, _path, _powered_prefix
from factorio_ai.factory_templates import DIRECTIONS


def belt(x, y, direction, role=None):
    entity = {"name": "underground-belt" if role else "transport-belt",
              "position": {"x": x, "y": y}, "direction": direction}
    if role:
        entity["belt_to_ground_type"] = role
    return entity


def route(direction=0):
    dx, dy = DIRECTIONS[direction]
    entities = [belt(.5 + dx * n, .5 + dy * n, direction, role)
                for n, role in ((0, None), (1, "input"), (5, "output"), (6, None))]
    return {"ok": True, "entities": entities, "ports": [], "underground_pairs": [
        {"input": deepcopy(entities[1]), "output": deepcopy(entities[2]), "max_distance": 5}]}


class UndergroundGeometryTests(unittest.TestCase):
    def test_cardinal_paths_include_both_mouths_and_preserve_catalog_bounded_pair(self):
        for direction in DIRECTIONS:
            with self.subTest(direction=direction):
                plan = route(direction)
                belts, edges, inlets = _geometry(plan)
                first, last = plan["entities"][0]["position"], plan["entities"][-1]["position"]
                path = _path(belts, edges, tuple(first.values()), tuple(last.values()))
                self.assertEqual(path, plan["entities"])
                self.assertEqual(inlets, [])
                self.assertEqual(_powered_prefix(plan, path), plan)

    def test_malformed_or_unpaired_tunnels_never_create_reachable_edges(self):
        original = route()
        cases = []
        def change(fn):
            plan = deepcopy(original)
            fn(plan)
            cases.append(plan)
        change(lambda p: p.pop("underground_pairs"))
        change(lambda p: p["underground_pairs"].append(deepcopy(p["underground_pairs"][0])))
        change(lambda p: p["entities"][1].update(belt_to_ground_type="output"))
        change(lambda p: p["underground_pairs"][0].update(max_distance=3))
        change(lambda p: p["underground_pairs"][0].update(max_distance=True))
        change(lambda p: p["underground_pairs"][0].update(max_distance=0))
        change(lambda p: p["entities"][2].update(direction=4))
        change(lambda p: p["entities"].pop(2))
        change(lambda p: p["entities"].append(belt(.5, -.5, 0)))
        change(lambda p: p["entities"][1]["position"].update(x=float("nan")))
        for plan in cases:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                _geometry(plan)

    def test_behind_off_axis_fractional_and_intercepted_pairs_are_rejected(self):
        for x, y in ((.5, 2.5), (1.5, -4.5), (.5, -3.75)):
            plan = route()
            plan["entities"][2]["position"] = {"x": x, "y": y}
            plan["underground_pairs"][0]["output"] = deepcopy(plan["entities"][2])
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                _geometry(plan)
        plan = route()
        inner = {"input": belt(.5, -1.5, 0, "input"), "output": belt(.5, -2.5, 0, "output"), "max_distance": 5}
        plan["entities"].extend([inner["input"], inner["output"]])
        plan["underground_pairs"].append(inner)
        with self.assertRaisesRegex(ValueError, "interrupts"):
            _geometry(plan)

    def test_tunnel_does_not_create_surface_edges_or_accept_unproven_side_feeds(self):
        plan = route()
        plan["entities"].extend([belt(.5, -1.5, 0), belt(-.5, -.5, 4), belt(-.5, -4.5, 4)])
        belts, edges, _ = _geometry(plan)
        self.assertEqual(edges[(.5, -.5)], [((.5, -4.5), None)])
        self.assertEqual(edges[(-.5, -.5)], [])
        self.assertEqual(edges[(-.5, -4.5)], [])
        self.assertIsNone(_path(belts, edges, (.5, .5), (.5, -1.5)))

    def test_prefix_omits_untraversed_pairs_but_cannot_drop_one_mouth(self):
        plan = route()
        self.assertEqual(_powered_prefix(plan, [plan["entities"][0]])["underground_pairs"], [])
        with self.assertRaisesRegex(ValueError, "both underground endpoints"):
            _powered_prefix(plan, plan["entities"][:2])

    def test_inserter_access_to_a_tunnel_requires_separate_supported_geometry(self):
        plan = route()
        plan["entities"].append({"name": "inserter", "position": {"x": -.5, "y": -.5}, "direction": 12})
        with self.assertRaisesRegex(ValueError, "inserter access"):
            _geometry(plan)

    def test_opposite_role_cannot_share_a_reserved_endpoint(self):
        with TemporaryDirectory() as root:
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(root)))
            catalog = SimpleNamespace(fingerprint="catalog", entities={})
            builder = FactoryBuilder(game, None, catalog)
            factory = DeterministicFactory(game, None, builder, catalog)
            obs = {"world_id": "one", "tick": 1}
            mouth = belt(.5, .5, 0, "input")
            self.assertTrue(factory.register_plan("owner", {"ok": True, "entities": [mouth]}, obs)["ok"])
            self.assertTrue(factory.register_plan("shared", {"ok": True, "entities": [deepcopy(mouth)]}, obs)["ok"])
            changed = deepcopy(mouth)
            changed["belt_to_ground_type"] = "output"
            self.assertFalse(factory.register_plan("conflict", {"ok": True, "entities": [changed]}, obs)["ok"])
            self.assertNotIn("conflict", factory.state["blocks"])


if __name__ == "__main__":
    unittest.main()
