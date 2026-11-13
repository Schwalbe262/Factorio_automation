import copy
import unittest

from factorio_agent.blocks.geometry import step, xy
from factorio_agent.blocks.render import render_ascii
from factorio_agent.blocks.row import extend_row, row_block
from factorio_agent.blocks.sizing import size_row
from factorio_agent.blocks.stock_tail import STOCK_STACKS, audit_stock_tail, stock_tail
from factorio_agent.core.world_catalog import WorldCatalog
from factorio_agent.production_layout import audit_line, design_line, extend_line, input_demand
from factorio_agent.site.transform import Transform, apply
from v2_blocks_fixture import FLOWS, _entity, _recipe, fake_catalog, real_catalog

AM1 = "assembling-machine-1"
PLATES = {"iron-plate", "copper-plate", "steel-plate"}


def catalog():
    """The blocks fake catalog plus a chest, a belt recipe and pipes."""
    data = fake_catalog().to_dict()
    data["entities"]["iron-chest"] = _entity("container", electric=False)
    data["recipes"]["transport-belt"] = _recipe(
        0.5, {"iron-plate": 1, "iron-gear-wheel": 1}, {"transport-belt": 2}, enabled=True
    )
    data["recipes"]["pipe"] = _recipe(0.5, {"iron-plate": 1}, {"pipe": 1}, enabled=True)
    data["recipes"]["iron-plate"] = _recipe(3.2, {"iron-ore": 1}, {"iron-plate": 1}, "smelting", enabled=True)
    data["technologies"]["automation"]["researched"] = True
    return WorldCatalog.from_dict(data)


def line_for(c, product, rate=30, inserter="inserter", origin=(0.5, 0.5)):
    return design_line(
        c,
        product,
        rate,
        inserter=inserter,
        enabled=sorted(c.recipes),
        sourced=PLATES.__contains__,
        chest="iron-chest",
        origin=origin,
    )


class StockTailTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()

    def row(self, flow, count=2, spare=2):
        sizing = size_row(self.catalog, "iron-gear-wheel", 45 * count, machine=AM1, inserter="inserter")
        return row_block(self.catalog, sizing, origin=(10.5, -3.5), flow=flow, spare=spare)

    def test_tail_geometry_in_every_flow(self):
        for flow in FLOWS:
            with self.subTest(flow=flow):
                plan = self.row(flow)
                tail = stock_tail(self.catalog, plan, chest="iron-chest")
                port = next(p for p in plan["ports"] if p["direction"] == "output")
                self.assertEqual(xy(tail["arm"]), step(xy(port), port["facing"]))
                self.assertEqual(xy(tail["chest"]), step(xy(port), port["facing"], 2))
                self.assertEqual(tail["arm"]["direction"], (port["facing"] + 8) % 16)
                self.assertEqual(tail["port"]["position"], tail["chest"]["position"])
                self.assertEqual(tail["bar_slots"], STOCK_STACKS)
                self.assertEqual(audit_stock_tail(self.catalog, plan, tail), [])

    def test_bridge_poles_reach_the_last_built_pole_and_never_block_extension(self):
        plan = self.row("east", count=1, spare=4)
        tail = stock_tail(self.catalog, plan, chest="iron-chest")
        self.assertGreater(len(tail["entities"]), 3)
        self.assertEqual(audit_stock_tail(self.catalog, plan, tail), [])
        extended = extend_row(plan, self.catalog, 4)
        self.assertEqual(audit_stock_tail(self.catalog, extended, tail), [])

    def test_sabotage_is_caught(self):
        plan = self.row("south")
        tail = stock_tail(self.catalog, plan, chest="iron-chest")
        turned = copy.deepcopy(tail)
        turned["arm"]["direction"] = (turned["arm"]["direction"] + 8) % 16
        self.assertIn("stock_tail_pickup_not_belt_end", audit_stock_tail(self.catalog, plan, turned))
        far = copy.deepcopy(tail)
        for e in far["entities"][2:]:
            e["position"] = {"x": e["position"]["x"] + 40, "y": e["position"]["y"] + 40}
        found = audit_stock_tail(self.catalog, plan, far)
        self.assertIn("stock_tail_pole_not_wired", found)
        unbounded = dict(tail, bar_slots=0)
        self.assertIn("stock_tail_unbounded", audit_stock_tail(self.catalog, plan, unbounded))

    def test_tail_rate_is_one_inserter(self):
        tail = stock_tail(self.catalog, self.row("east"), chest="iron-chest")
        self.assertEqual(tail["stock_rate_per_minute"], 30)
        fast = stock_tail(self.catalog, self.row("east"), inserter="fast-inserter", chest="iron-chest")
        self.assertEqual(fast["stock_rate_per_minute"], 90)


class LineDesignTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()

    def test_single_ingredient_line_has_no_sub_row(self):
        line = line_for(self.catalog, "pipe", 30)
        self.assertIsNone(line["sub"])
        self.assertEqual([p["item"] for p in line["inputs"]], ["iron-plate"])
        self.assertEqual(audit_line(self.catalog, line), [])
        self.assertEqual(line["output"]["name"], "iron-chest")

    def test_one_level_intermediate_gets_a_sub_row_feeding_the_far_belt(self):
        line = line_for(self.catalog, "transport-belt", 60)
        self.assertEqual(line["spec"]["sub_item"], "iron-gear-wheel")
        self.assertEqual(sorted(p["item"] for p in line["inputs"]), ["iron-plate", "iron-plate"])
        self.assertEqual(line["joint"]["to"]["item"], "iron-gear-wheel")
        self.assertEqual(audit_line(self.catalog, line), [])
        main = line["main"]
        gears = main["crafts_per_machine"] * (main["count"] + main["growth"]["remaining_slots"])
        self.assertGreaterEqual(line["sub"]["rated_per_minute"] + 1e-9, gears)
        self.assertGreater(input_demand(self.catalog, line, "iron-plate"), 0)
        self.assertIn("C", render_ascii(self.catalog, {"entities": line["entities"], "ports": line["inputs"]}))

    def test_sourced_intermediate_needs_no_sub_row(self):
        line = design_line(
            self.catalog,
            "transport-belt",
            30,
            inserter="inserter",
            enabled=sorted(self.catalog.recipes),
            sourced=(PLATES | {"iron-gear-wheel"}).__contains__,
            chest="iron-chest",
        )
        self.assertIsNone(line["sub"])
        self.assertEqual(sorted(p["item"] for p in line["inputs"]), ["iron-gear-wheel", "iron-plate"])

    def test_three_solid_tree_becomes_a_cluster_line(self):
        line = line_for(self.catalog, "inserter", 10)
        self.assertEqual(line["spec"]["kind"], "cluster")
        self.assertEqual(audit_line(self.catalog, line), [])
        self.assertEqual(sorted(p["item"] for p in line["inputs"]), ["copper-plate", "iron-plate"])
        self.assertEqual(line["output"]["name"], "iron-chest")
        self.assertGreater(input_demand(self.catalog, line, "copper-plate"), 0)
        with self.assertRaisesRegex(ValueError, "new row block"):
            extend_line(self.catalog, line, 1)

    def test_rejections_name_the_reason(self):
        with self.assertRaisesRegex(ValueError, "no source"):
            design_line(
                self.catalog,
                "pipe",
                10,
                inserter="inserter",
                enabled=[],
                sourced=lambda item: False,
                chest="iron-chest",
            )

    def test_every_rotation_audits_clean(self):
        line = line_for(self.catalog, "transport-belt", 60)
        for rotation in (0, 4, 8, 12):
            with self.subTest(rotation=rotation):
                turned = apply(Transform(rotation, None, (0.5, 0.5)), line)
                self.assertEqual(audit_line(self.catalog, turned), [])

    def test_extension_keeps_ports_and_the_sub_row(self):
        line = line_for(self.catalog, "transport-belt", 30)
        grown = extend_line(self.catalog, line, 1)
        self.assertEqual(grown["main"]["count"], line["main"]["count"] + 1)
        self.assertEqual((grown["inputs"], grown["output"]), (line["inputs"], line["output"]))
        self.assertEqual(grown["sub"], line["sub"])
        self.assertEqual(audit_line(self.catalog, grown), [])

    def test_origin_shift_moves_everything_rigidly(self):
        a = line_for(self.catalog, "pipe", 30)
        b = line_for(self.catalog, "pipe", 30, origin=(10.5, -4.5))
        self.assertEqual(xy(b["output"]), (xy(a["output"])[0] + 10, xy(a["output"])[1] - 5))


@unittest.skipIf(real_catalog() is None, "real eval-780299489 catalog not available")
class RealLineTests(unittest.TestCase):
    def test_construction_lines_audit_clean(self):
        c = real_catalog()
        for product, rate in (("transport-belt", 60), ("electronic-circuit", 60), ("iron-gear-wheel", 120)):
            for inserter in ("inserter", "fast-inserter"):
                with self.subTest(product=product, inserter=inserter):
                    line = line_for(c, product, rate, inserter=inserter)
                    self.assertEqual(audit_line(c, line), [])
                    self.assertEqual(audit_line(c, extend_line(c, line, 1)), [])


if __name__ == "__main__":
    unittest.main()
