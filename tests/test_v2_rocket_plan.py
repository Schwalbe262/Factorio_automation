"""The rocket bill from the catalog and the roadmap tail generated from it (``rocket_plan``)."""

import unittest

from factorio_agent import progression as pg
from factorio_agent import rocket_plan as rp
from factorio_agent.core.world_catalog import WorldCatalog
from factorio_agent.jobs import CAPABILITIES, Request
from factorio_agent.production_rows import validate_parameters


def _rows(values, kind="item"):
    out = []
    for name, amount in values.items():
        row_kind, row_name = ("fluid", name[6:]) if name.startswith("fluid:") else (kind, name)
        out.append({"type": row_kind, "name": row_name, "amount": amount})
    return out


def _recipe(category, energy, ingredients, products):
    return {"category": category, "energy": energy, "ingredients": _rows(ingredients), "products": _rows(products)}


def _tech(prerequisites, packs, units, unlocks=(), trigger=None, researched=False):
    return {
        "prerequisites": list(prerequisites),
        "ingredients": _rows({p: 1 for p in packs}),
        "unit_count": None if trigger else units,
        "unit_energy": 30,
        "research_trigger": trigger,
        "unlocks": list(unlocks),
        "researched": researched,
    }


def _machine(kind, categories, speed, electric=True):
    return {"type": kind, "crafting_categories": list(categories), "crafting_speed": speed, "electric": electric}


# One input fluid box (2.1.20 assembling-machine-2 shape); enough for the fluid-row rule.
FLUID_BOX = [
    {
        "index": 1,
        "production_type": "input",
        "pipe_connections": [
            {
                "connection_type": "normal",
                "direction": 0,
                "positions": [{"x": 0, "y": -1}, {"x": 1, "y": 0}, {"x": 0, "y": 1}, {"x": -1, "y": 0}],
            }
        ],
    }
]
RED, GREEN, BLUE = "automation-science-pack", "logistic-science-pack", "chemical-science-pack"


def space_age_catalog():
    """A small catalog with the Space Age rocket shape (values as in 2.1 Space Age, chain shortened)."""
    recipes = {
        "iron-plate": _recipe("smelting", 3.2, {"iron-ore": 1}, {"iron-plate": 1}),
        "copper-plate": _recipe("smelting", 3.2, {"copper-ore": 1}, {"copper-plate": 1}),
        "steel-plate": _recipe("smelting", 16, {"iron-plate": 5}, {"steel-plate": 1}),
        "copper-cable": _recipe("crafting", 0.5, {"copper-plate": 1}, {"copper-cable": 2}),
        "electronic-circuit": _recipe("crafting", 0.5, {"iron-plate": 1, "copper-cable": 3}, {"electronic-circuit": 1}),
        "pipe": _recipe("crafting", 0.5, {"iron-plate": 1}, {"pipe": 1}),
        "basic-oil-processing": _recipe("oil-processing", 5, {"fluid:crude-oil": 100}, {"fluid:petroleum-gas": 45}),
        "advanced-oil-processing": _recipe(
            "oil-processing",
            5,
            {"fluid:water": 50, "fluid:crude-oil": 100},
            {"fluid:heavy-oil": 25, "fluid:light-oil": 45, "fluid:petroleum-gas": 55},
        ),
        "heavy-oil-cracking": _recipe(
            "chemistry", 2, {"fluid:water": 30, "fluid:heavy-oil": 40}, {"fluid:light-oil": 30}
        ),
        "light-oil-cracking": _recipe(
            "chemistry", 2, {"fluid:water": 30, "fluid:light-oil": 30}, {"fluid:petroleum-gas": 20}
        ),
        "plastic-bar": _recipe("chemistry", 1, {"coal": 1, "fluid:petroleum-gas": 20}, {"plastic-bar": 2}),
        "sulfur": _recipe("chemistry", 1, {"fluid:water": 30, "fluid:petroleum-gas": 30}, {"sulfur": 2}),
        "sulfuric-acid": _recipe(
            "chemistry", 1, {"iron-plate": 1, "sulfur": 5, "fluid:water": 100}, {"fluid:sulfuric-acid": 50}
        ),
        "solid-fuel-from-light-oil": _recipe("chemistry", 1, {"fluid:light-oil": 10}, {"solid-fuel": 1}),
        "rocket-fuel": _recipe(
            "crafting-with-fluid", 15, {"solid-fuel": 10, "fluid:light-oil": 10}, {"rocket-fuel": 1}
        ),
        "low-density-structure": _recipe(
            "crafting", 15, {"copper-plate": 20, "steel-plate": 2, "plastic-bar": 5}, {"low-density-structure": 1}
        ),
        "processing-unit": _recipe(
            "crafting-with-fluid", 10, {"electronic-circuit": 20, "fluid:sulfuric-acid": 5}, {"processing-unit": 1}
        ),
        "rocket-part": _recipe(
            "rocket-building",
            3,
            {"processing-unit": 1, "low-density-structure": 1, "rocket-fuel": 1},
            {"rocket-part": 1},
        ),
        "rocket-silo": _recipe(
            "crafting", 30, {"steel-plate": 1000, "processing-unit": 200, "pipe": 100}, {"rocket-silo": 1}
        ),
        "space-platform-starter-pack": _recipe(
            "crafting", 60, {"steel-plate": 20, "processing-unit": 20}, {"space-platform-starter-pack": 1}
        ),
    }
    technologies = {
        "steel-processing": _tech([], [RED], 50, ["steel-plate"], researched=True),
        "oil-processing": _tech([], [], 0, ["basic-oil-processing"], trigger={"type": "mine-entity"}),
        "plastics": _tech(["oil-processing"], [RED, GREEN], 200, ["plastic-bar"]),
        "sulfur-processing": _tech(["oil-processing"], [RED, GREEN], 150, ["sulfur", "sulfuric-acid"]),
        "chemical-science-pack": _tech(["plastics"], [RED, GREEN], 75, []),
        "advanced-oil-processing": _tech(
            ["chemical-science-pack"], [RED, GREEN, BLUE], 75, ["advanced-oil-processing", "solid-fuel-from-light-oil"]
        ),
        "processing-unit": _tech(
            ["chemical-science-pack", "sulfur-processing"], [RED, GREEN, BLUE], 300, ["processing-unit"]
        ),
        "low-density-structure": _tech(["chemical-science-pack"], [RED, GREEN, BLUE], 300, ["low-density-structure"]),
        "rocket-fuel": _tech(["advanced-oil-processing"], [RED, GREEN, BLUE], 300, ["rocket-fuel"]),
        "rocket-silo": _tech(
            ["processing-unit", "low-density-structure", "rocket-fuel"],
            [RED, GREEN, BLUE],
            1000,
            ["rocket-part", "rocket-silo", "space-platform-starter-pack"],
        ),
    }
    for name in ("copper-cable", "electronic-circuit", "pipe", "iron-plate", "copper-plate"):
        recipes[name]["enabled"] = True
    entities = {
        "iron-ore": {"type": "resource", "mining_products": _rows({"iron-ore": 1})},
        "copper-ore": {"type": "resource", "mining_products": _rows({"copper-ore": 1})},
        "coal": {"type": "resource", "mining_products": _rows({"coal": 1})},
        "crude-oil": {"type": "resource", "mining_products": _rows({"fluid:crude-oil": 10})},
        "stone-furnace": _machine("furnace", ["smelting"], 1, electric=False),
        "assembling-machine-1": _machine("assembling-machine", ["crafting"], 0.5),
        "assembling-machine-2": dict(
            _machine("assembling-machine", ["crafting", "crafting-with-fluid"], 0.75), fluidbox_prototypes=FLUID_BOX
        ),
        "chemical-plant": dict(_machine("assembling-machine", ["chemistry"], 1), fluidbox_prototypes=FLUID_BOX),
        "oil-refinery": _machine("assembling-machine", ["oil-processing"], 1),
        "character": {"type": "character", "crafting_categories": ["crafting"]},
        "rocket-silo": dict(
            _machine("rocket-silo", ["parameters", "rocket-building"], 1),
            rocket_parts_required=50,
            items_to_place_this=[{"name": "rocket-silo", "count": 1}],
        ),
    }
    items = {
        name: {"type": "item"} for recipe in recipes.values() for row in recipe["products"] for name in [row["name"]]
    }
    items.update({name: {"type": "item"} for name in ("iron-ore", "copper-ore", "coal", RED, GREEN, BLUE)})
    return WorldCatalog(
        {
            "recipes": recipes,
            "technologies": technologies,
            "entities": entities,
            "items": items,
            "fluids": {},
            "raw_sources": [{"type": "fluid", "name": "water"}],
        }
    )


RESEARCHED = ("steel-processing", "oil-processing", "plastics", "sulfur-processing", "chemical-science-pack")


def bill(**kwargs):
    kwargs.setdefault("researched", RESEARCHED)
    return rp.rocket_bill(space_age_catalog(), **kwargs)


def item(result, name):
    return next(row for row in result["items"] if row["item"] == name)


class RocketBillTests(unittest.TestCase):
    def test_silo_part_count_and_recipes_come_from_the_catalog(self):
        result = bill()
        self.assertEqual(result["parts_per_rocket"], 50)
        self.assertEqual(result["silo_item"], "rocket-silo")
        self.assertEqual(result["part_recipe"]["name"], "rocket-part")
        self.assertEqual(
            result["part_recipe"]["ingredients"], {"processing-unit": 1, "low-density-structure": 1, "rocket-fuel": 1}
        )
        self.assertEqual(result["payload"], "space-platform-starter-pack")

    def test_remaining_technologies_are_ordered_and_counted_per_pack(self):
        result = bill()
        names = [row["name"] for row in result["technologies"]]
        self.assertEqual(
            set(names),
            {"advanced-oil-processing", "processing-unit", "low-density-structure", "rocket-fuel", "rocket-silo"},
        )
        for index, name in enumerate(names):
            prerequisites = space_age_catalog().technologies[name]["prerequisites"]
            self.assertTrue(all(p not in names[index:] for p in prerequisites), name)
        self.assertEqual(names[-1], "rocket-silo")
        self.assertEqual(result["pack_types"], [RED, BLUE, GREEN])
        self.assertEqual(result["science_packs"][BLUE], 75 + 300 + 300 + 300 + 1000)

    def test_amounts_per_silo_and_per_rocket(self):
        result = bill(minutes=50)
        self.assertEqual(item(result, "rocket-part")["per_rocket"], 50)
        self.assertEqual(item(result, "pipe")["silo"], 100)
        # 200 PU for the silo, 50 for the parts, 20 for the payload.
        self.assertEqual(item(result, "processing-unit")["total"], 270)
        self.assertEqual(item(result, "electronic-circuit")["total"], 270 * 20)
        self.assertAlmostEqual(item(result, "pipe")["rate_per_minute"], 2.0)
        self.assertGreater(item(result, "processing-unit")["machines"], 0)

    def test_petroleum_comes_from_the_refinery_that_makes_light_oil(self):
        result = bill()
        self.assertEqual(item(result, "petroleum-gas")["recipe"], "advanced-oil-processing")
        self.assertEqual(item(result, "light-oil")["recipe"], "advanced-oil-processing")
        # Cracking takes the surplus: no refinery product backs up at the bill's ratios.
        self.assertNotIn("fluid:heavy-oil", result["surplus"])
        self.assertNotIn("fluid:light-oil", result["surplus"])
        stages = {row["recipe"]: row for row in result["cracking"]}
        self.assertEqual(set(stages), {"heavy-oil-cracking", "light-oil-cracking"})
        self.assertTrue(all(row["machine"] == "chemical-plant" for row in stages.values()))
        self.assertGreater(stages["heavy-oil-cracking"]["crafts"], 0)  # light oil binds here: heavy is cracked

    def test_every_item_names_its_producer_and_status(self):
        result = bill()
        expect = {
            "iron-plate": ("smelting_district", "roadmap"),
            "steel-plate": ("steel_production", "roadmap"),
            "plastic-bar": ("chemical_production", "roadmap"),
            "pipe": ("produce", "wired"),
            "electronic-circuit": ("produce", "wired"),
            "low-density-structure": ("produce", "wired"),
            "processing-unit": ("produce", "wired"),
            "rocket-fuel": ("produce", "wired"),
            "solid-fuel": ("produce", "wired"),
            "sulfuric-acid": ("fluid_product", "wired"),
            "petroleum-gas": ("advanced_petroleum", "wired"),
            "crude-oil": ("oil_gathering", "roadmap"),
            "rocket-part": ("rocket_parts", "missing"),
            "rocket-silo": ("rocket_silo", "missing"),
            "space-platform-starter-pack": ("rocket_launch", "missing"),
        }
        for name, (capability, status) in expect.items():
            producer = item(result, name)["producer"]
            self.assertEqual((producer["capability"], producer["status"]), (capability, status), name)
        gaps = {gap["capability"] for gap in result["gaps"]}
        self.assertTrue({"rocket_silo", "rocket_parts", "rocket_launch"} <= gaps)
        self.assertFalse(gaps & {"fluid_assembly", "chemical_product", "produce_merged"})

    def test_belts_follow_the_rate(self):
        result = bill(minutes=1)
        copper = item(result, "copper-plate")
        self.assertEqual(copper["belts"], -(-copper["rate_per_minute"] // 900))
        self.assertEqual(copper["bus_lanes_reserved"], 2)
        bus = {row["item"]: row for row in result["bus"]}
        for row in bus.values():
            self.assertEqual(row["lanes_short"], max(0, row["lanes_needed"] - row["reserved"]))
            self.assertGreaterEqual(row["taps"], 2)

    def test_rejects_a_bad_window(self):
        with self.assertRaises(ValueError):
            bill(minutes=0)


class BusLaneTests(unittest.TestCase):
    def test_the_planned_bus_reserves_the_bills_shared_items_with_spare_lanes(self):
        result = bill()
        base = ("iron-plate", "copper-plate")
        order = rp.bus_lane_order(result, base, capacity=20)
        self.assertEqual(order[: len(base)], base)
        for row in result["bus"]:
            if row["item"] not in result["raw"]:
                self.assertGreaterEqual(order.count(row["item"]), min(row["lanes_needed"], 1), row["item"])
        tight = rp.bus_lane_order(result, base, capacity=len(base) + rp.SPARE_LANES)
        self.assertEqual(tight, base)  # never eats the spare lanes

    def test_site_config_falls_back_without_a_rocket_bill(self):
        from types import SimpleNamespace

        from factorio_agent.site.planmodel import SiteConfig

        self.assertEqual(rp.site_config(SimpleNamespace(fingerprint="none")), SiteConfig())
        config = rp.site_config(space_age_catalog())
        self.assertEqual(config.lane_order[: len(SiteConfig().lane_order)], SiteConfig().lane_order)


class RocketTailTests(unittest.TestCase):
    def tail(self, **kwargs):
        return rp.tail(bill(**kwargs), existing=[s.id for s in pg.ROADMAP], researched_by=rp.roadmap_research())

    def test_research_rows_skip_researched_triggers_and_roadmap_research(self):
        rows = self.tail()
        research = [r["parameters"]["technology"] for r in rows if r["capability"] == "research"]
        self.assertNotIn("oil-processing", research)  # trigger
        self.assertNotIn("plastics", research)  # researched
        self.assertNotIn("advanced-material-processing", research)
        self.assertEqual(research[-1], "rocket-silo")
        for row in rows:
            if row["capability"] == "research":
                self.assertIn(rp.TAIL_ANCHOR, row["after"])

    def test_production_rows_use_produce_where_a_row_fits(self):
        rows = {r["id"]: r for r in self.tail(minutes=30)}
        pipe = rows["rocket_produce_pipe"]
        validate_parameters(pipe["parameters"])
        Request("k", "w", 0, pipe["capability"], pipe["parameters"]).validate()
        self.assertEqual(pipe["parameters"]["rate_per_minute"], round(100 / 30, 1))
        self.assertIn("rocket_produce_electronic_circuit", rows)
        # copper cable is only a sub-row of the circuit row: no row of its own
        self.assertNotIn("rocket_produce_copper_cable", rows)
        self.assertIn("rocket_produce_electronic_circuit", rows["rocket_produce_processing_unit"]["after"])
        # processing units cannot make circuits in-line: the circuit row is a belt-output row
        circuits = rows["rocket_produce_electronic_circuit"]["parameters"]
        self.assertEqual(circuits["output"], "belt")
        validate_parameters(circuits)
        lds = rows["rocket_produce_low_density_structure"]["parameters"]
        self.assertNotIn("output", lds)  # the rocket parts take it from the stock chest
        validate_parameters(lds)

    def test_produce_rows_name_their_input_and_output_mode(self):
        result = bill()
        self.assertEqual(item(result, "low-density-structure")["producer"]["layout"]["inputs"], "lane_merge")
        circuit = item(result, "electronic-circuit")["producer"]
        self.assertEqual(circuit["layout"], {"inputs": "sub_row:copper-cable", "output": "belt"})

    def test_stage_rows_end_the_tail_and_after_refers_to_earlier_ids(self):
        rows = self.tail()
        self.assertEqual([r["id"] for r in rows[-3:]], ["rocket_silo", "rocket_parts", "rocket_launch"])
        seen = {s.id for s in pg.ROADMAP}
        for row in rows:
            self.assertTrue(set(row["after"]) <= seen, row["id"])
            self.assertRegex(pg.NODE_PREFIX + row["id"], r"^[A-Za-z0-9_-]{1,64}$")
            seen.add(row["id"])

    def test_receipt_is_cached_and_compact(self):
        catalog = space_age_catalog()
        observation = {"technologies": {name: True for name in RESEARCHED}}
        first = rp.receipt(catalog, observation, capabilities=CAPABILITIES)
        self.assertIs(first, rp.receipt(catalog, observation, capabilities=CAPABILITIES))
        self.assertEqual(first["parts_per_rocket"], 50)
        self.assertEqual(first["remaining_technologies"], 5)
        self.assertTrue(first["tail"])


class RoadmapTailTests(unittest.TestCase):
    def observation(self):
        catalog = space_age_catalog()
        researched = {name: True for name in RESEARCHED}
        return {"technologies": researched, "rocket_plan": rp.receipt(catalog, {"technologies": researched})}

    def done(self):
        return [
            {"capability": s.capability, "parameters": dict(s.parameters), "status": "succeeded"} for s in pg.ROADMAP
        ]

    def test_tail_steps_append_to_the_roadmap(self):
        steps = pg.roadmap_for(self.observation())
        self.assertEqual(steps[: len(pg.ROADMAP)], pg.ROADMAP)
        self.assertEqual(steps[-1].capability, "rocket_launch")

    def test_malformed_or_clashing_rows_are_dropped(self):
        observation = {
            "rocket_plan": {
                "tail": [
                    {"id": "starter", "capability": "starter", "parameters": {}, "after": []},
                    {"id": "Bad-Id", "capability": "research", "parameters": {}, "after": []},
                    {"id": "ok_row", "capability": "research", "parameters": {"technology": "x"}, "after": ["nowhere"]},
                    "row",
                ]
            }
        }
        steps = pg.tail_steps(observation)
        self.assertEqual([s.id for s in steps], ["ok_row"])
        self.assertEqual(steps[0].after, ())

    def test_autopilot_proposes_the_tail_after_the_roadmap(self):
        nodes = pg.autopilot_nodes(self.done(), [], self.observation(), CAPABILITIES, limit=3)
        proposed = [n["id"] for n in nodes]
        self.assertTrue(proposed, proposed)
        self.assertTrue(all(i.startswith("auto_research_") or i.startswith("auto_rocket_produce_") for i in proposed))
        for node in nodes:
            Request("k", "w", 0, node["capability"], node["parameters"]).validate()

    def test_unregistered_tail_capabilities_are_never_proposed(self):
        observation = self.observation()
        jobs = self.done()
        for _ in range(12):
            nodes = pg.autopilot_nodes(jobs, [], observation, CAPABILITIES, limit=3)
            if not nodes:
                break
            for node in nodes:
                self.assertIn(node["capability"], CAPABILITIES)
                jobs.append({"capability": node["capability"], "parameters": node["parameters"], "status": "succeeded"})
        self.assertNotIn("rocket_silo", CAPABILITIES)  # until (d) registers it

    def test_a_faster_produce_row_satisfies_a_tail_row(self):
        step = pg.Step("rocket_produce_pipe", "produce", {"item": "pipe", "rate_per_minute": 8.3})
        fast = {"capability": "produce", "parameters": {"item": "pipe", "rate_per_minute": 20}, "status": "succeeded"}
        slow = dict(fast, parameters={"item": "pipe", "rate_per_minute": 2})
        self.assertTrue(pg._Progress((fast,), frozenset()).satisfied(step))
        self.assertFalse(pg._Progress((slow,), frozenset()).satisfied(step))
        self.assertTrue(pg._same(slow, step))


if __name__ == "__main__":
    unittest.main()
