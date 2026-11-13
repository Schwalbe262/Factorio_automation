"""Fluid input rows: one fluid per row through underground risers onto a header pipe (``blocks.row``)."""

import copy
import json
import unittest
from unittest.mock import patch

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
import test_v2_production_rows as rows_tests
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
    data["recipes"]["pipe-to-ground"] = _recipe(0.5, {"pipe": 10, "iron-plate": 5}, {"pipe-to-ground": 2})
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


class FluidRowControllerTests(unittest.TestCase):
    """A processing-unit row on the fake site: route from the published acid outlet, handoff, proof."""

    setUp_rows = rows_tests.ProductionRowsTests.setUp
    tearDown = rows_tests.ProductionRowsTests.tearDown
    obs = rows_tests.ProductionRowsTests.obs
    live = rows_tests.ProductionRowsTests.live

    def board(self, *, science=True, ports=(), bus=None):
        circuits = [
            dict(rows_tests.IRON_PORT, item=item, position={"x": 60.5, "y": 40.5 + 2 * i})
            for i, item in enumerate(("electronic-circuit", "advanced-circuit"))
        ]
        rows_tests.ProductionRowsTests.board(self, science=science, ports=tuple(circuits), bus=bus)

    def setUp(self):
        import test_v2_production_rows as rows_module

        rows_module_catalog = rows_module.catalog
        rows_module.catalog = catalog
        try:
            self.setUp_rows()
        finally:
            rows_module.catalog = rows_module_catalog
        self.game.expected_world = "w"
        self.acid = {
            "kind": "fluid",
            "item": "sulfuric-acid",
            "name": "pipe",
            "direction": "output",
            "facing": 4,
            "fluidbox_index": 1,
            "position": {"x": 80.5, "y": 70.5},
            "unit_number": 900,
            "world_id": "w",
        }
        registry = {
            "world_id": "w",
            "catalog_fingerprint": self.catalog.fingerprint,
            "sources": {"processing-unit-sulfuric-acid": self.acid},
            "handoffs": {},
        }
        (self.root / "fluid-sources.json").write_text(json.dumps(registry), encoding="utf-8")
        self.game.query.return_value = {"ok": True}

        def pipe_route(game, builder, obs, source, destination, core, prior_groups=(), *, owned_prior=None):
            self.assertEqual(source, self.acid)
            self.assertEqual(destination["item"], "sulfuric-acid")
            first = {"name": "pipe", "position": {"x": 81.5, "y": 70.5}, "direction": 0}
            return {
                "ok": True,
                "entities": [first, {"name": "pipe", "position": destination["position"], "direction": 0}],
                "underground_pairs": [],
                "first_neighbor": dict(first, fluidbox_index=1),
                "maximum_underground_span": 10,
            }

        self.pipe = patch("factorio_agent.chemical_production.plan_typed_fluid_connection", side_effect=pipe_route)
        self.pipe.start()
        iso = patch("factorio_agent.chemical_production.chemical_plan_isolation", return_value={"ok": True})
        iso.start()
        self.patches += [self.pipe, iso]

    def test_a_processing_unit_row_routes_declares_binds_and_proves(self):
        parameters = {"item": "processing-unit", "rate_per_minute": 4, "fluid_source": "processing-unit-sulfuric-acid"}
        state = self.rows.prepare(self.obs(), parameters)
        self.assertTrue(state["ok"], state)
        self.assertEqual(state["fluid_handoff"]["source"], self.acid)
        self.assertIn({"name": "pipe", "position": {"x": 81.5, "y": 70.5}, "direction": 0}, state["plan"]["entities"])
        self.rows.save("processing-unit", state)
        self.rows.step(self.obs(), parameters)
        registry = json.loads((self.root / "fluid-sources.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["handoffs"]["produce:processing-unit"]["sulfuric-acid"]["phase"], "declared")
        live = self.live(state["plan"], "processing-unit")
        done = self.rows.step(self.obs(live), parameters)
        if done.get("type") == "bar":  # the stock chest bar first (game.query answers ok without slots)
            self.game.query.return_value = {"ok": True, "slots": 2, "size": 32}
            done = self.rows.step(self.obs(live), parameters)
        self.assertEqual(done.get("status"), "succeeded", done)
        registry = json.loads((self.root / "fluid-sources.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["handoffs"]["produce:processing-unit"]["sulfuric-acid"]["phase"], "bound")

    def test_a_missing_outlet_is_a_planning_failure_naming_the_key(self):
        from factorio_agent import plan_needs

        parameters = {"item": "processing-unit", "rate_per_minute": 4, "fluid_source": "elsewhere"}
        result = self.rows.prepare(self.obs(), parameters)
        self.assertFalse(result["ok"])
        reasons = {f.get("reason") for f in result.get("failures", [])} | {
            f.get("reason") for f in result.get("layout_failures", [])
        }
        self.assertTrue(
            reasons & {"production_row_fluid_source_missing", "no_production_row_site_with_complete_connections"},
            result,
        )
        self.assertIsNotNone(plan_needs.from_result("produce", result, parameters))
        without = self.rows.prepare(self.obs(), {"item": "processing-unit", "rate_per_minute": 4})
        self.assertEqual(without["reason"], "production_row_fluid_source_required")


STAGES = {rp.STAGE_SILO, rp.STAGE_PARTS, rp.STAGE_LAUNCH}  # another worker registers these
# Real-catalog items without a producer today; a new one fails the test.
KNOWN_GAPS: set = set()  # no gap: electric engines take engine units from a belt-output engine row


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
        bill = rp.rocket_bill(catalog, researched=())
        self.assertLessEqual(unproduced(bill), KNOWN_GAPS)
        rows = {
            r["id"]: r for r in rp.tail(bill, existing=[s.id for s in pg.ROADMAP], researched_by=rp.roadmap_research())
        }
        engines = rows["rocket_produce_engine_unit_belt"]
        self.assertEqual(engines["parameters"]["output"], "belt")
        self.assertIn("rocket_produce_engine_unit_belt", rows["rocket_produce_electric_engine_unit"]["after"])
        validate_parameters(engines["parameters"])


if __name__ == "__main__":
    unittest.main()
