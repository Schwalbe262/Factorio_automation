"""One produced line: a recipe row, an optional upstream sub-row, a stock tail.

Built on ``blocks`` (``size_row`` -> ``row_block`` -> ``audit_row``) and
``blocks.stock_tail``. Pure Python, no game IO.

Canonical layout (flow east; ``origin`` = the main row's first pole)::

    sub-row (only for a one-level intermediate)   main row
    @>>>>>>>>>>>>>>>>                              (plates for the sub-row)
    .ii.ii.ii........
    +MMM+MMM+:::.:::.
    .MMM.MMM.:::.:::.
    .MMM.MMM.:::.:::.
    ..i...i..........         @>>>>>>>>>>>>>>>>>  near belt: sourced ingredient
    >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>  far belt: the sub-row's output runs
                                .iiL.iiL.........   straight on as the main row's last
                                ...                 input belt (one internal joint)
                                >>>>>>>>>>>>>>>>O iC  output belt -> stock tail chest

Lane rule (routable inputs): every input belt carries ONE item on both lanes,
because ``transport.plan_connection`` lays a plain belt to a port and cannot
fill one lane of a shared belt. So a line takes at most two solid
ingredients (near + far belt); three or four need a lane-merge adapter that
does not exist yet (``ValueError``). Belts are ordered by flow, heaviest
nearest, except that an ingredient made by the sub-row always takes the
LAST belt, so the sub-row output belt can continue straight into it while
the other input belt passes underneath the sub-row.

Ingredient sources (decided by the caller's ``sourced(item)`` predicate):
an item a registered belt port or bus tap can deliver is *sourced*. An item
that is not sourced but has a row-craftable recipe whose ingredients are all
sourced is a *one-level intermediate* and gets the sub-row. At most one
intermediate per line; anything else is a ``ValueError`` naming the item.
"""

from __future__ import annotations

from typing import Any, Callable

from .blocks.audit import audit_row
from .blocks.geometry import bounding_box, footprint, step, xy
from .blocks.row import RowShape, extend_row, row_block
from .blocks.sizing import RowSizing, size_row
from .blocks.stock_tail import audit_stock_tail, stock_tail

MAIN_ORIGIN = (0.5, 0.5)
MARGIN = 1


def _spare(sizing: RowSizing) -> int:
    """``row_block``'s default spare: as many slots again, within the lane budget."""
    return min(sizing.count, sizing.max_count - sizing.count)


def _row_recipe(catalog: Any, item: str) -> str | None:
    recipe = catalog.recipe_for_product(item)
    return recipe.get("name") if isinstance(recipe, dict) else None


def _ingredients(catalog: Any, recipe: str) -> list[str]:
    rows = catalog.recipes[recipe]["ingredients"]
    return list(dict.fromkeys(r["name"] for r in rows if r.get("type", "item") == "item"))


def _lanes(flows: dict[str, float], last: str | None) -> list[tuple[str, ...]]:
    """One item per belt, heaviest flow nearest; ``last`` (sub-row item) on the last belt."""
    order = sorted(flows, key=lambda item: (item == last, -flows[item], item))
    return [(item,) for item in order]


def _assembler_recipe(catalog: Any, recipe: str) -> bool:
    """Solid-only recipe some electric assembling machine can craft (smelting and chemistry are sources)."""
    row = catalog.recipes.get(recipe) or {}
    if any(r.get("type") == "fluid" for r in list(row.get("ingredients", ())) + list(row.get("products", ()))):
        return False
    categories = set(row.get("categories") or ())
    return any(
        e.get("type") == "assembling-machine"
        and e.get("electric")
        and categories & set(e.get("crafting_categories") or ())
        for e in catalog.entities.values()
    )


def _check_width(catalog: Any, recipe: str) -> list[str]:
    items = _ingredients(catalog, recipe)
    if len(items) > 2:
        raise ValueError(
            f"{recipe}: {len(items)} solid ingredients need a lane-merge adapter (routes fill whole belts); "
            "a produced line takes at most two"
        )
    return items


def _size(catalog, recipe, rate, *, inserter, enabled, sub_item=None, machine=None) -> RowSizing:
    items = _check_width(catalog, recipe)
    amounts: dict[str, float] = {}
    for row in catalog.recipes[recipe]["ingredients"]:
        amounts[row["name"]] = amounts.get(row["name"], 0.0) + float(row["amount"])
    return size_row(
        catalog,
        recipe,
        rate,
        machine=machine,
        enabled_recipes=enabled,
        inserter=inserter,
        lanes=_lanes({item: amounts[item] for item in items}, sub_item),
    )


def unsourced_raw(catalog: Any, recipe: str, sourced: Callable[[str], bool]) -> list[str]:
    """Ingredients no source delivers and no assembler can make (plates without a port)."""
    found = []
    for item in _ingredients(catalog, recipe):
        candidate = _row_recipe(catalog, item)
        if not sourced(item) and (candidate is None or not _assembler_recipe(catalog, candidate)):
            found.append(item)
    return found


def plan_sources(catalog: Any, recipe: str, sourced: Callable[[str], bool]) -> dict:
    """``{"sourced": [...], "sub_item": item | None, "sub_recipe": recipe | None}``; ValueError when unbuildable."""
    sub_item, sub_recipe, plain = None, None, []
    for item in _check_width(catalog, recipe):
        if sourced(item):
            plain.append(item)
            continue
        candidate = _row_recipe(catalog, item)
        if candidate is None or sub_item is not None or not _assembler_recipe(catalog, candidate):
            raise ValueError(f"{recipe}: no source for {item} and no single one-level intermediate sub-row")
        missing = [i for i in _check_width(catalog, candidate) if not sourced(i)]
        if missing:
            raise ValueError(
                f"{recipe}: {item} is a deeper intermediate ({candidate} needs {', '.join(missing)}); "
                "request a produce row with a belt output for it first"
            )
        sub_item, sub_recipe = item, candidate
    return {"sourced": plain, "sub_item": sub_item, "sub_recipe": sub_recipe}


def design_line(
    catalog: Any,
    product: str,
    rate: float,
    *,
    inserter: str,
    enabled: list[str],
    sourced: Callable[[str], bool],
    chest: str,
    origin: tuple[float, float] = MAIN_ORIGIN,
) -> dict:
    """Canonical (east-flowing) line at ``origin``; see the module docstring."""
    recipe = _row_recipe(catalog, product)
    if recipe is None:
        raise ValueError(f"no recipe makes {product}")
    plan = plan_sources(catalog, recipe, sourced)
    main_sizing = _size(catalog, recipe, rate, inserter=inserter, enabled=enabled, sub_item=plan["sub_item"])
    spare = _spare(main_sizing)
    sub = None
    if plan["sub_item"]:
        sub_sizing, spare = _sub_sizing(catalog, plan, main_sizing, spare, inserter=inserter, enabled=enabled)
        sub = row_block(
            catalog,
            sub_sizing,
            origin=sub_origin(main_sizing, sub_sizing, origin),
            inserter=inserter,
            spare=0,
        )
    main = row_block(catalog, main_sizing, origin=origin, inserter=inserter, spare=spare)
    tail = stock_tail(catalog, main, inserter=inserter, chest=chest)
    spec = {
        "product": product,
        "recipe": recipe,
        "rate_per_minute": rate,
        "inserter": inserter,
        "chest": chest,
        "sub_item": plan["sub_item"],
        "sub_recipe": plan["sub_recipe"],
    }
    return compose(catalog, spec, main, sub, tail)


def _sub_sizing(catalog, plan: dict, main: RowSizing, spare: int, *, inserter, enabled) -> tuple[RowSizing, int]:
    """Sub-row sized for every main slot (built and spare), so extending the main row
    never needs a bigger sub-row; main spare shrinks until one sub-row carries it."""
    error = None
    for main_spare in range(spare, -1, -1):
        need = main.flow(plan["sub_item"]).per_machine * (main.count + main_spare)
        try:
            return _size(catalog, plan["sub_recipe"], need, inserter=inserter, enabled=enabled), main_spare
        except ValueError as exc:
            error = exc
    raise ValueError(f"{plan['sub_recipe']} sub-row cannot feed the main row: {error}")


def sub_origin(main: RowSizing, sub: RowSizing, origin: tuple[float, float]) -> tuple[float, float]:
    """Sub-row first pole so its output port (no spare slots) sits one tile before
    the main row's last input port."""
    main_shape, sub_shape = RowShape(main.machine_size), RowShape(sub.machine_size)
    belt_v = main_shape.input_belt_v(len(main.input_belts) - 1)
    return (origin[0] - 1 - sub_shape.pitch * sub.count, origin[1] + belt_v - sub_shape.output_belt_v)


def compose(catalog: Any, spec: dict, main: dict, sub: dict | None, tail: dict) -> dict:
    """The line dict: rows, tail, external input ports, the internal joint, the merged entities."""
    joint = None
    inputs = [p for p in main["ports"] if p["direction"] == "input"]
    if sub is not None:
        out = next(p for p in sub["ports"] if p["direction"] == "output")
        target = next(p for p in inputs if p["item"] == spec["sub_item"])
        joint = {"from": out, "to": target}
        inputs = [p for p in inputs if p is not target] + [p for p in sub["ports"] if p["direction"] == "input"]
    entities = list(main["entities"]) + list((sub or {}).get("entities", [])) + list(tail["entities"])
    tiles = set().union(*(footprint(catalog, e) for e in entities))
    for row in (main, sub):
        for slot in (row or {}).get("expansion_slots", []):
            tiles |= footprint(catalog, dict(slot, direction=0))
    return {
        "spec": dict(spec),
        "main": main,
        "sub": sub,
        "tail": tail,
        "inputs": inputs,
        "output": tail["port"],
        "joint": joint,
        "entities": entities,
        "reserved_area": bounding_box(tiles, margin=MARGIN),
        "rated_per_minute": main["rated_per_minute"],
        "stock_rate_per_minute": tail["stock_rate_per_minute"],
        "bar_slots": tail["bar_slots"],
    }


def audit_line(catalog: Any, line: dict) -> list[str]:
    """Every row audit, the tail audit, the joint and whole-line overlaps; ``[]`` = OK."""
    errors = [f"main:{e}" for e in audit_row(catalog, line["main"])]
    if line["sub"] is not None:
        errors += [f"sub:{e}" for e in audit_row(catalog, line["sub"])]
        joint = line["joint"]
        if (
            step(xy(joint["from"]), joint["from"]["facing"]) != xy(joint["to"])
            or joint["from"]["facing"] != joint["to"]["facing"]
            or joint["from"]["item"] != joint["to"]["item"]
        ):
            errors.append("joint_not_straight")
        if line["sub"]["rated_per_minute"] + 1e-9 < _sub_demand(catalog, line):
            errors.append("sub_row_rate_below_main_demand")
    errors += [f"tail:{e}" for e in audit_stock_tail(catalog, line["main"], line["tail"])]
    counts: dict = {}
    for e in line["entities"]:
        for tile in footprint(catalog, e):
            counts[tile] = counts.get(tile, 0) + 1
    errors += [f"line_overlap:{t}" for t, n in counts.items() if n > 1]
    return sorted(set(errors))


def _sub_demand(catalog: Any, line: dict) -> float:
    """Items/minute of the sub-row item the main row consumes at its declared rate."""
    main = line["main"]
    recipe = catalog.recipes[main["recipe"]]
    amount = sum(float(r["amount"]) for r in recipe["ingredients"] if r["name"] == line["spec"]["sub_item"])
    return amount * float(main["crafts_per_machine"]) * main["count"]


def input_demand(catalog: Any, line: dict, item: str) -> float:
    """Items/minute of external input ``item`` the line's built machines consume at declared rates."""
    total = 0.0
    for row in (line["main"], line["sub"]):
        if row is None:
            continue
        recipe = catalog.recipes[row["recipe"]]
        amount = sum(float(r["amount"]) for r in recipe["ingredients"] if r["name"] == item)
        if any(p["direction"] == "input" and p["item"] == item for p in line["inputs"] if p in row["ports"]):
            total += amount * float(row["crafts_per_machine"]) * row["count"]
    return total


def extend_line(catalog: Any, line: dict, extra: int) -> dict:
    """Fill ``extra`` more main-row slots in place; the sub-row was sized for every
    slot already, the tail and every external port stay where they are."""
    main = extend_row(line["main"], catalog, extra)
    extended = compose(catalog, line["spec"], main, line["sub"], line["tail"])
    if extended["inputs"] != line["inputs"] or extended["output"] != line["output"]:
        raise ValueError("line extension would move ports")
    return extended
