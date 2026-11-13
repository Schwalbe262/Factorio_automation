"""The rocket bill from the catalog (``rocket_plan``)."""

import unittest

from factorio_agent import rocket_plan as rp
from factorio_agent.core.world_catalog import WorldCatalog


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
        "assembling-machine-2": _machine("assembling-machine", ["crafting", "crafting-with-fluid"], 0.75),
        "chemical-plant": _machine("assembling-machine", ["chemistry"], 1),
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
        self.assertIn("fluid:heavy-oil", result["surplus"])

    def test_every_item_names_its_producer_and_status(self):
        result = bill()
        expect = {
            "iron-plate": ("smelting_district", "roadmap"),
            "steel-plate": ("steel_production", "roadmap"),
            "plastic-bar": ("chemical_production", "roadmap"),
            "pipe": ("produce", "wired"),
            "electronic-circuit": ("produce", "wired"),
            "low-density-structure": ("produce_merged", "missing"),
            "processing-unit": ("fluid_assembly", "missing"),
            "rocket-fuel": ("fluid_assembly", "missing"),
            "solid-fuel": ("chemical_product", "missing"),
            "sulfuric-acid": ("fluid_product", "unwired"),
            "petroleum-gas": ("advanced_petroleum", "unwired"),
            "crude-oil": ("oil_gathering", "roadmap"),
            "rocket-part": ("rocket_parts", "missing"),
            "rocket-silo": ("rocket_silo", "missing"),
            "space-platform-starter-pack": ("rocket_launch", "missing"),
        }
        for name, (capability, status) in expect.items():
            producer = item(result, name)["producer"]
            self.assertEqual((producer["capability"], producer["status"]), (capability, status), name)
        gaps = {gap["capability"] for gap in result["gaps"]}
        self.assertTrue({"fluid_assembly", "rocket_silo", "rocket_parts", "rocket_launch"} <= gaps)

    def test_belts_follow_the_rate(self):
        result = bill(minutes=1)
        copper = item(result, "copper-plate")
        self.assertEqual(copper["belts"], -(-copper["rate_per_minute"] // 900))
        self.assertEqual(copper["bus_lanes_reserved"], 2)

    def test_rejects_a_bad_window(self):
        with self.assertRaises(ValueError):
            bill(minutes=0)


if __name__ == "__main__":
    unittest.main()
