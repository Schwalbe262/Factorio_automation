from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from factorio_agent.board import entity
from factorio_agent.site.fakeworld import ORES, make_world
from factorio_agent.site.geometry import Rect
from factorio_agent.site.render import main as render_main
from factorio_agent.site.render import render_svg

SVG = "{http://www.w3.org/2000/svg}"


class FakeWorldTests(unittest.TestCase):
    def test_same_seed_gives_the_same_map(self):
        self.assertEqual(make_world(5).to_json(), make_world(5).to_json())

    def test_different_seeds_give_different_geography(self):
        maps = {make_world(seed).to_json() for seed in range(4)}
        self.assertEqual(len(maps), 4)
        centroids = {tuple(round(c) for c in make_world(s).resource_patches("iron-ore")[0].centroid) for s in range(4)}
        self.assertGreater(len(centroids), 1)

    def test_every_resource_and_terrain_type_appears(self):
        world = make_world(9)
        names = {p.name for p in world.resource_patches()}
        self.assertTrue(set(ORES) | {"crude-oil"} <= names)
        self.assertTrue(world.terrain_tiles())
        self.assertTrue(world.features("tree"))
        self.assertTrue(world.features("rock"))
        self.assertIsNotNone(world.nearest_shore(0, 0, 200))

    def test_start_area_is_clear(self):
        for seed in range(5):
            report = make_world(seed).free_rect(-12, -12, 12, 12, allow_clearable=False)
            self.assertTrue(report.free, seed)
            self.assertEqual(report.resources, {})

    def test_whole_map_is_surveyed_and_size_is_validated(self):
        world = make_world(1, size=128)
        self.assertEqual(world.bounds(), Rect(-64, -64, 64, 64))
        with self.assertRaises(ValueError):
            make_world(1, size=100)


class RenderTests(unittest.TestCase):
    def test_svg_is_well_formed_and_draws_every_layer(self):
        world = make_world(2, size=128)
        world.reserve(Rect(-8, -8, 8, 8), "main <bus>")
        world.apply_plan([entity("assembling-machine-2", 10.5, 10.5)], owner="mall")
        plans = [entity("stone-furnace", -5.0, -5.0), entity("inserter", -3.5, -5.5, 4)]
        root = ET.fromstring(render_svg(world, plans=plans, scale=2))
        self.assertEqual(root.tag, SVG + "svg")
        self.assertEqual(root.get("width"), "256")
        rects = root.findall(SVG + "rect")
        self.assertGreater(len(rects), 50)
        self.assertEqual([t.text for t in root.findall(SVG + "text")], ["main <bus>"])
        self.assertEqual(len([r for r in rects if r.get("data-name")]), 2)

    def test_reservations_can_be_hidden(self):
        world = make_world(2, size=64)
        world.reserve(Rect(0, 0, 4, 4), "zone")
        root = ET.fromstring(render_svg(world, reservations=False))
        self.assertEqual(root.findall(SVG + "text"), [])

    def test_command_line_renders_a_cached_map(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "map.json"
            target = Path(folder) / "map.svg"
            source.write_text(make_world(4, size=64).to_json(), encoding="utf-8")
            self.assertEqual(render_main([str(source), str(target)]), 0)
            ET.fromstring(target.read_text(encoding="utf-8"))
            with redirect_stderr(io.StringIO()):
                self.assertEqual(render_main([]), 2)


if __name__ == "__main__":
    unittest.main()
