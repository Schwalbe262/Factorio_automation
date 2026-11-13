"""Fluid input rows: one fluid per row through underground risers onto a header pipe (``blocks.row``)."""

import copy
import unittest

from factorio_agent import progression as pg
from factorio_agent import rocket_plan as rp
from factorio_agent.blocks.audit import audit_row
from factorio_agent.blocks.geometry import xy
from factorio_agent.core.world_catalog import WorldCatalog
from factorio_agent.production_layout import audit_line, design_line, extend_line, fluid_inputs, input_demand
from factorio_agent.production_rows import validate_parameters
from factorio_agent.site.transform import Transform, apply
from test_v2_production_layout import PLATES, catalog as base_catalog
from test_v2_rocket_plan import RESEARCHED, space_age_catalog
from v2_blocks_fixture import _entity, _recipe, real_catalog


def _box(index, positions, kind="input", direction=0):
    return {
        "index": index,
        "production_type": kind,
        "pipe_connections": [
            {
                "connection_type": "normal",
                "direction": direction,
                "positions": [{"x": x, "y": y} for x, y in positions],
            }
        ],
    }


ASSEMBLER_BOXES = [
    _box(1, [(0, -1), (1, 0), (0, 1), (-1, 0)]),
    _box(2, [(0, 1), (-1, 0), (0, -1), (1, 0)], "output", 8),
]
PLANT_BOXES = [
    _box(1, [(-1, -1), (1, -1), (1, 1), (-1, 1)]),
    _box(2, [(1, -1), (1, 1), (-1, 1), (-1, -1)]),
    _box(3, [(-1, 1), (-1, -1), (1, -1), (1, 1)], "output", 8),
]


def catalog():
    data = base_catalog().to_dict()
    data["entities"]["assembling-machine-2"]["fluidbox_prototypes"] = ASSEMBLER_BOXES
    data["entities"]["chemical-plant"]["fluidbox_prototypes"] = PLANT_BOXES
    data["entities"]["pipe"] = _entity("pipe", electric=False)
    data["entities"]["pipe-to-ground"] = _entity("pipe-to-ground", electric=False)
    data["recipes"]["chemical-plant"] = _recipe(0.5, {"iron-plate": 5}, {"chemical-plant": 1})
    data["recipes"]["solid-fuel-from-light-oil"] = _recipe(1, {"fluid:light-oil": 10}, {"solid-fuel": 1}, "chemistry")
    return WorldCatalog.from_dict(data)


SOURCED = PLATES | {"electronic-circuit", "advanced-circuit", "solid-fuel"}


def line(c, product, rate, output="stock_chest"):
    return design_line(
        c,
        product,
        rate,
        inserter="fast-inserter",
        enabled=sorted(c.recipes) + ["assembling-machine-2", "chemical-plant"],
        sourced=SOURCED.__contains__,
        chest="iron-chest",
        output=output,
    )


class FluidRowLayoutTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()

    def test_processing_units_get_one_typed_header_port_and_a_riser_per_machine(self):
        found = line(self.catalog, "processing-unit", 6)
        self.assertEqual(audit_line(self.catalog, found), [])
        ports = fluid_inputs(found)
        self.assertEqual([(p["item"], p["kind"]) for p in ports], [("sulfuric-acid", "fluid")])
        main = found["main"]
        self.assertEqual(main["machine"], "assembling-machine-2")
        self.assertEqual(main["fluid"]["item"], "sulfuric-acid")
        machines = [e for e in main["entities"] if e.get("recipe")]
        self.assertTrue(all(m["direction"] == 8 for m in machines))  # input box turned to the output side
        risers = [e for e in main["entities"] if e["name"] == "pipe-to-ground"]
        self.assertEqual(len(risers), 2 * len(machines))
        self.assertGreater(input_demand(self.catalog, found, "sulfuric-acid"), 0)
        self.assertEqual(
            sorted(p["item"] for p in found["inputs"] if p["kind"] == "item"),
            ["advanced-circuit", "electronic-circuit"],
        )

    def test_every_rotation_and_an_extension_audit_clean(self):
        found = line(self.catalog, "processing-unit", 6)
        for rotation in (0, 4, 8, 12):
            with self.subTest(rotation=rotation):
                self.assertEqual(audit_line(self.catalog, apply(Transform(rotation, None, (0.5, 0.5)), found)), [])
        grown = extend_line(self.catalog, found, 1)
        self.assertEqual(audit_line(self.catalog, grown), [])
        self.assertEqual(fluid_inputs(grown), fluid_inputs(found))

    def test_a_fluid_only_chemistry_row_in_chemical_plants(self):
        found = line(self.catalog, "solid-fuel", 30, output="belt")
        self.assertEqual(audit_line(self.catalog, found), [])
        self.assertEqual(found["main"]["machine"], "chemical-plant")
        self.assertEqual([p["item"] for p in found["inputs"]], ["light-oil"])
        rocket_fuel = line(self.catalog, "rocket-fuel", 1)
        self.assertEqual(audit_line(self.catalog, rocket_fuel), [])

    def test_a_broken_header_or_a_turned_machine_is_caught(self):
        found = line(self.catalog, "processing-unit", 6)
        main = found["main"]
        header = sorted(xy(e) for e in main["entities"] if e["name"] == "pipe")
        cut = copy.deepcopy(main)
        cut["entities"] = [e for e in cut["entities"] if not (e["name"] == "pipe" and xy(e) == header[1])]
        self.assertTrue(any(f.startswith("fluid_riser_not_on_header") for f in audit_row(self.catalog, cut)))
        turned = copy.deepcopy(main)
        machine = next(e for e in turned["entities"] if e.get("recipe"))
        machine["direction"] = 0
        self.assertTrue(any(f.startswith("fluid_box_not_connected") for f in audit_row(self.catalog, turned)))
        foreign = copy.deepcopy(main)
        end = header[-1]
        foreign["entities"].append(
            {"name": "pipe-to-ground", "position": {"x": end[0], "y": end[1] + 1}, "direction": 0}
        )
        self.assertTrue(any(f.startswith("foreign_fluid_neighbour") for f in audit_row(self.catalog, foreign)))

    def test_a_machine_without_an_input_fluid_box_is_never_chosen(self):
        with self.assertRaises(ValueError):
            design_line(
                self.catalog,
                "processing-unit",
                6,
                inserter="fast-inserter",
                enabled=sorted(self.catalog.recipes) + ["assembling-machine-1"],
                sourced=SOURCED.__contains__,
                chest="iron-chest",
                genome={"form": "row", "machine": "assembling-machine-1", "spare": "default"},
            )


class FluidRowPlanTests(unittest.TestCase):
    def test_validate_takes_a_fluid_source_key(self):
        validate_parameters({"item": "processing-unit", "rate_per_minute": 4, "fluid_source": "processing-unit-acid"})
        with self.assertRaises(ValueError):
            validate_parameters({"item": "processing-unit", "rate_per_minute": 4, "fluid_source": "Bad Key"})

    def test_the_bill_makes_every_fluid_item_with_produce_and_supplies_its_key(self):
        bill = rp.rocket_bill(space_age_catalog(), researched=RESEARCHED)
        for name in ("processing-unit", "rocket-fuel", "solid-fuel"):
            producer = next(r for r in bill["items"] if r["item"] == name)["producer"]
            self.assertEqual(producer["capability"], "produce", name)
            self.assertTrue(producer["layout"]["fluid"], name)
        rows = {
            r["id"]: r for r in rp.tail(bill, existing=[s.id for s in pg.ROADMAP], researched_by=rp.roadmap_research())
        }
        unit = rows["rocket_produce_processing_unit"]
        key = unit["parameters"]["fluid_source"]
        supply = next(
            r for r in rows.values() if r["capability"] == "fluid_distribution" and key in r["parameters"]["aliases"]
        )
        self.assertIn(supply["id"], unit["after"])
        validate_parameters(unit["parameters"])


STAGES = {rp.STAGE_SILO, rp.STAGE_PARTS, rp.STAGE_LAUNCH}  # another worker registers these
# Real-catalog items without a producer today; a new one fails the test.
KNOWN_GAPS = {"electric-engine-unit"}  # engine-unit comes only from the engine row's stock chest


def unproduced(bill):
    from factorio_agent.jobs import CAPABILITIES

    return {
        r["item"]
        for r in bill["items"]
        if r["producer"]["capability"] not in CAPABILITIES and r["producer"]["capability"] not in STAGES
    }


class EveryBillItemHasAProducerTests(unittest.TestCase):
    def test_the_test_bill_is_fully_produced(self):
        self.assertEqual(unproduced(rp.rocket_bill(space_age_catalog(), researched=RESEARCHED)), set())

    @unittest.skipIf(real_catalog() is None, "real catalog not available")
    def test_the_real_bill_has_no_new_gap(self):
        catalog = real_catalog()
        if "rocket-silo" not in catalog.technologies:
            self.skipTest("catalog without a rocket silo")
        self.assertLessEqual(unproduced(rp.rocket_bill(catalog, researched=())), KNOWN_GAPS)


if __name__ == "__main__":
    unittest.main()
