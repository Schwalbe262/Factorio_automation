"""Shared fake and real catalogs for the ``factorio_agent.blocks`` tests."""

from __future__ import annotations

import json
from pathlib import Path

from factorio_agent.core.world_catalog import WorldCatalog

REAL_CATALOG = "runtime/v2/eval-780299489/catalog.json"
REAL_FALLBACK = "C:/Users/NEC/Documents/git/factorio_automation/" + REAL_CATALOG

# The inserter each recipe needs at assembling-machine-1 speed under the default rate model.
ROW_RECIPES = {
    "iron-gear-wheel": "fast-inserter",
    "copper-cable": "fast-inserter",
    "electronic-circuit": "fast-inserter",
    "automation-science-pack": "inserter",
    "logistic-science-pack": "inserter",
    "inserter": "fast-inserter",
    "low-density-structure": "inserter",
}
FLUID_RECIPES = ("rocket-fuel", "processing-unit")
FLOWS = ("north", "east", "south", "west")


def _box(size: float) -> dict:
    half = size / 2 - 0.3 if size > 1 else 0.15
    return {"left_top": {"x": -half, "y": -half}, "right_bottom": {"x": half, "y": half}}


def _entity(kind: str, size: int = 1, *, electric: bool = True, **extra) -> dict:
    return dict(type=kind, collision_box=_box(size), electric=electric, **extra)


def _recipe(energy: float, ingredients: dict, products: dict, category: str = "crafting", **extra) -> dict:
    def rows(values):
        return [
            {"type": "fluid" if name.startswith("fluid:") else "item", "name": name.removeprefix("fluid:"), "amount": n}
            for name, n in values.items()
        ]

    return dict(energy=energy, categories=[category], ingredients=rows(ingredients), products=rows(products), **extra)


def _machine(speed: float, size: int, categories: list[str]) -> dict:
    return _entity("assembling-machine", size, crafting_speed=speed, crafting_categories=categories)


def fake_catalog() -> WorldCatalog:
    """Vanilla-shaped numbers for the recipes under test, nothing researched."""
    crafting = ["advanced-crafting", "crafting"]
    entities = {
        "assembling-machine-1": _machine(0.5, 3, crafting),
        "assembling-machine-2": _machine(0.75, 3, crafting + ["crafting-with-fluid"]),
        "wide-plant": _machine(2, 4, crafting),
        "chemical-plant": _machine(1, 3, ["chemistry"]),
        "inserter": _entity("inserter"),
        "fast-inserter": _entity("inserter"),
        "long-handed-inserter": _entity("inserter"),
        "transport-belt": _entity("transport-belt", electric=False),
        "small-electric-pole": _entity("electric-pole", electric=False),
    }
    recipes = {
        "iron-gear-wheel": _recipe(0.5, {"iron-plate": 2}, {"iron-gear-wheel": 1}, enabled=True),
        "copper-cable": _recipe(0.5, {"copper-plate": 1}, {"copper-cable": 2}),
        "electronic-circuit": _recipe(0.5, {"iron-plate": 1, "copper-cable": 3}, {"electronic-circuit": 1}),
        "automation-science-pack": _recipe(
            5, {"copper-plate": 1, "iron-gear-wheel": 1}, {"automation-science-pack": 1}
        ),
        "logistic-science-pack": _recipe(6, {"transport-belt": 1, "inserter": 1}, {"logistic-science-pack": 1}),
        "inserter": _recipe(0.5, {"iron-plate": 1, "iron-gear-wheel": 1, "electronic-circuit": 1}, {"inserter": 1}),
        "low-density-structure": _recipe(
            15, {"copper-plate": 20, "steel-plate": 2, "plastic-bar": 5}, {"low-density-structure": 1}
        ),
        "rocket-fuel": _recipe(
            15, {"solid-fuel": 10, "fluid:light-oil": 10}, {"rocket-fuel": 1}, "crafting-with-fluid"
        ),
        "processing-unit": _recipe(
            10,
            {"electronic-circuit": 20, "advanced-circuit": 2, "fluid:sulfuric-acid": 5},
            {"processing-unit": 1},
            "crafting-with-fluid",
        ),
        "five-part": _recipe(1, {"a": 1, "b": 1, "c": 1, "d": 1, "e": 1}, {"five-part": 1}),
        "two-out": _recipe(1, {"a": 1}, {"b": 1, "c": 1}),
        "assembling-machine-1": _recipe(0.5, {"iron-plate": 9}, {"assembling-machine-1": 1}),
        "assembling-machine-2": _recipe(0.5, {"iron-plate": 20}, {"assembling-machine-2": 1}),
        "wide-plant": _recipe(0.5, {"iron-plate": 50}, {"wide-plant": 1}),
    }
    technologies = {
        "automation": {"researched": False, "unlocks": ["assembling-machine-1"]},
        "automation-2": {"researched": False, "unlocks": ["assembling-machine-2"]},
        "wide": {"researched": False, "unlocks": ["wide-plant"]},
    }
    return WorldCatalog.from_dict({"entities": entities, "recipes": recipes, "technologies": technologies})


def real_catalog_path() -> Path | None:
    for candidate in (Path(REAL_CATALOG), Path(REAL_FALLBACK)):
        if candidate.exists():
            return candidate
    return None


_REAL = {}


def real_catalog() -> WorldCatalog | None:
    path = real_catalog_path()
    if path is None:
        return None
    if path not in _REAL:
        _REAL[path] = WorldCatalog.from_dict(json.loads(path.read_text(encoding="utf-8")))
    return _REAL[path]
