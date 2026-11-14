"""The blocks a base will need, for the planned floor (``site.floorplan``).

``floor_blocks(catalog, lane_order)`` lists every district block in roadmap order
(``progression.ROADMAP``), then the rocket bill's further blocks (``rocket_plan.rocket_bill``:
fluid products, the rocket silo, and the produce rows a rocket stage consumes), each with:

* the allocation base names its controller asks the ``SiteService`` for (``CAPABILITY_BLOCKS``;
  a produce row ``production_row_<item>`` and its belt-output row ``..__belt`` share one frame);
* inputs, outputs and the privately routed inputs (an item that is not on a bus lane, or one the
  controller routes from its supplier's outlet: advanced circuits take plastic from the plastic
  cell);
* the real footprint where the block generator is a pure function of the catalog
  (``block_sites.footprint`` of the advanced circuit, chemical science, refinery and chemical
  cores; the planned furnace bank's ``LENGTH`` x ``BANK_DEPTH``; the silo ring), else the
  footprint the controller requested on eval-1575138435 (``FALLBACK_SIZES``);
* a growth allowance (``GROWTH``): tiles the frame holds beyond the built length (owner rule:
  reserve the full block, build two units first, grow in place).

Pure and read-only; any failure returns the roadmap blocks with fallback sizes, never raises.
"""

from __future__ import annotations

from .site.floorplan import FloorBlock

# capability (+ product) -> (names, kind, inputs, private, outputs)
CAPABILITY_BLOCKS = {
    "automation_science": (
        ("automation_science",),
        "science",
        ("iron-plate", "copper-plate"),
        (),
        ("automation-science-pack",),
    ),
    "logistic_science": (
        ("logistic_science", "logistic_science_split"),
        "science",
        ("iron-plate", "copper-plate"),
        (),
        ("logistic-science-pack",),
    ),
    "steel_production": (("steel_production",), "production", ("iron-plate", "coal"), (), ("steel-plate",)),
    "brick_production": (("brick_production",), "production", ("stone", "coal"), (), ("stone-brick",)),
    "ammo_production": (
        ("production_row_firearm-magazine",),
        "production",
        ("iron-plate",),
        (),
        ("firearm-magazine",),
    ),
    "basic_petroleum": (("basic_petroleum",), "production", ("crude-oil",), (), ("petroleum-gas",)),
    "engine_production": (
        ("production_row_engine-unit", "production_row_engine-unit__belt"),
        "production",
        ("iron-plate", "steel-plate"),
        (),
        ("engine-unit",),
    ),
    "chemical_production:plastic-bar": (
        ("chemical_production_plastic-bar",),
        "production",
        ("petroleum-gas", "coal"),
        ("petroleum-gas",),
        ("plastic-bar",),
    ),
    "advanced_circuit_production": (
        ("advanced_circuit_production",),
        "production",
        ("iron-plate", "copper-plate", "plastic-bar"),
        ("plastic-bar",),
        ("advanced-circuit",),
    ),
    "chemical_production:sulfur": (
        ("chemical_production_sulfur",),
        "production",
        ("petroleum-gas", "water"),
        ("petroleum-gas",),
        ("sulfur",),
    ),
    "chemical_science": (
        ("chemical_science_production",),
        "science",
        ("engine-unit", "advanced-circuit", "sulfur"),
        ("engine-unit", "advanced-circuit", "sulfur"),
        ("chemical-science-pack",),
    ),
}

# along x depth requested on eval-1575138435 (site-plan.json allocations), used when no generator runs.
FALLBACK_SIZES = {
    "automation_science": (32, 32),
    "logistic_science": (16, 46),
    "steel_production": (30, 12),
    "brick_production": (30, 12),
    "production_row_firearm-magazine": (9, 9),
    "basic_petroleum": (30, 12),
    "production_row_engine-unit": (28, 22),
    "chemical_production_plastic-bar": (18, 11),
    "chemical_production_sulfur": (18, 11),
    "advanced_circuit_production": (31, 22),
    "chemical_science_production": (31, 22),
    "rocket_silo": (13, 13),
    "advanced-petroleum": (30, 16),
}
DEFAULT_ROW = (22, 22)  # a generic produce row
DEFAULT_CELL = (18, 11)  # a fluid product cell

# Frame tiles held beyond the built length (along the bus). Most footprints already are the full
# block: a produce row reserves its slots along its belts (``blocks/row.extend_row``), the cores
# keep ``growth_slots`` inside their reserved area, the furnace bank reserves ``SLOTS`` per row and
# science blocks their ``science_growth`` frame, so their allowance is 0. A produce row that a
# rocket stage or a block consumes from a belt gets the room for its belt-output row (``__belt``,
# ``floorsim.BELT_ROW_ALONG`` + the corridor gap) beside it in the same frame.
BELT_ROW_ROOM = 18
GROWTH = {
    "production_row_engine-unit": BELT_ROW_ROOM,
}
DEFAULT_GROWTH = 0

STAGES = frozenset({"rocket_parts", "rocket_silo"})
BILL_KIND = "rocket"  # kind of a block that only the rocket bill (not the roadmap) asks for


def _key(step) -> str:
    product = (step.parameters or {}).get("product")
    return f"{step.capability}:{product}" if product else step.capability


def _real_size(catalog, name: str):
    """``(along, depth)`` from the block generator, or None."""
    if catalog is None:
        return None
    from .block_sites import footprint

    try:
        if name == "advanced_circuit_production":
            from .advanced_circuits import advanced_circuit_core

            w, h, _dx, _dy = footprint(lambda x, y: advanced_circuit_core(catalog, x, y, 2))
        elif name == "chemical_science_production":
            from .chemical_science import chemical_science_core

            w, h, _dx, _dy = footprint(lambda x, y: chemical_science_core(catalog, x, y, 2))
        elif name == "basic_petroleum":
            from .petroleum import refinery_core

            w, h, _dx, _dy = footprint(lambda x, y: refinery_core(catalog, x, y))
        elif name.startswith("chemical_production_"):
            from .chemical_production import chemical_core

            product = name[len("chemical_production_") :]
            w, h, _dx, _dy = footprint(lambda x, y: chemical_core(catalog, product, x, y))
        elif name in ("steel_production", "brick_production"):
            from .planned_furnaces import LENGTH
            from .smelting import BANK_DEPTH

            w, h = LENGTH, BANK_DEPTH
        elif name == "rocket_silo":
            from .rocket_stages import RING, SILO_TILES

            w = h = SILO_TILES + 2 * RING
        else:
            return None
    except Exception:  # noqa: BLE001 - a generator that needs more than the catalog falls back
        return None
    return int(w), int(h)


def _size(catalog, name: str, default) -> tuple[int, int]:
    return _real_size(catalog, name) or FALLBACK_SIZES.get(name) or default


def _block(catalog, names, kind, inputs, private, outputs, rank, default=DEFAULT_ROW, growth=None) -> FloorBlock:
    along, depth = _size(catalog, names[0], default)
    return FloorBlock(
        names=tuple(names),
        kind=kind,
        along=along,
        depth=depth,
        growth=GROWTH.get(names[0], DEFAULT_GROWTH) if growth is None else growth,
        inputs=tuple(inputs),
        private=tuple(private),
        outputs=tuple(outputs),
        rank=rank,
    )


# Produce rows a run asks for right after the bus (construction stock: eval-1575138435 placed them at
# the bus head before automation science), at the rank of the roadmap step named here.
EARLY_ROWS = (
    ("main_bus", ("production_row_inserter",), (22, 10), ("iron-plate", "copper-plate"), ("inserter",)),
    ("main_bus", ("production_row_transport-belt",), (11, 9), ("iron-plate",), ("transport-belt",)),
)


def roadmap_blocks(catalog=None, steps=None) -> list[FloorBlock]:
    """Blocks of the roadmap's district capabilities, in roadmap order."""
    if steps is None:
        from .progression import ROADMAP as steps
    found, seen = [], set()
    ids = {step.id: rank for rank, step in enumerate(steps)}
    for step_id, names, size, inputs, outputs in EARLY_ROWS:
        if step_id in ids:
            seen.add(names)
            found.append(
                FloorBlock(
                    names,
                    "production",
                    size[0],
                    size[1],
                    GROWTH.get(names[0], DEFAULT_GROWTH),
                    inputs,
                    (),
                    outputs,
                    ids[step_id],
                )
            )
    for rank, step in enumerate(steps):
        spec = CAPABILITY_BLOCKS.get(_key(step))
        if spec is None or spec[0] in seen:
            continue
        seen.add(spec[0])
        found.append(_block(catalog, *spec, rank))
    return found


def _bill_blocks(catalog, bill, known: dict[str, FloorBlock], lane_items: set[str], rank0: int) -> list[FloorBlock]:
    """Rocket-bill blocks the roadmap does not list (module doc)."""
    rows = list(bill.get("items") or ())
    by_recipe = {r.get("recipe"): r for r in rows if r.get("recipe")}
    producer_of = {}
    for block in known.values():
        for item in block.outputs:
            producer_of.setdefault(item, block.name)
    wanted = []
    for row in rows:
        producer = row.get("producer") or {}
        capability, kind = producer.get("capability"), producer.get("kind")
        item = row.get("item")
        if kind == "block" and capability == "fluid_product":
            wanted.append((("fluid_product_" + item,), "production", [row], DEFAULT_CELL))
        elif kind == "block" and capability == "advanced_petroleum":
            if not any(w[0] == ("advanced-petroleum",) for w in wanted):
                wanted.append((("advanced-petroleum",), "production", [], FALLBACK_SIZES["advanced-petroleum"]))
            wanted[[w[0] for w in wanted].index(("advanced-petroleum",))][2].append(row)
        elif kind == "generic" and capability == "produce":
            consumers = [by_recipe.get(c) for c in row.get("consumers") or ()]
            if any(c and (c.get("producer") or {}).get("capability") in STAGES for c in consumers):
                wanted.append(
                    ((f"production_row_{item}", f"production_row_{item}__belt"), "production", [row], DEFAULT_ROW)
                )
        elif kind == "stage" and capability in STAGES:
            if not any(w[0] == ("rocket_silo",) for w in wanted):
                wanted.append((("rocket_silo",), "production", [], FALLBACK_SIZES["rocket_silo"]))
            wanted[[w[0] for w in wanted].index(("rocket_silo",))][2].append(row)
    for names, _kind, members, _size in wanted:
        for row in members:
            if row.get("item") and row["item"] not in ("rocket-part",):
                producer_of.setdefault(row["item"], names[0])
    found = []
    for offset, (names, _kind, members, default) in enumerate(wanted):
        if names[0] in known:
            continue
        recipes = {r.get("recipe") for r in members if r.get("recipe")}
        inputs = sorted({r["item"] for r in rows if set(r.get("consumers") or ()) & recipes and r.get("item")})
        outputs = sorted({r["item"] for r in members if r.get("item") and r["item"] != "rocket-part"})
        private = [i for i in inputs if i not in lane_items and producer_of.get(i) not in (None, names[0])]
        growth = BELT_ROW_ROOM if len(names) > 1 and names[1].endswith("__belt") else None
        found.append(_block(catalog, names, BILL_KIND, inputs, private, outputs, rank0 + offset, default, growth))
    return found


def floor_blocks(catalog=None, lane_order=(), *, bill=None) -> tuple[FloorBlock, ...]:
    """Roadmap blocks, then the rocket bill's further blocks (module doc)."""
    blocks = roadmap_blocks(catalog)
    if bill is None and catalog is not None:
        try:
            from .rocket_plan import rocket_bill

            bill = rocket_bill(catalog, researched=())
        except Exception:  # noqa: BLE001 - a catalog without a rocket bill plans the roadmap blocks only
            bill = None
    if isinstance(bill, dict):
        known = {b.name: b for b in blocks}
        rank0 = max((b.rank for b in blocks), default=0) + 1
        try:
            blocks += _bill_blocks(catalog, bill, known, set(lane_order), rank0)
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    return tuple(blocks)
