import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from factorio_agent.blocks.audit import audit_row
from factorio_agent.blocks.geometry import xy
from factorio_agent.blocks.render import render_ascii
from factorio_agent.blocks.row import extend_row, main, row_block
from factorio_agent.blocks.sizing import size_row
from v2_blocks_fixture import FLOWS, ROW_RECIPES, fake_catalog, real_catalog, real_catalog_path

AM1 = "assembling-machine-1"
ORIGIN = (20.5, -7.5)


def build(catalog, recipe, machines=1, **kw):
    sizing = size_row(catalog, recipe, 1, machine=AM1).with_count(machines)
    return row_block(catalog, sizing, inserter=ROW_RECIPES[recipe], **kw)


def names(plan, name):
    return [e for e in plan["entities"] if e["name"] == name]


class RowLayoutContract:
    """Shared assertions; subclasses provide ``self.catalog``."""

    def test_every_recipe_and_orientation_audits_clean(self):
        for recipe in ROW_RECIPES:
            for flow in FLOWS:
                with self.subTest(recipe=recipe, flow=flow):
                    plan = build(self.catalog, recipe, origin=ORIGIN, flow=flow)
                    self.assertEqual(audit_row(self.catalog, plan), [])
                    self.assertEqual(plan["recipe"], recipe)

    def test_sizes_audit_clean(self):
        for count in (1, 2, 7, 20):
            with self.subTest(count=count):
                sizing = size_row(self.catalog, "automation-science-pack", 6 * count, machine=AM1)
                self.assertEqual(sizing.count, count)
                plan = row_block(self.catalog, sizing, origin=ORIGIN, flow="south")
                self.assertEqual(len(names(plan, AM1)), count)
                self.assertEqual(plan["rated_per_minute"], 6 * count)
                self.assertEqual(audit_row(self.catalog, plan), [])

    def test_three_ingredients_use_long_handed_second_belt(self):
        plan = build(self.catalog, "inserter")
        self.assertTrue(names(plan, "long-handed-inserter"))
        inputs = [p for p in plan["ports"] if p["direction"] == "input"]
        self.assertEqual(len({xy(p) for p in inputs}), 2)
        lds = build(self.catalog, "low-density-structure")
        self.assertEqual(len(names(lds, "long-handed-inserter")), 1)

    def test_extend_keeps_ports_and_audits_clean(self):
        for flow in FLOWS:
            with self.subTest(flow=flow):
                plan = build(self.catalog, "logistic-science-pack", 2, origin=ORIGIN, flow=flow)
                bigger = extend_row(plan, self.catalog, 2)
                self.assertEqual(bigger["ports"], plan["ports"])
                self.assertEqual(bigger["reserved_area"], plan["reserved_area"])
                self.assertEqual(len(names(bigger, AM1)), 4)
                self.assertEqual(bigger["expansion_slots"], [])
                self.assertEqual(audit_row(self.catalog, bigger), [])
                with self.assertRaisesRegex(ValueError, "reserved slots"):
                    extend_row(bigger, self.catalog, 1)


class FakeRow(RowLayoutContract, unittest.TestCase):
    def setUp(self):
        self.catalog = fake_catalog()

    def test_two_item_belt_ports_name_lanes(self):
        plan = build(self.catalog, "logistic-science-pack", origin=ORIGIN)
        inputs = [p for p in plan["ports"] if p["direction"] == "input"]
        self.assertEqual({p["lane"]: p["item"] for p in inputs}, {"left": "inserter", "right": "transport-belt"})
        self.assertEqual({xy(p) for p in inputs}, {(20.5, -10.5)})
        output = next(p for p in plan["ports"] if p["direction"] == "output")
        self.assertEqual((output["item"], output["lane"], output["facing"]), ("logistic-science-pack", "right", 4))

    def test_rotation_maps_canonical_layout(self):
        east = build(self.catalog, "inserter", origin=ORIGIN, flow="east")
        north = build(self.catalog, "inserter", origin=ORIGIN, flow="north")

        def turned(e):
            dx, dy = xy(e)[0] - ORIGIN[0], xy(e)[1] - ORIGIN[1]
            return e["name"], ORIGIN[0] + dy, ORIGIN[1] - dx

        self.assertEqual(sorted(map(turned, east["entities"])), sorted((e["name"], *xy(e)) for e in north["entities"]))
        self.assertEqual(north["growth"]["axis"], "y")
        self.assertEqual(east["growth"]["axis"], "x")

    def test_metadata(self):
        plan = build(self.catalog, "automation-science-pack", 2, origin=ORIGIN)
        for key in ("recipe", "machine", "count", "rated_per_minute", "reserved_area", "growth", "expansion_slots"):
            self.assertIn(key, plan)
        self.assertEqual(plan["machine"], AM1)
        self.assertEqual(len(plan["expansion_slots"]), 2)
        tiles = [xy(e) for e in plan["entities"]]
        (x0, y0), (x1, y1) = plan["reserved_area"]
        self.assertEqual(x0, min(t[0] for t in tiles) - 1.5)
        self.assertEqual(y1, max(t[1] for t in tiles) + 1.5)
        self.assertEqual(plan["power_port"]["position"], {"x": ORIGIN[0], "y": ORIGIN[1]})

    def test_rejections(self):
        sizing = size_row(self.catalog, "iron-gear-wheel", 60, machine=AM1)
        with self.assertRaisesRegex(ValueError, "faster inserter"):
            row_block(self.catalog, sizing)
        with self.assertRaisesRegex(ValueError, "tile centre"):
            row_block(self.catalog, sizing, inserter="fast-inserter", origin=(1, 1))
        with self.assertRaisesRegex(ValueError, "spare"):
            row_block(self.catalog, sizing, inserter="fast-inserter", spare=99)
        with self.assertRaisesRegex(ValueError, "same belt"):
            row_block(self.catalog, sizing, inserter="fast-inserter", belt="fast-transport-belt")

    def test_cli_prints_picture(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.json"
            path.write_text(json.dumps(self.catalog.to_dict()), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main([str(path), "logistic-science-pack", "10", "--machine", AM1])
            self.assertEqual(code, 0)
            self.assertIn("+MMM+MMM+", out.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main([str(path), "rocket-fuel", "1", "--machine", "assembling-machine-2"]), 2)
            self.assertIn("fluid", err.getvalue())

    def test_render_marks_ports_and_slots(self):
        picture = render_ascii(self.catalog, build(self.catalog, "logistic-science-pack", 2)).splitlines()
        self.assertTrue(picture[0].startswith("@>"))
        self.assertTrue(picture[-1].endswith(">O"))
        self.assertIn(":::", picture[2])


@unittest.skipIf(real_catalog_path() is None, "real eval-780299489 catalog not available")
class RealRow(RowLayoutContract, unittest.TestCase):
    def setUp(self):
        self.catalog = real_catalog()


if __name__ == "__main__":
    unittest.main()
