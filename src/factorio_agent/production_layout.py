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
fill one lane of a shared belt. So a line takes at most two routed solid
ingredients (near + far belt). Belts are ordered by flow, heaviest
nearest, except that an ingredient made by the sub-row always takes the
LAST belt, so the sub-row output belt can continue straight into it while
the other input belt passes underneath the sub-row.

Twin sub-rows (a third solid ingredient, e.g. engine units: steel + gears +
pipes): two one-level intermediates made from the SAME single sourced input
share one mixed last belt, one item per lane. The sub-row ``P`` drops its
product on the far lane of its output belt, which runs straight on as the
main row's last belt; the twin ``G`` is a sub-row mirrored about its axis,
so it shares ``P``'s input belt (one external input port, one tap) and puts
its product on its own output belt on the far side; that belt turns down in
the free column before the main row and side-loads onto the other lane
(``blocks.belts`` models the side-load; ``audit_line`` checks the lanes)::

    XXXXXXXXXXXXX.v        X: G's output belt (twin item)
    .o...o...o....v        G: mirrored sub-row (output arms north, input arms south)
    +GGG+GGG+GGG+.v
    .i...i...i....v
    @>>>>>>>>>>>..v        shared input belt (one port @), stops one tile early
    .i...i...i....v
    +PPP+PPP+PPP+.v        P: sub-row
    .o...o...o....v
    YYYYYYYYYYYYY>>>>>     Y: P's output (one lane) + the side-load = the main row's last belt
                   @>>>    the sourced ingredient's near belt (port reached from the west)

Ingredient sources (decided by the caller's ``sourced(item)`` predicate):
an item a registered belt port or bus tap can deliver is *sourced*. An item
that is not sourced but has a row-craftable recipe whose ingredients are all
sourced is a *one-level intermediate* and gets the sub-row. At most one
intermediate per line; anything else is a ``ValueError`` naming the item.
"""

from __future__ import annotations

from typing import Any, Callable

from .blocks.audit import audit_row
from .blocks.belts import belt_graph, belt_tiles, spread
from .blocks.geometry import bounding_box, footprint, step, xy
from .blocks.row import RowShape, extend_row, row_block
from .blocks.sizing import RowSizing, size_row
from .blocks.stock_tail import audit_stock_tail, stock_tail
from .board import entity
from .site.transform import Transform
from .site.transform import apply as apply_transform

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


def _width_error(recipe: str, count: int) -> ValueError:
    return ValueError(
        f"{recipe}: {count} solid ingredients need a lane-merge adapter (routes fill whole belts); "
        "a produced line takes at most two, or three when two are in-line intermediates of one shared input"
    )


def _check_width(catalog: Any, recipe: str) -> list[str]:
    items = _ingredients(catalog, recipe)
    if len(items) > 2:
        raise _width_error(recipe, len(items))
    return items


def _amounts(catalog: Any, recipe: str) -> dict[str, float]:
    amounts: dict[str, float] = {}
    for row in catalog.recipes[recipe]["ingredients"]:
        if row.get("type", "item") == "item":
            amounts[row["name"]] = amounts.get(row["name"], 0.0) + float(row["amount"])
    return amounts


def _size(catalog, recipe, rate, *, inserter, enabled, sub_item=None, twin_item=None, machine=None) -> RowSizing:
    """Size ``recipe``; with ``twin_item`` its last belt carries the twin (left) and sub (right) lanes."""
    amounts = _amounts(catalog, recipe)
    if twin_item is None:
        lanes = _lanes({item: amounts[item] for item in _check_width(catalog, recipe)}, sub_item)
    else:
        plain = {item: amounts[item] for item in amounts if item not in (sub_item, twin_item)}
        lanes = _lanes(plain, None) + [(twin_item, sub_item)]
    return size_row(
        catalog,
        recipe,
        rate,
        machine=machine,
        enabled_recipes=enabled,
        inserter=inserter,
        lanes=lanes,
    )


def unsourced_raw(catalog: Any, recipe: str, sourced: Callable[[str], bool]) -> list[str]:
    """Ingredients no source delivers and no assembler can make (plates without a port)."""
    found = []
    for item in _ingredients(catalog, recipe):
        candidate = _row_recipe(catalog, item)
        if not sourced(item) and (candidate is None or not _assembler_recipe(catalog, candidate)):
            found.append(item)
    return found


def _single_input(catalog: Any, item: str, sourced: Callable[[str], bool]) -> tuple[str, str] | None:
    """``(recipe, input)`` when ``item`` is assembler-made from exactly one sourced item."""
    candidate = _row_recipe(catalog, item)
    if candidate is None or not _assembler_recipe(catalog, candidate):
        return None
    inner = _ingredients(catalog, candidate)
    if len(inner) != 1 or not sourced(inner[0]):
        return None
    return candidate, inner[0]


def plan_sources(catalog: Any, recipe: str, sourced: Callable[[str], bool]) -> dict:
    """``{"sourced", "sub_item", "sub_recipe", "twin_item", "twin_recipe"}``; ValueError when unbuildable.

    Two unsourced intermediates made from the same single sourced input become
    twin sub-rows (module doc); a third solid ingredient is accepted only so.
    """
    items = _ingredients(catalog, recipe)
    if len(items) > 3:
        raise _width_error(recipe, len(items))
    unsourced = [item for item in items if not sourced(item)]
    singles = [_single_input(catalog, item, sourced) for item in unsourced]
    if len(unsourced) == 2 and all(singles) and singles[0][1] == singles[1][1]:
        amounts = _amounts(catalog, recipe)
        # The heavier intermediate takes the straight-through lane (sub-row P).
        sub, twin = sorted(unsourced, key=lambda item: (-amounts[item], item))
        return {
            "sourced": [item for item in items if item not in unsourced],
            "sub_item": sub,
            "sub_recipe": _row_recipe(catalog, sub),
            "twin_item": twin,
            "twin_recipe": _row_recipe(catalog, twin),
        }
    if len(items) == 3:
        raise _width_error(recipe, len(items))
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
    return {"sourced": plain, "sub_item": sub_item, "sub_recipe": sub_recipe, "twin_item": None, "twin_recipe": None}


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
    main_sizing = _size(
        catalog,
        recipe,
        rate,
        inserter=inserter,
        enabled=enabled,
        sub_item=plan["sub_item"],
        twin_item=plan["twin_item"],
    )
    spare = _spare(main_sizing)
    sub = twin = merge = None
    if plan["sub_item"]:
        sizings, spare = _sub_sizing(catalog, plan, main_sizing, spare, inserter=inserter, enabled=enabled)
        if plan["twin_item"] is None:
            sub = row_block(
                catalog,
                sizings[0],
                origin=sub_origin(main_sizing, sizings[0], origin),
                inserter=inserter,
                spare=0,
            )
        else:
            sub, twin, merge = twin_rows(catalog, main_sizing, sizings[0], sizings[1], origin, inserter=inserter)
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
        "twin_item": plan["twin_item"],
        "twin_recipe": plan["twin_recipe"],
    }
    return compose(catalog, spec, main, sub, tail, twin=twin, merge=merge)


def _sub_sizing(catalog, plan: dict, main: RowSizing, spare: int, *, inserter, enabled):
    """Sub-row(s) sized for every main slot (built and spare), so extending the main row
    never needs a bigger sub-row; main spare shrinks until the sub-row(s) carry it.
    Returns ``([sub sizing] + [twin sizing], main spare)``."""
    rows = [(plan["sub_item"], plan["sub_recipe"])]
    if plan.get("twin_item"):
        rows.append((plan["twin_item"], plan["twin_recipe"]))
    error = None
    for main_spare in range(spare, -1, -1):
        try:
            sized = [
                _size(
                    catalog,
                    recipe,
                    main.flow(item).per_machine * (main.count + main_spare),
                    inserter=inserter,
                    enabled=enabled,
                )
                for item, recipe in rows
            ]
            return sized, main_spare
        except ValueError as exc:
            error = exc
    raise ValueError(f"{plan['sub_recipe']} sub-row cannot feed the main row: {error}")


def sub_origin(main: RowSizing, sub: RowSizing, origin: tuple[float, float]) -> tuple[float, float]:
    """Sub-row first pole so its output port (no spare slots) sits one tile before
    the main row's last input port."""
    main_shape, sub_shape = RowShape(main.machine_size), RowShape(sub.machine_size)
    belt_v = main_shape.input_belt_v(len(main.input_belts) - 1)
    return (origin[0] - 1 - sub_shape.pitch * sub.count, origin[1] + belt_v - sub_shape.output_belt_v)


def twin_rows(catalog: Any, main: RowSizing, sub: RowSizing, twin: RowSizing, origin, *, inserter) -> tuple:
    """Sub-row P, mirrored twin G and the merge belts (module doc), canonical east flow.

    Both rows end two tiles before the main row (output ports at main
    u = -2); column u = -1 holds G's turn-down belt and the side-load tile,
    so the mixed belt continues straight into the main row's last input
    port. The shared input belt stops at u = -3, so it never feeds the
    turn-down column.
    """
    main_shape, p_shape, g_shape = RowShape(main.machine_size), RowShape(sub.machine_size), RowShape(twin.machine_size)
    ox, oy = origin
    v_mix = main_shape.input_belt_v(len(main.input_belts) - 1)
    p_v = v_mix - p_shape.output_belt_v
    v_in = p_v + p_shape.input_belt_v(0)
    g_v = v_in + g_shape.input_belt_v(0)  # mirrored: G's input belt lies below its axis
    end = ox - 2
    p_origin = (end - p_shape.pitch * sub.count, oy + p_v)
    g_origin = (end - g_shape.pitch * twin.count, oy + g_v)
    p_row = row_block(catalog, sub, origin=p_origin, inserter=inserter, spare=0)
    g_row = apply_transform(
        Transform(0, "y", g_origin), row_block(catalog, twin, origin=g_origin, inserter=inserter, spare=0)
    )
    g_row["mirrored"] = True
    shared_end = (end, oy + v_in)
    for row in (p_row, g_row):
        row["entities"] = [e for e in row["entities"] if not (e["name"] == main.belt and xy(e) == shared_end)]
    g_out = next(p for p in g_row["ports"] if p["direction"] == "output")
    column, mixed_v = ox - 1, oy + v_mix
    count = int(round(mixed_v - 1 - g_out["position"]["y"]))
    belts = [entity(main.belt, column, g_out["position"]["y"] + k, 8) for k in range(count + 1)]
    belts.append(entity(main.belt, column, mixed_v, 4))
    longer = p_row if p_origin[0] <= g_origin[0] else g_row
    shared = next(p for p in longer["ports"] if p["direction"] == "input")
    return p_row, g_row, {"entities": belts, "shared_input": shared}


def _even_turn(area: list[list[float]]) -> list[list[float]]:
    """Grow the box one tile east when its sides differ in parity: a quarter turn about the
    centre of an odd x even box leaves the tile grid, so ``site.fit`` could never turn it."""
    (x0, y0), (x1, y1) = area
    if int(round(x1 - x0 - (y1 - y0))) % 2:
        x1 += 1
    return [[x0, y0], [x1, y1]]


def rows(line: dict) -> list[dict]:
    """Every machine row of a line: main, sub-row, twin (those present)."""
    return [row for row in (line["main"], line.get("sub"), line.get("twin")) if row]


def _unique(entities: list[dict]) -> list[dict]:
    """Drop exact duplicates (the twin rows' shared input belt), keep order."""
    seen, out = set(), []
    for e in entities:
        key = (e["name"], *xy(e), e.get("direction", 0))
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def compose(
    catalog: Any,
    spec: dict,
    main: dict,
    sub: dict | None,
    tail: dict,
    *,
    twin: dict | None = None,
    merge: dict | None = None,
) -> dict:
    """The line dict: rows, tail, external input ports, the internal joint, the merged entities."""
    joint = None
    inputs = [p for p in main["ports"] if p["direction"] == "input"]
    if twin is not None:
        inputs = [p for p in inputs if p["item"] not in (spec["sub_item"], spec["twin_item"])]
        inputs.append(merge["shared_input"])
    elif sub is not None:
        out = next(p for p in sub["ports"] if p["direction"] == "output")
        target = next(p for p in inputs if p["item"] == spec["sub_item"])
        joint = {"from": out, "to": target}
        inputs = [p for p in inputs if p is not target] + [p for p in sub["ports"] if p["direction"] == "input"]
    entities = _unique(
        list(main["entities"])
        + list((sub or {}).get("entities", []))
        + list((twin or {}).get("entities", []))
        + list((merge or {}).get("entities", []))
        + list(tail["entities"])
    )
    tiles = set().union(*(footprint(catalog, e) for e in entities))
    for row in (main, sub, twin):
        for slot in (row or {}).get("expansion_slots", []):
            tiles |= footprint(catalog, dict(slot, direction=0))
    return {
        "spec": dict(spec),
        "main": main,
        "sub": sub,
        "twin": twin,
        "merge": merge,
        "tail": tail,
        "inputs": inputs,
        "output": tail["port"],
        "joint": joint,
        "entities": entities,
        "reserved_area": _even_turn(bounding_box(tiles, margin=MARGIN)),
        "rated_per_minute": main["rated_per_minute"],
        "stock_rate_per_minute": tail["stock_rate_per_minute"],
        "bar_slots": tail["bar_slots"],
    }


def audit_line(catalog: Any, line: dict) -> list[str]:
    """Every row audit, the tail audit, the joint and whole-line overlaps; ``[]`` = OK."""
    errors = [f"main:{e}" for e in audit_row(catalog, line["main"])]
    if line.get("twin") is not None:
        errors += [f"sub:{e}" for e in audit_row(catalog, line["sub"])]
        errors += [f"twin:{e}" for e in audit_row(catalog, line["twin"])]
        errors += _audit_merge(catalog, line)
        for item in (line["spec"]["sub_item"], line["spec"]["twin_item"]):
            row = line["sub"] if item == line["spec"]["sub_item"] else line["twin"]
            if row["rated_per_minute"] + 1e-9 < _sub_demand(catalog, line, item):
                errors.append(f"sub_row_rate_below_main_demand:{item}")
    elif line["sub"] is not None:
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


def _sub_demand(catalog: Any, line: dict, item: str | None = None) -> float:
    """Items/minute of a sub-row item the main row consumes at its declared rate."""
    main = line["main"]
    recipe = catalog.recipes[main["recipe"]]
    item = item or line["spec"]["sub_item"]
    amount = sum(float(r["amount"]) for r in recipe["ingredients"] if r["name"] == item)
    return amount * float(main["crafts_per_machine"]) * main["count"]


def _audit_merge(catalog: Any, line: dict) -> list[str]:
    """Twin lanes: each sub-row product reaches only its declared lane of the main row's last
    belt (through the side-load), and the shared input never reaches a main row port."""
    belts = belt_tiles(catalog, line["entities"])
    graph = belt_graph(belts)
    lanes: dict = {}

    def seed(port, item, sides):
        for side in sides:
            lanes.setdefault((xy(port), side), set()).add(item)

    for row in (line["sub"], line["twin"]):
        out = next(p for p in row["ports"] if p["direction"] == "output")
        seed(out, out["item"], [out["lane"]])
    shared = line["merge"]["shared_input"]
    seed(shared, shared["item"], ("left", "right"))
    for p, (q, _) in graph.items():
        for side in ("left", "right"):
            lanes.setdefault((p, side), set())
            lanes.setdefault((q, side), set())
    spread(lanes, graph)
    errors = []
    for port in line["main"]["ports"]:
        if port["direction"] != "input" or port["item"] not in (line["spec"]["sub_item"], line["spec"]["twin_item"]):
            continue
        got = lanes.get((xy(port), port["lane"]), set())
        if got != {port["item"]}:
            errors.append(f"merge_lane_wrong:{port['item']}:{port['lane']}:{','.join(sorted(got))}")
    for port in line["main"]["ports"]:
        for side in ("left", "right"):
            if shared["item"] in lanes.get((xy(port), side), set()) and port["item"] != shared["item"]:
                errors.append(f"merge_input_leaks:{port['item']}")
    return errors


def input_demand(catalog: Any, line: dict, item: str) -> float:
    """Items/minute of external input ``item`` the line's built machines consume at declared rates."""
    total = 0.0
    if item not in {p["item"] for p in line["inputs"]}:
        return total
    for row in rows(line):
        recipe = catalog.recipes[row["recipe"]]
        amount = sum(float(r["amount"]) for r in recipe["ingredients"] if r["name"] == item)
        # A row's own port for an external item is external (twin rows share one belt and port).
        if not any(p["direction"] == "input" and p["item"] == item for p in row["ports"]):
            continue
        crafts = float(row["crafts_per_machine"]) * row["count"]
        if row is not line["main"]:
            # A sub-row only runs as fast as the built main row takes its product.
            product = next(p["item"] for p in row["ports"] if p["direction"] == "output")
            made = sum(float(r["amount"]) for r in recipe["products"] if r["name"] == product) or 1.0
            crafts = min(crafts, _sub_demand(catalog, line, product) / made)
        total += amount * crafts
    # A stock row stops when its chest is full: it averages its requested rate, not its nameplate.
    requested = (line.get("spec") or {}).get("rate_per_minute")
    rated = line.get("rated_per_minute")
    if isinstance(requested, (int, float)) and isinstance(rated, (int, float)) and rated > requested > 0:
        total *= requested / rated
    return total


def extend_line(catalog: Any, line: dict, extra: int) -> dict:
    """Fill ``extra`` more main-row slots in place; the sub-row was sized for every
    slot already, the tail and every external port stay where they are."""
    main = extend_row(line["main"], catalog, extra)
    extended = compose(
        catalog, line["spec"], main, line["sub"], line["tail"], twin=line.get("twin"), merge=line.get("merge")
    )
    if extended["inputs"] != line["inputs"] or extended["output"] != line["output"]:
        raise ValueError("line extension would move ports")
    return extended
