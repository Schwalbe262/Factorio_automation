"""Normally supplied PCB smelting district; actor never feeds these furnaces."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import built_keys, entity, hardware_conflicts, planned_unbuilt_hardware
from .power_grid import plan_connection as power_connection
from .transport import plan_connection as belt_connection, underground_proof, xy
from .planning import preflight
from .site.transform import apply as apply_transform
from .smelting_site import (
    DISTRICT_SIZE,
    canonical_bank,
    corridor_guide,
    district_candidate,
    district_head_choice,
    plan_candidates,
)

FURNACE_NAMES = frozenset({"stone-furnace", "steel-furnace", "electric-furnace"})


def smelting_site_anchors(sources):
    """Keep accepted legacy ordering, then search a bounded current-source grid."""
    old = ((24, -48), (-24, -48), (24, -64), (-24, -32), (32, -48), (-40, -32), (8, -16), (-40, -48), (24, -24))
    points = [p["position"] for p in sources.values()]
    bx = math.floor(sum(p["x"] for p in points) / len(points) / 16) * 16
    by = math.floor((sum(p["y"] for p in points) / len(points) - 32) / 16) * 16
    offsets = (
        (0, 0),
        (32, 0),
        (-32, 0),
        (0, -32),
        (32, -32),
        (-32, -32),
        (0, 32),
        (32, 32),
        (-32, 32),
        (64, 0),
        (-64, 0),
        (0, -64),
        (0, 64),
        (64, -32),
        (-64, -32),
        (32, -64),
    )
    return tuple(dict.fromkeys((*old, *((bx + dx, by + dy) for dx, dy in offsets))))


def port(item, x, y, facing, direction="input"):
    return {"kind": "item", "item": item, "position": {"x": x, "y": y}, "facing": facing, "direction": direction}


def validate_capacity(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"resource", "rate_per_minute"}:
        raise ValueError("smelting_capacity requires resource and rate_per_minute")
    if parameters["resource"] not in ("iron-ore", "copper-ore"):
        raise ValueError("only iron/copper plate rows can expand here")
    rate = parameters["rate_per_minute"]
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 450:
        raise ValueError("smelting rate must be finite in (0,450]")


def furnace_bank(x, y, count, resource, *, furnace_name="stone-furnace"):
    """Separate ore/coal lanes side-load a straight shared furnace input belt."""
    product = {"iron-ore": "iron-plate", "copper-ore": "copper-plate"}[resource]
    es = []
    for i in range(count):
        dx = 4 * i
        es.extend(
            [
                entity(furnace_name, x + dx, y),
                entity("inserter", x + dx + 0.5, y - 1.5, 0),
                entity("inserter", x + dx + 0.5, y + 1.5, 0),
                entity("small-electric-pole", x + dx + 1.5, y + 0.5),
            ]
        )
    # The empty west stub makes the coal junction a straight belt, so the
    # south feed side-loads one lane instead of turning both lanes around a bend.
    es += [entity("transport-belt", x + dx + 0.5, y - 2.5, 4) for dx in range(-4, 4 * (count - 1) + 1)]
    # Keep the output at the six-slot block boundary as machines are added.
    es += [entity("transport-belt", x + dx + 0.5, y + 2.5, 4) for dx in range(23)]
    es += [
        entity("transport-belt", x - 1.5, y - 4.5, 8),
        entity("transport-belt", x - 1.5, y - 3.5, 8),
        entity("transport-belt", x - 2.5, y - 0.5, 0),
        entity("transport-belt", x - 2.5, y - 1.5, 0),
    ]
    ports = [
        port(resource, x - 1.5, y - 4.5, 8),
        port("coal", x - 2.5, y - 0.5, 0),
        port(product, x + 22.5, y + 2.5, 4, "output"),
    ]
    return _plan(
        es, ports, template="dual_lane_furnace_row", resource=resource, product=product, count=count, capacity_slots=6
    )


BANK_SLOTS = 12  # furnace slots per row of a paired district bank
SLOT_PITCH = 3  # furnace (2 tiles) + pole column
BANK_LENGTH = 48  # tiles from the ore entry edge to the plate port edge
BANK_DEPTH = 12  # tile rows of one bank (ore entry row .. lower input belt)
RESERVED_TILE = "reserved-tile"  # 1x1 routing keepout, never built


def _slot_entities(x0, y0, slot, row, furnace_name):
    """Furnace, input/output inserters, pole and input belts of one slot (see :func:`paired_furnace_bank`)."""
    cx = x0 + 9 + SLOT_PITCH * slot
    if row == "right":  # upper row, drops onto the right lane of the eastbound output
        fy, inserter_y, output_y, pole_y, belt_y, direction = y0 + 4, y0 + 2.5, y0 + 5.5, y0 + 4.5, y0 + 1.5, 0
    else:  # lower row, drops onto the left lane
        fy, inserter_y, output_y, pole_y, belt_y, direction = y0 + 9, y0 + 10.5, y0 + 7.5, y0 + 8.5, y0 + 11.5, 8
    belts = [cx - 0.5, cx + 0.5] if slot == 0 else [cx - 1.5, cx - 0.5, cx + 0.5]
    return [
        entity(furnace_name, cx, fy),
        entity("inserter", cx + 0.5, inserter_y, direction),
        entity("inserter", cx + 0.5, output_y, direction),
        entity("small-electric-pole", cx + 1.5, pole_y),
        *(entity("transport-belt", bx, belt_y, 4) for bx in belts),
    ]


def paired_furnace_bank(
    x0, y0, per_row, resource, *, slots=BANK_SLOTS, length=BANK_LENGTH, furnace_name="stone-furnace", product=None
):
    """Two furnace rows around one shared eastbound output, ``slots`` furnace slots each.

    Canonical frame, band ``[x0, x0 + length) x [y0, y0 + 12)``::

        row 0   ore enters on the west edge (port, facing east) and side-loads the
        row 1   mixed belt: stub, coal side-load from the south, splitter, upper input row
        row 2   upper input inserters                    (splitter lower output turns south)
        row 3-4 upper furnaces, poles in the pole column (vertical feed at x0 + 7.5)
        row 5   upper output inserters
        row 6   output belt from x0 + 9.5 to the east edge (plate port, both lanes)
        row 7   lower output inserters
        row 8-9 lower furnaces and poles
        row 10  lower input inserters
        row 11  lower input row (fed by the vertical belt, lanes kept by two curves)

    Slot ``i`` sits at ``x0 + 9 + 3 i``; the last slot ends four tiles before the
    port so the bus feed splitter and branch fit beside it. ``per_row`` slots
    of both rows are built; the others are the bank's reserved growth
    (``growth_slots``/``growth_entities``, inside ``growth_area``), added in
    place by ``SmeltingDistrict.capacity_step`` without rerouting anything.

    ``product`` defaults to the ore's plate; another single-input furnace
    recipe (iron plate -> steel, stone -> brick: ``planned_furnaces``) uses
    the same bank with ``resource`` as its raw input item.
    """
    product = product or {"iron-ore": "iron-plate", "copper-ore": "copper-plate"}[resource]
    if not 1 <= per_row <= slots or 9 + SLOT_PITCH * (slots - 1) + 2 > length - 4:
        raise ValueError("paired bank slots do not fit its length")
    fixed = [entity("transport-belt", x0 + dx + 0.5, y0 + 0.5, 4) for dx in range(4)]
    fixed += [
        entity("transport-belt", x0 + 4.5, y0 + 0.5, 8),
        # The empty west stub keeps the coal junction straight, so coal side-loads one lane.
        entity("transport-belt", x0 + 2.5, y0 + 1.5, 4),
        entity("transport-belt", x0 + 3.5, y0 + 1.5, 4),
        entity("transport-belt", x0 + 4.5, y0 + 1.5, 4),
        entity("transport-belt", x0 + 3.5, y0 + 2.5, 0),
        entity("transport-belt", x0 + 3.5, y0 + 3.5, 0),
        dict(entity("splitter", x0 + 5.5, y0 + 2, 4), _width=2, _height=1),
        entity("transport-belt", x0 + 6.5, y0 + 1.5, 4),
        entity("transport-belt", x0 + 6.5, y0 + 2.5, 4),
        entity("transport-belt", x0 + 7.5, y0 + 1.5, 4),
        *(entity("transport-belt", x0 + 7.5, y0 + dy + 0.5, 8) for dy in range(2, 11)),
        entity("transport-belt", x0 + 7.5, y0 + 11.5, 4),
    ]
    fixed += [entity("transport-belt", x0 + dx + 0.5, y0 + 6.5, 4) for dx in range(9, length)]
    built, growth, rows = [], [], {"right": [], "left": []}
    for row in ("right", "left"):
        for slot in range(slots):
            es = _slot_entities(x0, y0, slot, row, furnace_name)
            if slot < per_row:
                built += es
                rows[row].append(es[0])
            else:
                growth.append({"row": row, "slot": slot, "entities": es})
    feed = [
        {"x": e["position"]["x"], "y": e["position"]["y"]}
        for row in ("right", "left")
        for slot in range(slots)
        for e in _slot_entities(x0, y0, slot, row, furnace_name)
        if e["name"] == "transport-belt"
    ]
    ports = [
        port(resource, x0 + 0.5, y0 + 0.5, 4),
        port("coal", x0 + 3.5, y0 + 3.5, 0),
        dict(port(product, x0 + length - 0.5, y0 + 6.5, 4, "output"), lane_items={"left": product, "right": product}),
    ]
    return _plan(
        built + fixed,
        ports,
        template="paired_furnace_bank",
        layout_policy="pcb-independent-producers-per-lane-v1",
        resource=resource,
        product=product,
        count=2 * per_row,
        per_side_count=per_row,
        capacity_slots=slots,
        producer_rows=rows,
        feed_belts=feed,
        growth_area=[[x0 + 8, y0 + 1], [x0 + 9 + SLOT_PITCH * (slots - 1) + 2, y0 + 12]],
        growth_slots=growth,
        growth_entities=[e for g in growth for e in g["entities"]],
    )


def growth_tiles(builder, banks):
    """Tiles of every bank's ``growth_area`` its built entities do not occupy (reserved for growth)."""
    tiles = set()
    for bank in banks:
        area = bank.get("growth_area")
        if not area:
            continue
        (x0, y0), (x1, y1) = area
        built = builder._occupied_by_plan(bank["entities"])
        tiles.update(
            (x + 0.5, y + 0.5)
            for x in range(math.floor(x0), math.ceil(x1))
            for y in range(math.floor(y0), math.ceil(y1))
            if (x + 0.5, y + 0.5) not in built
        )
    return tiles


def growth_keepouts(builder, banks):
    """1x1 non-belt keepout specs on every growth tile, for ``reserved`` of belt and pole planners.

    Belt routing (``transport.plan_connection``) treats a reserved non-belt
    as hard, the pole planner (``power_grid.plan_connection``) removes its
    tile from the legal points; neither is ever built or preflighted.
    """
    return [entity(RESERVED_TILE, x, y) for x, y in sorted(growth_tiles(builder, banks))]


def grown_bank(bank, per_row):
    """``bank`` with every reserved slot below ``per_row`` built in both rows (pure).

    Only entities of ``growth_slots`` move into ``entities``; ports, the
    output belt and the feed stay as they are. A furnace keeps the bank's
    ``furnace_name`` when it has one.
    """
    if not 1 <= per_row <= bank["capacity_slots"]:
        raise ValueError("per-row furnace count outside the bank's reserved slots")
    if per_row <= bank["per_side_count"]:
        return copy.deepcopy(bank)
    furnace = bank.get("furnace_name")
    entities = copy.deepcopy(bank["entities"])
    rows = copy.deepcopy(bank["producer_rows"])
    kept = []
    for slot in sorted(bank["growth_slots"], key=lambda g: (g["row"], g["slot"])):
        if slot["slot"] >= per_row:
            kept.append(slot)
            continue
        added = copy.deepcopy(slot["entities"])
        if furnace:
            added[0]["name"] = furnace
        entities += added
        rows[slot["row"]].append(added[0])
    extra = {k: v for k, v in bank.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
    extra.update(
        count=2 * per_row,
        per_side_count=per_row,
        producer_rows=rows,
        growth_slots=kept,
        growth_entities=[e for g in kept for e in g["entities"]],
    )
    return _plan(entities, bank["ports"], **extra)


def merge_entities(*groups):
    result = {}
    for group in groups:
        for e in group:
            key = e["name"], *xy(e)
            previous = result.get(key)
            if previous and any(previous.get(k) != e.get(k) for k in ("direction", "belt_to_ground_type", "recipe")):
                raise ValueError("conflicting shared port configuration")
            result[key] = e
    return list(result.values())


# Build order of the district banks: the plate the construction bill needs most first.
# Belts, gears, inserters and splitters, the bulk of every early block, are iron.
PRODUCER_FIRST = ("iron-ore", "copper-ore")


def producer_first_order(entities, site, connections, routes, powers):
    """The district plan in producer-first build order (same entities, same values).

    The plan used to list both banks, then every route, then the bank power, so
    no furnace smelted before the last entity stood: on eval-4177156614 the
    district (455 entities, ~700 plates) was built for 12 minutes from starter
    furnace plates while the finished iron outpost's ore waited at the end of an
    unbuilt route. Now each bank comes complete with what makes it run: the first
    bank (PRODUCER_FIRST), the shared coal network and the coal source route, then
    that bank's coal branch, ore route and power; then the next bank. Its output
    port starts carrying plates (a public construction source until the bus feed is
    spliced) while the rest is still being built.
    """
    banks = site["banks"]
    count = len(banks)
    if len(routes) != len(connections) or len(connections) != 2 * count + 1 or len(powers) != count:
        return entities
    rank = {resource: i for i, resource in enumerate(PRODUCER_FIRST)}
    order = sorted(range(count), key=lambda i: (rank.get(banks[i].get("resource"), len(rank)), i))
    in_banks = {(e["name"], *xy(e)) for bank in banks for e in bank["entities"]}
    groups = []
    for step, i in enumerate(order):
        groups.append(banks[i]["entities"])
        if step == 0:
            groups.append([e for e in site["entities"] if (e["name"], *xy(e)) not in in_banks])
            groups.append(routes[2 * count]["entities"])  # coal source -> district coal input
        groups.extend((routes[i]["entities"], routes[count + i]["entities"], powers[i]))
    groups.append(entities)  # anything else keeps its place at the end
    values = {(e["name"], *xy(e)): e for e in entities}
    ordered, seen = [], set()
    for group in groups:
        for e in group:
            key = (e["name"], *xy(e))
            if key in values and key not in seen:
                seen.add(key)
                ordered.append(values[key])
    return ordered if len(ordered) == len(entities) else entities


def bank_capacities(catalog, banks):
    """Derive planned product/minute from unique furnace entities, never row counts.

    These are catalog nameplate capacities, independent of current ore, fuel,
    power or downstream load. Rates retain the resource keys used by the state.
    """
    supported = {"stone-furnace", "steel-furnace", "electric-furnace"}
    rates, seen = {}, {}
    for bank in banks:
        resource, product = bank["resource"], bank["product"]
        recipe = catalog.recipes.get(product)
        if not recipe:
            raise ValueError("missing smelting product recipe: " + product)
        duration = recipe.get("energy", 0)
        products = [p for p in recipe.get("products", []) if p["name"] == product]
        if (
            type(duration) not in (int, float)
            or not math.isfinite(duration)
            or duration <= 0
            or any(
                p.get("probability", 1) != 1
                or p.get("independent_probability", 1) != 1
                or type(p.get("amount")) not in (int, float)
                for p in products
            )
        ):
            raise ValueError("deterministic smelting recipe required: " + product)
        amount = sum(p["amount"] for p in products)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("positive smelting product yield required: " + product)
        rates.setdefault(resource, 0.0)
        found = False
        for furnace in bank["entities"]:
            name = furnace["name"]
            prototype = catalog.entities.get(name, {})
            if name not in supported:
                if prototype.get("type") == "furnace":
                    raise ValueError("unsupported planned furnace: " + name)
                continue
            found = True
            key = name, *xy(furnace)
            owner = resource, product
            if key in seen:
                if seen[key] != owner:
                    raise ValueError("one furnace assigned to multiple smelting products")
                continue
            speed = prototype.get("crafting_speed", 0)
            if type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0:
                raise ValueError("missing furnace crafting speed: " + name)
            seen[key] = owner
            rates[resource] += speed * 60 / duration * amount
        if not found:
            raise ValueError("smelting bank has no supported furnace entities: " + resource)
    return rates


def guided(guide):
    """Keyword arguments for ``belt_connection``: a corridor guide only when there is one."""
    return {} if guide is None else {"guide": guide}


def feed_lane_positions(bank):
    """Map positions of the bank's shared furnace input belt (east of the ore junction).

    The canonical rule (same row as the first entity minus 2.5, from its x +
    0.5 on) is applied in the bank's canonical frame, so a turned or mirrored
    bank selects the same belts. A paired bank lists its input rows in
    ``feed_belts`` (map positions); only the built ones are returned.
    """
    if "feed_belts" in bank:
        built = {xy(e) for e in bank["entities"] if e["name"] == "transport-belt"}
        return [dict(p) for p in bank["feed_belts"] if (p["x"], p["y"]) in built]
    canonical, _turn = canonical_bank(bank)
    first = canonical["entities"][0]["position"]
    picked = []
    for e, original in zip(canonical["entities"], bank["entities"]):
        p = e["position"]
        if e["name"] == "transport-belt" and p["y"] == first["y"] - 2.5 and p["x"] >= first["x"] + 0.5:
            picked.append(original["position"])
    return picked


def native_feed_lanes(game, banks) -> dict:
    """One query: item counts on both lanes of every bank's built shared input belts.

    ``{"ok": True, "banks": [{"resource", "lanes": [left, right]}]}``; a bank's
    ``resource`` is its raw input item (ore, iron plate for steel, stone for bricks).
    """
    payload = json.dumps(
        json.dumps(
            [{"resource": b["resource"], "positions": feed_lane_positions(b)} for b in banks],
            separators=(",", ":"),
        )
    )
    return game.query(
        """
local banks=helpers.json_to_table("""
        + payload
        + """);local result={}
for _,bank in ipairs(banks) do
 local counts={{},{}}
 for _,p in ipairs(bank.positions) do
  local belt=target(p,"transport-belt");if not belt then return {ok=false,reason="feed_belt_missing"} end
  for lane=1,2 do for _,item in pairs(belt.get_transport_line(lane).get_contents()) do
   counts[lane][item.name]=(counts[lane][item.name] or 0)+item.count
  end end
 end
 result[#result+1]={resource=bank.resource,lanes=counts}
end
return {ok=true,banks=result}
"""
    )


def feed_lanes_ok(lanes) -> bool:
    """Every bank's input belt carries one item per lane: its raw item on one, coal on the other."""
    if not isinstance(lanes, dict) or not lanes.get("ok"):
        return False
    for row in lanes["banks"]:
        kinds = [set(lane) for lane in row["lanes"]]
        if any(len(k) != 1 for k in kinds) or kinds[0] & kinds[1] or set.union(*kinds) != {row["resource"], "coal"}:
            return False
    return True


# District sources: (resource, nominal ore/min the bank must offer).
DISTRICT_SOURCES = (("iron-ore", 60), ("copper-ore", 30), ("coal", 30))
SEARCH_INTERRUPTIONS_PER_SITE = 2  # budget cut-offs before one candidate counts as failed
SEARCH_FAILURES_KEPT = 12  # failure records the resumable sweep carries between passes


def _site_key(site):
    """A stable identity of one district candidate for the resumable sweep."""
    import hashlib

    body = json.dumps(
        [site["area"], site.get("site_transform"), [(e["name"], *xy(e)) for e in site["entities"]]],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def district_sources(mining):
    """``({resource: output port}, None)`` from saved mining banks, or ``(None, reason)``.

    Per resource the first ready bank with enough nominal rate, else the first
    saved (planned, maybe unbuilt) one: a saved bank's port is final (its exit
    side was chosen before saving; a bridge re-plan keeps the ports).
    """
    sources = {}
    banks = [b for b in mining.get("banks", []) if isinstance(b, dict) and isinstance(b.get("plan"), dict)]
    for resource, rate in DISTRICT_SOURCES:
        fitting = [
            b
            for b in banks
            if b["plan"].get("resource") == resource
            and b.get("nominal_rate_per_minute", 0) >= rate
            and b["plan"].get("ports")
            and (b.get("ready") or b.get("ok"))
        ]
        bank = next((b for b in fitting if b.get("ready")), fitting[0] if fitting else None)
        if not bank:
            return None, f"one_ready_{resource}_bank_with_nominal_{rate}_required"
        sources[resource] = bank["plan"]["ports"][0]
    return sources, None


def _bank_for(mining, port):
    return next((b for b in mining.get("banks", []) if (b.get("plan", {}).get("ports") or [None])[0] == port), {})


def _foreign_unbuilt_conflicts(builder, obs, blocks, entities):
    """Plan entities on a tile of another block's planned-but-unbuilt hardware, unless it is that same entity."""
    planned = planned_unbuilt_hardware(builder, obs, blocks, exclude=("smelting_district",))
    same = {(e["name"], e["position"]["x"], e["position"]["y"]) for e in entities}
    conflicts = []
    for row in hardware_conflicts(builder, entities, planned):
        owner = planned[row["tile"]]
        if (owner["name"], owner["position"]["x"], owner["position"]["y"]) not in same:
            conflicts.append(row)
    return conflicts


class SmeltingDistrict:
    def __init__(self, game, bootstrap, builder, catalog, site=None):
        self.game, self.bootstrap, self.builder, self.catalog = game, bootstrap, builder, catalog
        self.site = site
        self.path = Path(game.cfg.runtime_dir) / "smelting-district.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _master_plan(self, obs):
        """The shared master plan for the observed world, or None (no service, survey failure)."""
        if self.site is None:
            return None
        try:
            return self.site.plan(obs["world_id"])
        except Exception:  # noqa: BLE001 - the plan is preferred, never a new failure mode
            return None

    def owns_furnace(self, e, obs):
        if self.state.get("world_id") != obs["world_id"] or e["name"] not in FURNACE_NAMES:
            return False
        return any(
            w["name"] in FURNACE_NAMES and w["position"] == e["position"]
            for w in self.state.get("plan", {}).get("entities", [])
        )

    def clean_feed_baseline(self, obs):
        if self.state.get("automatic_feed_baseline"):
            return None
        furnaces = [e for e in obs["entities"] if self.owns_furnace(e, obs)]
        if any(e.get("products_finished", 0) > 0 or e.get("remaining_burning_fuel", 0) > 0 for e in furnaces):
            return {"status": "blocked", "reason": "prebaseline_furnace_activity_requires_explicit_review"}
        for e in furnaces:
            for item in ("coal", "wood"):
                count = e.get("inventory", {}).get(item, 0)
                if count:
                    return {
                        "type": "take",
                        "name": e["name"],
                        "position": e["position"],
                        "inventory": "fuel",
                        "item": item,
                        "count": count,
                        "reason": "normally recover legacy bootstrap fuel before automatic-feed baseline",
                    }
        self.state["automatic_feed_baseline"] = {
            "tick": obs["tick"],
            "preexisting_units": [e["unit_number"] for e in furnaces],
            "products_finished": 0,
            "remaining_burning_fuel": 0,
            "fuel_inventory_empty": True,
        }
        _atomic_json(self.path, self.state)
        return None

    def capacity_step(self, obs, parameters):
        validate_capacity(parameters)
        if not self.state or self.state.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "current_world_smelting_district_required"}
        resource = parameters["resource"]
        rate = parameters["rate_per_minute"]
        mining = json.loads((self.path.parent / "mining-outposts.json").read_text(encoding="utf-8"))
        available = sum(
            b["nominal_rate_per_minute"]
            for b in mining.get("banks", [])
            if b.get("ready") and b["plan"]["resource"] == resource
        )
        if mining.get("world_id") != obs["world_id"] or available < rate:
            return {
                "status": "blocked",
                "reason": "expand_connected_mining_before_smelting",
                "available_nominal_ore_rate": available,
            }
        old = next(b for b in self.state["banks"] if b["resource"] == resource)
        if "growth_slots" in old:
            return self._grow(obs, old, rate)
        existing = {xy(e): e["name"] for e in old["entities"] if e["name"] in FURNACE_NAMES}
        if not existing or set(existing.values()) - {"stone-furnace", "steel-furnace"}:
            return {"status": "blocked", "reason": "row_expansion_requires_existing_two_by_two_burner_furnaces"}
        preferred = old.get("furnace_name", next(iter(existing.values())))
        if preferred not in {"stone-furnace", "steel-furnace"}:
            return {"status": "blocked", "reason": "unsupported_expansion_furnace_type"}
        each = self.catalog.entities[preferred]["crafting_speed"] * 60 / self.catalog.recipes[old["product"]]["energy"]
        paired = self.state.get("paired", False)
        current = bank_capacities(self.catalog, [old])[resource]
        count = old.get("per_side_count", old["count"]) + max(
            0, math.ceil((rate - current) / each / (2 if paired else 1))
        )
        if count > 6:
            return {"status": "blocked", "reason": "reserved_row_has_six_furnace_slots"}
        # Rebuild in the canonical frame, then turn back like the original bank.
        canonical_old, turn = canonical_bank(old)
        first = next(e for e in canonical_old["entities"] if e["name"] in FURNACE_NAMES)
        if paired:
            from .paired_smelting import paired_bank

            new = paired_bank(first["position"]["x"], first["position"]["y"], count, resource, furnace_name=preferred)
        else:
            new = furnace_bank(first["position"]["x"], first["position"]["y"], count, resource, furnace_name=preferred)
        if not turn.identity:
            new = apply_transform(turn, new)
            new["site_transform"] = old["site_transform"]
        for e in new["entities"]:
            if e["name"] in FURNACE_NAMES and xy(e) in existing:
                e["name"] = existing[xy(e)]
        for row in new.get("producer_rows", {}).values():
            for e in row:
                if xy(e) in existing:
                    e["name"] = existing[xy(e)]
        extra = {k: v for k, v in new.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
        if preferred != "stone-furnace":
            extra["furnace_name"] = preferred
        new = _plan(new["entities"], new["ports"], **extra)
        if new != old:
            board_path = self.path.parent / "pcb-board.json"
            board = json.loads(board_path.read_text(encoding="utf-8"))
            if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
                return {"status": "blocked", "reason": "board_identity_or_catalog_changed"}
            if old["ports"][-1] != new["ports"][-1] and any(
                c["source"] == old["ports"][-1]["position"] for c in board.get("connections", [])
            ):
                return {
                    "status": "blocked",
                    "reason": "old_movable_output_has_registered_consumer_requires_parallel_cutover",
                }
            es = merge_entities(self.state["plan"]["entities"], new["entities"])
            boxes = [self.builder._occupied_by_plan([e]) for e in es]
            if sum(map(len, boxes)) != len(set().union(*boxes)):
                return {"status": "blocked", "reason": "expanded_row_prospective_overlap"}
            native = self.builder.can_place(es)
            if not native.get("ok"):
                return {
                    "status": "blocked",
                    "reason": "expanded_row_native_preflight_failed",
                    "blocked": native.get("blocked"),
                }
            banks = [new if b["resource"] == resource else b for b in self.state["banks"]]
            extra = {
                k: v
                for k, v in self.state["plan"].items()
                if k not in ("ok", "reason", "entities", "ports", "required_items")
            }
            ports = (
                self.state["plan"]["ports"]
                if paired
                else [p for b in banks for p in b["ports"] if p["direction"] == "output"]
            )
            plan = _plan(es, ports, **extra)
            self.state.update(plan=plan, banks=banks)
            self.state["nominal_smelting_capacity_per_minute"] = bank_capacities(self.catalog, banks)
            _atomic_json(self.path, self.state)
            board["blocks"]["smelting_district"] = plan
            _atomic_json(board_path, board)
        rates = bank_capacities(self.catalog, self.state["banks"])
        if self.state.get("nominal_smelting_capacity_per_minute") != rates:
            self.state["nominal_smelting_capacity_per_minute"] = rates
            _atomic_json(self.path, self.state)
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs)
        if proof:
            return {
                "status": "succeeded",
                "reason": "expanded furnace row produces with fixed boundary output",
                "evidence": proof,
            }
        return {"status": "waiting", "reason": "waiting for added furnaces to smelt ordinary belt inputs"}

    def _grow(self, obs, old, rate):
        """Paired-bank growth: build reserved slots in place (both rows), nothing is rerouted."""
        resource = old["resource"]
        names = {e["name"] for e in old["entities"] if e["name"] in FURNACE_NAMES}
        preferred = old.get("furnace_name", "stone-furnace")
        if names - {preferred} or preferred not in {"stone-furnace", "steel-furnace"}:
            return {"status": "blocked", "reason": "row_expansion_requires_existing_two_by_two_burner_furnaces"}
        each = self.catalog.entities[preferred]["crafting_speed"] * 60 / self.catalog.recipes[old["product"]]["energy"]
        wanted = max(1, math.ceil(rate / each / 2 - 1e-9))
        slots = old["capacity_slots"]
        if wanted > slots:
            # The next step is a second bank of the same product (module doc of smelting_site).
            return {
                "status": "blocked",
                "reason": "paired_bank_full_next_growth_is_another_bank",
                "evidence": {"capacity_slots": slots, "full_bank_rate_per_minute": 2 * slots * each},
            }
        new = grown_bank(old, max(wanted, old["per_side_count"]))
        if new["entities"] != old["entities"]:
            board_path = self.path.parent / "pcb-board.json"
            board = json.loads(board_path.read_text(encoding="utf-8"))
            if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
                return {"status": "blocked", "reason": "board_identity_or_catalog_changed"}
            before = {(e["name"], *xy(e)) for e in old["entities"]}
            added = [e for e in new["entities"] if (e["name"], *xy(e)) not in before]
            es = merge_entities(self.state["plan"]["entities"], added)
            boxes = [self.builder._occupied_by_plan([e]) for e in es]
            if sum(map(len, boxes)) != len(set().union(*boxes)):
                return {"status": "blocked", "reason": "expanded_row_prospective_overlap"}
            native = preflight(self.game, self.builder, obs, added)
            if not native.get("ok"):
                return {"status": "blocked", "reason": "expanded_row_native_preflight_failed", "detail": native}
            banks = [new if b["resource"] == resource else b for b in self.state["banks"]]
            extra = {
                k: v
                for k, v in self.state["plan"].items()
                if k not in ("ok", "reason", "entities", "ports", "required_items")
            }
            obstacles = {(e["name"], *xy(e)): e for e in self.state["plan"].get("clear_obstacles", [])}
            obstacles.update({(e["name"], *xy(e)): e for e in native.get("clear_obstacles", [])})
            extra.update(
                clear_obstacles=list(obstacles.values()),
                clearance_world_id=obs["world_id"],
                growth_entities=[e for b in banks for e in b.get("growth_entities", [])],
            )
            plan = _plan(es, self.state["plan"]["ports"], **extra)
            self.state.update(plan=plan, banks=banks)
            self.state["nominal_smelting_capacity_per_minute"] = bank_capacities(self.catalog, banks)
            _atomic_json(self.path, self.state)
            board["blocks"]["smelting_district"] = plan
            _atomic_json(board_path, board)
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs)
        if proof:
            return {
                "status": "succeeded",
                "reason": "reserved furnace slots built in place; ports and feed unchanged",
                "evidence": proof,
            }
        return {"status": "waiting", "reason": "waiting for added furnaces to smelt ordinary belt inputs"}

    def _map_site_anchors(self, game, sources):
        """District anchors on free land nearest the ore and coal sources, from one bulk map survey.

        Fixed coordinates and offsets around the sources can all land on ore or
        water on an unfamiliar map; a surveyed tile map finds real free land.
        Any survey problem returns no anchors, leaving the older candidates.
        """
        from .site.candidates import free_sites, survey_around

        points = [(p["position"]["x"], p["position"]["y"]) for p in sources.values()]
        try:
            world = survey_around(game, points)
            rects = free_sites(world, points, *DISTRICT_SIZE, limit=12)
        except Exception:  # noqa: BLE001 - site search is an optimisation, never a new failure mode
            return []
        # The district's reserved area is [x - 8, x + 40) x [y - 8, y + 24).
        return [(rect.x0 + 8, rect.y0 + 8) for rect in rects]

    def prepare(self, obs):
        from .industrial import PlanningBudget

        budget = PlanningBudget(self.game)
        game = budget.game
        if not obs.get("technologies", {}).get("logistics") and "logistics" not in self._coming(obs):
            return {"ok": False, "reason": "native_logistics_research_required"}
        p = self.path.parent / "mining-outposts.json"
        mining = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if mining.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "current_world_mining_banks_required"}
        sources, reason = district_sources(mining)
        if sources is None:
            return {"ok": False, "reason": reason}
        # Early start: a bank may be planned but not built yet. Its unbuilt
        # hardware outside its reserved area (bridge poles) is no routing
        # keep-out (transport.unbuilt_external_keepouts skips mining blocks),
        # so the whole plan is checked against it below.
        early_banks = not all(_bank_for(mining, port).get("ready") for port in sources.values())
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"ok": False, "reason": "board_identity_or_catalog_changed"}
        counts = {
            r: math.ceil(
                rate
                * self.catalog.recipes[product]["energy"]
                / 60
                / self.catalog.entities["stone-furnace"]["crafting_speed"]
            )
            for r, product, rate in (("iron-ore", "iron-plate", 60), ("copper-ore", "copper-plate", 30))
        }
        master = self._master_plan(obs)
        # The master plan's smelting zone comes first; free-land and legacy
        # anchors remain as fallbacks for saved worlds and plan failures.
        planned = plan_candidates(master, counts) if master is not None else []
        if planned:
            fallback = []
        else:
            fallback = self._map_site_anchors(game, sources)
        fallback = [district_candidate(x, y, counts) for x, y in fallback + list(smelting_site_anchors(sources))]
        candidates = []
        # Other blocks' planned-but-unbuilt hardware (bridges, routed belts)
        # lies outside their reserved areas; a district must not sit on it.
        unbuilt = planned_unbuilt_hardware(self.builder, obs, board.get("blocks", {}), exclude=("smelting_district",))
        for candidate in planned + fallback:
            area = candidate["area"]
            growth = [e for b in candidate["banks"] for e in b.get("growth_entities", [])]
            if hardware_conflicts(self.builder, candidate["entities"] + growth, unbuilt):
                continue
            if any(
                area[0][0] < b["reserved_area"][1][0]
                and area[1][0] > b["reserved_area"][0][0]
                and area[0][1] < b["reserved_area"][1][1]
                and area[1][1] > b["reserved_area"][0][1]
                for b in board.get("blocks", {}).values()
                if b.get("reserved_area")
            ):
                continue
            candidates.append(candidate)
        # Only what the survey reads: banks carry their (large) growth metadata.
        surveyed = [{"area": c["area"], "entities": c["entities"]} for c in candidates]
        args = json.dumps(json.dumps(surveyed, separators=(",", ":")))
        survey = game.query(
            """
local plans=helpers.json_to_table("""
            + args
            + """);local legal={}
for i,plan in ipairs(plans) do
 local clear=s.count_entities_filtered{area=plan.area,type="resource"}==0
 if clear then for _,e in pairs(s.find_entities_filtered{area=plan.area}) do
  local natural=e.force.name=="neutral" and e.minable and (e.type=="tree" or e.type=="simple-entity")
  if e.type~="electric-pole" and e.type~="character" and not natural
   and not e.prototype.collision_mask.colliding_with_tiles_only then clear=false;break end
 end end
 if clear then for _,tile in pairs(s.find_tiles_filtered{area=plan.area}) do
  for layer in pairs(prototypes.entity["stone-furnace"].collision_mask.layers) do
   if tile.collides_with(layer) then clear=false;break end
  end
  if not clear then break end
 end end
 if clear then for _,e in ipairs(plan.entities) do
  local old=target(e.position,e.name)
  if not (old and old.force==f and old.type=="electric-pole")
   and not s.can_place_entity{name=e.name,position=e.position,direction=e.direction,force=f,
    build_check_type=defines.build_check_type.manual_ghost,forced=true} then clear=false;break end
 end end
 if clear then legal[#legal+1]=i end
end
return {ok=true,legal=legal}
"""
        )
        if not survey.get("ok"):
            return survey
        # The search resumes where the last pass's wall-clock budget ran out:
        # a candidate that already failed is not routed again in the same sweep
        # (fleet auto-1113-1758: two seeds restarted the whole search for 12-15
        # game minutes on a loaded machine and never reached the good site).
        cursor = self._search_cursor(obs)
        failures = list(cursor["failures"])
        pending = None  # the candidate being evaluated; it failed when the loop moves on in budget
        for index in survey.get("legal", []):
            if pending is not None and not budget.expired():
                cursor["tried"].append(pending)
                pending = None
            if budget.expired():
                break
            site = candidates[index - 1]
            site_key = _site_key(site)
            if site_key in cursor["tried"]:
                continue
            pending = site_key
            es = site["entities"]
            routes = []
            good = True
            connections = []
            for bank, branch in zip(site["banks"], site["coal_branches"]):
                connections.append((branch, next(p for p in bank["ports"] if p["item"] == "coal")))
            for bank in site["banks"]:
                connections.append(
                    (sources[bank["resource"]], next(p for p in bank["ports"] if p["item"] == bank["resource"]))
                )
            connections.append((sources["coal"], site["coal_input"]))
            # The banks' unbuilt furnace slots are hard for the district's own
            # feed routes and pole bridges (once registered, the whole reserved
            # area is hard for every other block's routes and poles).
            keepouts = growth_keepouts(self.builder, site["banks"])
            reserved_growth = {xy(e) for e in keepouts}
            for source, destination in connections:
                if budget.expired():
                    good = False
                    failures.append({"stage": "planning_budget", "reason": "smelting_planning_budget_exhausted"})
                    break
                guide = None
                if master is not None and source in sources.values():
                    guide = corridor_guide(master, source["item"])
                route = belt_connection(game, self.builder, obs, source, destination, es + keepouts, **guided(guide))
                if not route.get("ok"):
                    failures.append(
                        {
                            "stage": "corridor",
                            "area": site["area"],
                            "item": source["item"],
                            "source": source["position"],
                            "destination": destination["position"],
                            "reason": route.get("reason"),
                        }
                    )
                    good = False
                    break
                routes.append(route)
                es = merge_entities(es, route["entities"])
            if not good:
                continue
            powers = []
            for bank in site["banks"]:
                pole = next(e for e in bank["entities"] if e["name"] == "small-electric-pole")
                power = power_connection(
                    game,
                    self.builder,
                    obs,
                    pole["position"],
                    [e for e in es if e["name"] != "small-electric-pole"] + keepouts,
                )
                if not power.get("ok"):
                    failures.append({"stage": "power", "area": site["area"], "reason": power.get("reason")})
                    good = False
                    break
                powers.append(power["entities"])
                es = merge_entities(es, power["entities"])
            if not good:
                continue
            boxes = [self.builder._occupied_by_plan([e]) for e in es]
            if sum(map(len, boxes)) != len(set().union(*boxes)):
                failures.append(
                    {"stage": "whole_geometry", "area": site["area"], "reason": "whole_prospective_plan_overlaps"}
                )
                continue
            if early_banks and _foreign_unbuilt_conflicts(self.builder, obs, board.get("blocks", {}), es):
                failures.append(
                    {"stage": "planned_hardware", "area": site["area"], "reason": "plan_meets_unbuilt_mining_hardware"}
                )
                continue
            if set().union(*boxes) & reserved_growth:
                failures.append(
                    {"stage": "growth_footprint", "area": site["area"], "reason": "plan_entity_in_reserved_growth_slot"}
                )
                continue
            # Corridors may include exact approved natural clearances. A raw
            # can_place repeat rejects those trees and discards its own earlier
            # route approval. Recheck the complete resource/ownership footprint
            # with the same native preflight used by paid clearance/building.
            whole = preflight(game, self.builder, obs, es)
            if not whole.get("ok"):
                failures.append({"stage": "whole_preflight", "area": site["area"], "reason": whole.get("reason")})
                continue
            pairs = [pair for route in routes for pair in route.get("underground_pairs", [])]
            pairs += [pair for bank in site["banks"] for pair in bank.get("underground_pairs", [])]  # split heads
            es = producer_first_order(es, site, connections, routes, powers)
            plan = _plan(
                es,
                [p for b in site["banks"] for p in b["ports"] if p["direction"] == "output"],
                reserved_area=site["area"],
                layout_policy="pcb-smelting-district-v1",
                underground_pairs=pairs,
                clear_obstacles=whole["clear_obstacles"],
                clearance_world_id=obs["world_id"],
                growth_entities=[e for b in site["banks"] for e in b.get("growth_entities", [])],
                **({"site_transform": site["site_transform"]} if "site_transform" in site else {}),
            )
            self._save_search_cursor(None)
            return {
                "ok": True,
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "plan": plan,
                "banks": site["banks"],
                "routes": [{"metrics": r["route_metrics"], "pairs": r["underground_pairs"]} for r in routes],
                "nominal_input_rates": {"iron-ore": 60, "copper-ore": 30},
                "coal_splitter_policy": "ordinary balanced outputs; existing power feed remains separate",
                "bandwidth": {
                    **district_head_choice(),
                    "scope": "canonical district bank at full reserved growth through belt_flow",
                },
                # The bank ports this plan was routed from (re-plan or block when one changes).
                "source_ports": dict(sources),
            }
        if pending is not None:
            if not budget.expired():
                cursor["tried"].append(pending)
            else:
                # Cut short by the budget: retried first next pass, but a candidate that
                # alone outlasts the budget twice counts as failed so the sweep moves on.
                count = cursor["interrupted"].get(pending, 0) + 1
                cursor["interrupted"][pending] = count
                if count >= SEARCH_INTERRUPTIONS_PER_SITE:
                    cursor["tried"].append(pending)
                    failures.append({"stage": "planning_budget", "reason": "site_outlasted_planning_budget"})
        untried = [i for i in survey.get("legal", []) if _site_key(candidates[i - 1]) not in cursor["tried"]]
        exhausted = budget.expired() and bool(untried)
        cursor["failures"] = failures[-SEARCH_FAILURES_KEPT:]
        self._save_search_cursor(cursor if exhausted else None)
        stages = {
            stage: sum(f["stage"] == stage for f in failures) for stage in dict.fromkeys(f["stage"] for f in failures)
        }
        from . import evidence_capture

        if evidence_capture.active():  # post-mortem: every candidate, filtered before or after the land survey
            legal = set(survey.get("legal", []))
            evidence_capture.note(
                "smelting_district_candidates",
                offered=len(planned) + len(fallback),
                after_block_filters=len(candidates),
                land_legal=[c["area"] for i, c in enumerate(candidates, 1) if i in legal],
                land_illegal=[c["area"] for i, c in enumerate(candidates, 1) if i not in legal],
                failures=failures,
            )
        return {
            "ok": False,
            "reason": "no_clear_pcb_smelting_district_with_corridors",
            "legal_sites": len(survey.get("legal", [])),
            "candidate_count": len(candidates),
            "failures": failures[-6:],
            "evidence": {
                "legal_sites": len(survey.get("legal", [])),
                "candidate_count": len(candidates),
                "failure_stage_counts": stages,
                "last_failure_reason": failures[-1]["reason"] if failures else "no_legal_land_site",
                "planning_budget_exhausted": exhausted,
                "sites_tried": len(cursor["tried"]),
                "sites_untried": len(untried),
            },
        }

    def _search_path(self):
        return self.path.parent / "smelting-district-search.json"

    def _search_cursor(self, obs):
        """The resumable site sweep of this world: candidates that failed, and budget cut-offs."""
        try:
            cursor = json.loads(self._search_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cursor = {}
        if not isinstance(cursor, dict) or cursor.get("world_id") != obs["world_id"]:
            cursor = {}
        return {
            "world_id": obs["world_id"],
            "tried": list(cursor.get("tried", [])),
            "interrupted": dict(cursor.get("interrupted", {})),
            "failures": list(cursor.get("failures", [])),
        }

    def _save_search_cursor(self, cursor):
        """Keep the sweep while it is unfinished; a finished sweep (found or exhausted) starts afresh."""
        path = self._search_path()
        if cursor is None:
            if path.exists():
                path.unlink()
            return
        _atomic_json(path, cursor)

    def _coming(self, obs):
        from .early_start import ledger_jobs, technologies_coming

        return technologies_coming(obs, ledger_jobs(self.path.parent, obs.get("world_id")))

    def _inputs_changed(self, obs):
        """None, a blocked result, or ``{"replan": True}`` when a source bank port of this plan changed.

        Only plans that recorded ``source_ports`` are checked. A port is still
        valid while some saved bank of this world has it. Nothing of the plan
        built: the plan and its board block are dropped and planned again.
        Something built: blocked (the built part may stand in the new way).
        """
        recorded = self.state.get("source_ports")
        if not isinstance(recorded, dict) or self.state.get("world_id") != obs["world_id"]:
            return None
        p = self.path.parent / "mining-outposts.json"
        mining = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        saved = []
        if mining.get("world_id") == obs["world_id"]:
            saved = [b["plan"]["ports"][0] for b in mining.get("banks", []) if b.get("plan", {}).get("ports")]
        if all(port in saved for port in recorded.values()):
            return None
        built = built_keys(obs)
        if any((e["name"], *xy(e)) in built for e in self.state["plan"]["entities"]):
            return {"status": "blocked", "reason": "smelting_inputs_changed_after_partial_build"}
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
        if board.get("blocks", {}).get("smelting_district") == self.state["plan"]:
            board["blocks"].pop("smelting_district")
            _atomic_json(board_path, board)
        self.state = {}
        _atomic_json(self.path, self.state)
        return {"replan": True}

    def step(self, obs):
        changed = self._inputs_changed(obs) if self.state else None
        if changed is not None and not changed.get("replan"):
            return changed
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok") and (prepared.get("evidence") or {}).get("planning_budget_exhausted"):
                # The search ran out of its wall-clock budget before trying every candidate: not a
                # "no site" answer (fleet fixpace1 seed 2692229611 blocked here at 16.5 and 19.0 min
                # on a loaded machine). Search again next pass; the stall rule still bounds it.
                return {
                    "status": "waiting",
                    "reason": "district site search ran out of planning budget; searching again",
                    "evidence": prepared["evidence"],
                }
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            _atomic_json(self.path, self.state)
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "smelting_world_or_catalog_changed"}
        # Republish after a crash between state persistence and board publication.
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "board_identity_or_catalog_changed"}
        if board["blocks"].get("smelting_district") != self.state["plan"]:
            if "smelting_district" in board["blocks"]:
                return {"status": "blocked", "reason": "smelting_reservation_changed"}
            board["blocks"]["smelting_district"] = self.state["plan"]
            _atomic_json(board_path, board)
        cleanup = self.clean_feed_baseline(obs)
        if cleanup is not None:
            return cleanup
        from .early_start import deferral

        # Early start: entities waiting for coming research or for a plate
        # another job waits for are left for later; the rest is built.
        defer = deferral(self.path.parent, obs, self.catalog, "smelting_district")
        result = self.builder.ensure_plan(obs, self.state["plan"], **({"defer": defer} if defer else {}))
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs)
        if proof:
            return {
                "status": "succeeded",
                "reason": "mined ore and split coal normally smelted onto fixed plate ports",
                "evidence": proof,
            }
        return {"status": "waiting", "reason": "waiting for normal belt-fed ore/coal and native furnace production"}

    def proof(self, obs):
        if (
            not self.state
            or not self.state.get("automatic_feed_baseline")
            or not underground_proof(obs, self.state["plan"])
        ):
            return None
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        for wanted in self.state["plan"]["entities"]:
            found = actual.get((wanted["name"], *xy(wanted)))
            if found is None:
                return None
            if (
                wanted["name"] in {"transport-belt", "underground-belt", "inserter", "splitter"}
                and found["direction"] != wanted["direction"]
            ):
                return None
            if wanted["name"] == "inserter" and (
                not found.get("electric_network_connected") or found.get("energy", 0) <= 0
            ):
                return None
        evidence = []
        planned = {(e["name"], *xy(e)) for e in self.state["plan"]["entities"]}
        for saved in self.state["banks"]:
            # The district plan is what is built: a later splice (bus feed splitter on the
            # output belt) replaces a bank belt there, so the bank's own list is filtered by it.
            bank = dict(saved, entities=[e for e in saved["entities"] if (e["name"], *xy(e)) in planned])
            furnaces = [actual[(e["name"], *xy(e))] for e in bank["entities"] if e["name"] in FURNACE_NAMES]
            if not furnaces or any(e.get("products_finished", 0) < 1 for e in furnaces):
                return None
            for furnace in furnaces:
                if furnace["name"] in {"stone-furnace", "steel-furnace"}:
                    if not (
                        furnace.get("remaining_burning_fuel", 0) > 0
                        or any(furnace.get("inventory", {}).get(fuel, 0) > 0 for fuel in ("coal", "wood"))
                    ):
                        return None
                elif not furnace.get("electric_network_connected") or furnace.get("energy", 0) <= 0:
                    return None
            poles = [actual[(e["name"], *xy(e))] for e in bank["entities"] if e["name"] == "small-electric-pole"]
            if any(not e.get("electric_network_id") for e in poles):
                return None
            plate_count = sum(
                actual[(e["name"], *xy(e))].get("belt_inventory", {}).get(bank["product"], 0)
                for e in bank["entities"]
                if e["name"] == "transport-belt"
            )
            if not plate_count:
                return None
            evidence.append(
                {
                    "product": bank["product"],
                    "furnace_units": [e["unit_number"] for e in furnaces],
                    "machine_products_finished": [e["products_finished"] for e in furnaces],
                    "plates_on_belts": plate_count,
                }
            )
        lanes = native_feed_lanes(self.game, self.state["banks"])
        if not feed_lanes_ok(lanes):
            return None
        return {
            "tick": obs["tick"],
            "automatic_feed_baseline": self.state["automatic_feed_baseline"],
            "reserved_area": self.state["plan"]["reserved_area"],
            "banks": evidence,
            "native_feed_lanes": lanes["banks"],
            "underground_pairs": len(self.state["plan"]["underground_pairs"]),
            "splitter_count": sum(e["name"] == "splitter" for e in self.state["plan"]["entities"]),
            "scope": "ordinary mined/belt-fed smelting and output, no actor furnace feeding; sustained rate requires a downstream load and timed proof",
        }
