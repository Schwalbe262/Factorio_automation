"""Recipe + target rate -> machine choice, machine count and belt-lane plan.

All rates are items per minute at nameplate speed (no modules, no
productivity, normal quality). Per-machine crafts/minute is
``60 * crafting_speed / recipe.energy``.

Lane plan: ``input_belts[0]`` is the NEAR input belt (plain inserters),
``input_belts[1]`` the FAR belt (long-handed inserters). A belt with one item
type carries it on both lanes; two item types use one lane each. Lanes are
named relative to the flow ("left" is the left-hand side when looking
downstream), so the plan is orientation independent.

Fluid rows (``fluid=True``): a recipe with exactly one fluid ingredient and a
single solid product (processing units, rocket fuel, concrete, electric
engines, solid fuel) is sized like any row on its solid ingredients; the fluid
arrives through the machine's input fluid box (``RowSizing.fluid``,
``blocks.row``: one fluid per row, an underground riser per machine to a
header pipe). Only machines with an input fluid box qualify.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from .derate import edge_capacity
from .geometry import tile_size
from .params import DEFAULT_LIMITS, RowLimits, inserter_rate, lane_capacity, pole_radius

MAX_SOLID_INGREDIENTS = 4


@dataclass(frozen=True)
class Flow:
    """One item stream: ``per_machine`` and ``total`` are items/minute."""

    item: str
    amount: float
    per_machine: float
    total: float


@dataclass(frozen=True)
class BeltLanes:
    """Item on each lane of one belt; equal names mean one item on both lanes."""

    left: str
    right: str

    @property
    def items(self) -> tuple[str, ...]:
        return (self.left,) if self.left == self.right else (self.left, self.right)


@dataclass(frozen=True)
class RowSizing:
    recipe: str
    machine: str
    machine_size: int
    crafting_speed: float
    crafts_per_machine: float
    count: int
    target_per_minute: float
    product: Flow
    ingredients: tuple[Flow, ...]
    input_belts: tuple[BeltLanes, ...]
    belt: str
    lane_capacity_per_minute: float
    max_count: int
    recipe_unlocked: bool
    fluid: Flow | None = None  # the one fluid ingredient of a fluid row
    # Nameplate crafts/minute of one machine; ``crafts_per_machine`` is lower
    # when the row was derated for ``inserter`` (see ``blocks.derate``).
    nameplate_crafts: float = 0.0
    inserter: str | None = None

    @property
    def rated_per_minute(self) -> float:
        return self.product.per_machine * self.count

    @property
    def derated(self) -> bool:
        return self.crafts_per_machine < self.nameplate_crafts - 1e-9

    def flow(self, item: str) -> Flow:
        return next(f for f in self.ingredients if f.item == item)

    def with_count(self, count: int) -> "RowSizing":
        """Same machine and lane plan with another machine count (lane budget enforced)."""
        if not 1 <= count <= self.max_count:
            raise ValueError(f"{count} machines exceed the lane budget of {self.max_count} for {self.recipe}")
        scale = [replace(f, total=f.per_machine * count) for f in self.ingredients]
        product = replace(self.product, total=self.product.per_machine * count)
        fluid = replace(self.fluid, total=self.fluid.per_machine * count) if self.fluid else None
        return replace(self, count=count, ingredients=tuple(scale), product=product, fluid=fluid)


def _amount(row: Mapping[str, Any], recipe: str) -> float:
    probability = float(row.get("independent_probability", row.get("probability", 1)))
    shared = row.get("shared_probability")
    if shared:
        probability *= float(shared["max"]) - float(shared["min"])
    if probability != 1 or row.get("extra_count_fraction"):
        raise ValueError(f"{recipe}: stochastic output {row['name']} cannot be rated by a row")
    if "amount" in row:
        return float(row["amount"])
    if "amount_min" in row and row.get("amount_min") == row.get("amount_max"):
        return float(row["amount_min"])
    raise ValueError(f"{recipe}: variable amount for {row['name']} cannot be rated by a row")


def fluid_ingredient(recipe: Mapping[str, Any]) -> str | None:
    """The single fluid ingredient of a fluid-row recipe (one fluid in, no fluid out), else None."""
    fluids = [r["name"] for r in recipe["ingredients"] if r.get("type") == "fluid"]
    out = [r for r in recipe["products"] if r.get("type") == "fluid"]
    return fluids[0] if len(fluids) == 1 and not out else None


def _validate_recipe(catalog: Any, name: str, fluid: bool = False) -> Mapping[str, Any]:
    recipe = catalog.recipes.get(name)
    if recipe is None:
        raise ValueError(f"unknown recipe {name!r}")
    if recipe.get("hidden"):
        raise ValueError(f"{name}: hidden recipe")
    rows = list(recipe["ingredients"]) + list(recipe["products"])
    fluids = sorted({r["name"] for r in rows if r.get("type") == "fluid"})
    if fluids and fluid and fluid_ingredient(recipe) is None:
        raise ValueError(f"{name}: a fluid row takes exactly one fluid ingredient and no fluid product")
    if fluids and not fluid:
        raise ValueError(
            f"{name}: fluid {', '.join(fluids)} not supported: the row reserves no pipe corridor; "
            "laying pipes needs in-game verification of the pipe_connections orientation frame "
            "and the pipe-to-ground max underground distance, which the catalog does not export"
        )
    solids = [r for r in recipe["ingredients"] if r.get("type", "item") == "item"]
    if not solids and not fluids:
        raise ValueError(f"{name}: no solid ingredient for an input belt")
    if len(solids) > MAX_SOLID_INGREDIENTS:
        raise ValueError(f"{name}: {len(solids)} solid ingredients; a row feeds at most {MAX_SOLID_INGREDIENTS}")
    if len(recipe["products"]) != 1:
        raise ValueError(f"{name}: {len(recipe['products'])} products; a row has one output lane")
    if not float(recipe.get("energy") or 0) > 0:
        raise ValueError(f"{name}: recipe energy must be positive")
    return recipe


def _unlocked_recipes(catalog: Any, enabled: Iterable[str] | None, researched: Iterable[str] | None) -> set[str]:
    """Catalog force state, extended by the caller's explicit sets."""
    names = {n for n, r in catalog.recipes.items() if r.get("enabled")}
    names.update(enabled or ())
    techs = {n for n, t in catalog.technologies.items() if t.get("researched")}
    techs.update(researched or ())
    for tech in techs:
        names.update((catalog.technologies.get(tech) or {}).get("unlocks", ()))
    return names


def _machine_item(row: Mapping[str, Any]) -> str:
    places = row.get("items_to_place_this") or []
    return places[0]["name"] if places else row["name"]


def row_supports_size(size: int, supply_radius: float) -> bool:
    """Odd square >= 3 (long-handed drop lands inside) whose inserter rows a gap pole reaches."""
    half = size // 2
    return size % 2 == 1 and size >= 3 and half + 1 < supply_radius + 0.5


def input_fluid_boxes(row: Mapping[str, Any]) -> list[dict]:
    """The machine's input fluid boxes, by index (catalog ``fluidbox_prototypes``)."""
    boxes = [b for b in row.get("fluidbox_prototypes") or () if b.get("production_type") == "input"]
    return sorted(boxes, key=lambda b: b.get("index", 0))


def _compatible(catalog: Any, name: str, recipe: Mapping[str, Any], radius: float) -> str:
    """Empty string when ``name`` can run ``recipe`` in a row, else the reason."""
    row = catalog.entities.get(name)
    if row is None:
        return "not in catalog"
    if fluid_ingredient(recipe) and not input_fluid_boxes(row):
        return "no input fluid box"
    if row.get("type") != "assembling-machine" or not row.get("electric"):
        return "not an electric assembling machine"
    if not float(row.get("crafting_speed") or 0) > 0:
        return "no crafting speed"
    if not set(recipe["categories"]) & set(row.get("crafting_categories") or ()):
        return "crafting categories do not include " + "/".join(recipe["categories"])
    width, height = tile_size(catalog, name)
    if width != height or not row_supports_size(width, radius):
        return f"{width}x{height} footprint does not fit the row pole/inserter geometry"
    return ""


def machine_candidates(catalog: Any, recipe: Mapping[str, Any], unlocked: set[str], radius: float) -> list[str]:
    """Unlocked row-compatible machines, fastest first; ties broken by name."""
    candidates = [
        (-float(row["crafting_speed"]), name)
        for name, row in catalog.entities.items()
        if not _compatible(catalog, name, recipe, radius)
        and any(r for r, rr in catalog.recipes.items() if r in unlocked and _produces(rr, _machine_item(row)))
    ]
    if not candidates:
        raise ValueError(f"{recipe['name']}: no unlocked row-compatible machine for {recipe['categories']}")
    return [name for _, name in sorted(candidates)]


def choose_machine(catalog: Any, recipe: Mapping[str, Any], unlocked: set[str], radius: float) -> str:
    """Fastest unlocked compatible machine; ties broken by name."""
    return machine_candidates(catalog, recipe, unlocked, radius)[0]


def _produces(recipe: Mapping[str, Any], item: str) -> bool:
    return any(p["name"] == item and p.get("type", "item") == "item" for p in recipe["products"])


def _belt_score(belts: tuple[BeltLanes, ...], flows: Mapping[str, float]) -> tuple[float, float, tuple]:
    lane_load = max(flows[b.left] / (2 if b.left == b.right else 1) for b in belts)
    belt_load = max(sum(flows[i] for i in b.items) for b in belts)
    return lane_load, belt_load, tuple((b.left, b.right) for b in belts)


def _belt(items: tuple[str, ...]) -> BeltLanes:
    return BeltLanes(items[0], items[-1])


def explicit_lanes(lanes: Iterable[Iterable[str]], items: Iterable[str]) -> tuple[BeltLanes, ...]:
    """Caller-chosen lane plan: belts in order (near first), each with 1-2 items."""
    belts = [tuple(belt) for belt in lanes]
    names = [item for belt in belts for item in belt]
    if not 1 <= len(belts) <= 2 or any(not 1 <= len(belt) <= 2 for belt in belts):
        raise ValueError("a lane plan has one or two belts with one or two items each")
    if len(set(names)) != len(names) or set(names) != set(items):
        raise ValueError(f"lane plan {belts} must carry each ingredient {sorted(items)} exactly once")
    return tuple(_belt(belt) for belt in belts)


def plan_lanes(flows: Mapping[str, float]) -> tuple[BeltLanes, ...]:
    """Assign 1-4 solid items to one or two belts, lowest lane load first; near belt carries more."""
    items = tuple(sorted(flows))
    if len(items) <= 2:
        return (_belt(items),)
    options = []
    for size in (1, 2):
        for near in itertools.combinations(items, size):
            far = tuple(i for i in items if i not in near)
            if len(far) <= 2:
                belts = tuple(sorted((_belt(near), _belt(far)), key=lambda b: -sum(flows[i] for i in b.items)))
                options.append(belts)
    return min(options, key=lambda belts: _belt_score(belts, flows))


def _max_count(per_machine: Mapping[str, float], belts: tuple[BeltLanes, ...], product: float, lane: float) -> int:
    limits = [lane / product]
    for belt in belts:
        lanes = 2 if belt.left == belt.right else 1
        limits.extend(lane * lanes / per_machine[i] for i in belt.items)
    return math.floor(min(limits) + 1e-9)


def _evaluate(
    catalog: Any,
    recipe: Mapping[str, Any],
    machine: str,
    rate_per_minute: float,
    *,
    belt: str,
    limits: RowLimits,
    inserter: str | None,
    lanes: Iterable[Iterable[str]] | None,
    unlocked: set[str],
) -> RowSizing:
    """Size ``recipe`` on ``machine``; derate for ``inserter`` when one is given."""
    name = recipe["name"]
    speed = float(catalog.entities[machine]["crafting_speed"])
    nameplate = 60 * speed / float(recipe["energy"])
    out = recipe["products"][0]
    product_amount = _amount(out, name)
    amounts: dict[str, float] = {}
    fluid_amount = 0.0
    for row in recipe["ingredients"]:
        if row.get("type") == "fluid":
            fluid_amount += _amount(row, name)
            continue
        amounts[row["name"]] = amounts.get(row["name"], 0) + _amount(row, name)
    if not amounts:
        belts = ()  # a fluid-only recipe (solid fuel from light oil): no input belt
    elif lanes is not None:
        belts = explicit_lanes(lanes, amounts)
    else:
        belts = plan_lanes({item: amount * nameplate for item, amount in amounts.items()})
    size = tile_size(catalog, machine)[0]
    crafts = nameplate
    if inserter is not None:
        rates = (
            inserter_rate(catalog, inserter, limits),
            inserter_rate(catalog, limits.long_inserter, limits),
            inserter_rate(catalog, inserter, limits),
        )
        demand = tuple(sum(amounts[i] for i in b.items) for b in belts)
        out_edge = size - 1 if fluid_ingredient(recipe) else size  # a fluid row's riser takes one output column
        out_cap = out_edge * rates[2] / product_amount
        if demand:
            crafts = min(nameplate, edge_capacity(size, demand, product_amount, rates).crafts, out_cap)
        else:
            crafts = min(nameplate, out_cap)
    per_product = crafts * product_amount
    count = max(1, math.ceil(rate_per_minute / per_product - 1e-9))
    per_machine = {item: amount * crafts for item, amount in amounts.items()}
    lane = lane_capacity(catalog, belt, limits)
    max_count = _max_count(per_machine, belts, per_product, lane)
    if count > max_count:
        raise ValueError(
            f"{name}: {count} x {machine} exceed one {belt} row (max {max_count}); split into several rows"
        )
    return RowSizing(
        recipe=name,
        machine=machine,
        machine_size=size,
        crafting_speed=speed,
        crafts_per_machine=crafts,
        count=count,
        target_per_minute=float(rate_per_minute),
        product=Flow(out["name"], product_amount, per_product, per_product * count),
        ingredients=tuple(
            Flow(item, amounts[item], per_machine[item], per_machine[item] * count) for item in sorted(amounts)
        ),
        input_belts=belts,
        belt=belt,
        lane_capacity_per_minute=lane,
        max_count=max_count,
        recipe_unlocked=name in unlocked,
        fluid=(
            Flow(fluid_ingredient(recipe), fluid_amount, fluid_amount * crafts, fluid_amount * crafts * count)
            if fluid_ingredient(recipe)
            else None
        ),
        nameplate_crafts=nameplate,
        inserter=inserter,
    )


def size_row(
    catalog: Any,
    recipe_name: str,
    rate_per_minute: float,
    *,
    machine: str | None = None,
    enabled_recipes: Iterable[str] | None = None,
    researched: Iterable[str] | None = None,
    belt: str = "transport-belt",
    pole: str = "small-electric-pole",
    limits: RowLimits = DEFAULT_LIMITS,
    inserter: str | None = None,
    lanes: Iterable[Iterable[str]] | None = None,
    fluid: bool = False,
) -> RowSizing:
    """Size one production row; ``ValueError`` names why a recipe cannot be laid out.

    ``fluid``: accept one fluid ingredient (module doc, fluid rows).

    ``machine`` overrides the choice (category and geometry are still checked).
    Unlocked = catalog force state plus ``enabled_recipes`` plus unlocks of
    ``researched`` technologies. The recipe itself may still be locked
    (planning ahead); ``recipe_unlocked`` reports it.

    ``inserter`` None (legacy): nameplate rates and the fastest unlocked
    machine; ``row.row_block`` rejects a machine whose edges cannot carry its
    demand. ``inserter`` given: every machine is derated to what that inserter
    (and ``limits.long_inserter`` on a far belt) can carry
    (``blocks.derate``), and among the unlocked machines the one needing the
    fewest machines wins (ties: the slower, cheaper machine, then the name).
    The recipe is rejected only when no machine fits one row.

    ``lanes`` optionally fixes the lane plan: belts near first, 1-2 items each.
    """
    if not isinstance(rate_per_minute, (int, float)) or not math.isfinite(rate_per_minute) or rate_per_minute <= 0:
        raise ValueError("rate_per_minute must be a positive finite number")
    recipe = _validate_recipe(catalog, recipe_name, fluid)
    radius = pole_radius(catalog, pole, limits)
    unlocked = _unlocked_recipes(catalog, enabled_recipes, researched)
    options = dict(belt=belt, limits=limits, inserter=inserter, lanes=lanes, unlocked=unlocked)
    if machine is not None or inserter is None:
        if machine is None:
            machine = choose_machine(catalog, recipe, unlocked, radius)
        reason = _compatible(catalog, machine, recipe, radius)
        if reason:
            raise ValueError(f"{recipe_name}: machine {machine} {reason}")
        return _evaluate(catalog, recipe, machine, rate_per_minute, **options)
    sized, reasons = [], []
    for candidate in machine_candidates(catalog, recipe, unlocked, radius):
        try:
            sized.append(_evaluate(catalog, recipe, candidate, rate_per_minute, **options))
        except ValueError as error:
            reasons.append(str(error))
    if not sized:
        raise ValueError(f"{recipe_name}: no unlocked machine fits one row with {inserter}: " + "; ".join(reasons))
    return min(sized, key=lambda s: (s.count, s.crafting_speed, s.machine))
