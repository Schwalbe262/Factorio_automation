"""``supply_cell``: a temporary, hand-fed assembler cell for bulk construction items.

Why: before the smelting district and the bus exist, every belt and inserter
was hand crafted (668 belts in the first 50 minutes on seed 283895142) and
jobs queued behind "crafting queue is busy". A player puts down a small
chest-fed factory as soon as power and automation exist. This module is that
cell, modelled on the startup science cell (``board.startup_science_cell``):
not connected to any belt or the bus, plates go in by hand in batches,
machines make the product, the product is collected from a chest.

Lifecycle (rules in code, see ``docs/v2/PLANNERS.md``, "Supply cell"):

1. Propose (``supply_cell_nodes``): after ``power`` succeeded and
   assembling-machine-1 is enabled, once per map, for the products that
   ``candidates`` qualifies from the next ``DEMAND_HORIZON`` roadmap steps
   (``progression.bulk_item_demand``): more than ``HANDCRAFT_LIMIT`` needed,
   the hand time saved pays for hand crafting the cell's own hardware, and
   machine time stays within ``WAIT_FACTOR`` x hand time.
2. Build (``SupplyCell.step``): design every product's recipe tree from the
   catalog (``recipe_tree``, ``layout``), survey a site near the powered
   grid (ring like ``board.reserve_startup_science``; never on ore, never in a
   site-plan zone or another block), register board block ``supply_cell``,
   build it, cap each output chest with a bar, feed a first batch, prove
   machine output in the chests.
3. Use: ``ensure_item`` collects from the output chests and stops hand
   crafting above ``HANDCRAFT_LIMIT`` (``stock`` hook, time rule); shared
   upkeep and the idle lane top up the input chests, sized from demand
   (``feed_action``).
4. Retire (``supply_cell_nodes`` with ``{"retire": true}``): when every
   product's remaining demand is at most ``HANDCRAFT_LIMIT`` or a ``produce``
   row for it succeeded. Feeding stops at once; the job waits while any job
   still waits for its products, then mines the machines, inserters and
   chests (paid ``mine``, contents return to the inventory), removes the board
   block and records ``phase: retired`` (never proposed again). Poles stay:
   other blocks' pole chains may hang on them.

Cell geometry for ``["transport-belt", "inserter"]`` (``layout`` on the real
catalog; ``>`` an inserter moving items east, ``^`` north; P pole)::

    ...GGG...BBB...     a iron (gear)       G gear     b iron + gears     B belt
    .a>GGG>b>BBB>O     O belt chest, bar 2 stacks; P poles
    ...GGG...BBB...
    P......P..P....
    ...GGG...III...     c iron (gear)       d iron + gears + circuits   I inserter
    .c>GGG>d>III>Q     Q inserter chest, bar 2 stacks
    ...GGG.^.III...
    ......EEE......     E circuit machine
    .....PEEE......
    ......EEE......
    .......^.......
    .......e.......     e iron + cables
    .......^.......
    ......KKK......     K cable machine
    .....PKKK......
    ......KKK......
    .......^.......
    .......f.......     f copper

Each machine sits between the chest it feeds and its own input chest, one
inserter on each side; a machine's first intermediate ingredient continues
the chain away from the product chest, a second and third branch off to the
sides of its input chest (``_place``). Raw
ingredients (``progression.RAW_ITEMS``) are hand-fed ports on that input
chest. Inserters only take what a recipe needs, so a chest may hold plates
and an intermediate at once (the startup science hub trick).
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from .blocks.params import pole_radius, pole_reach
from .blocks.stock_tail import STOCK_STACKS
from .board import Board, entity, hardware_conflicts, planned_unbuilt_hardware, pole_chain
from .construction_supply import HANDCRAFT_LIMIT, PLATE_BATCH
from .core.deterministic_builder import FactoryBuilder, _plan
from .core.deterministic_state import _atomic_json
from .machine_stock import STALL_TICKS
from .routing import VECTORS

# ----- constants (tuning = change here and the pinning test) -----------------
CAPABILITY = "supply_cell"
STATE_FILE = "supply-cell.json"
BLOCK = "supply_cell"
TEMPLATE = "supply_cell_v1"
MACHINE = "assembling-machine-1"
INSERTER = "inserter"
POLE = "small-electric-pole"
CHESTS = ("wooden-chest", "iron-chest")  # cheapest enabled first
DEMAND_HORIZON = 8  # roadmap steps whose ``builds`` count as upcoming demand
# Chest <-> machine basic inserter, hand size 1: about 0.83 swings/s.
CHEST_INSERTER_PER_MINUTE = 50.0
HAND_CRAFT_SPEED = 1.0  # the character crafts at speed 1
# A waiting job leaves the character to the other active jobs (jobs.MAX_ACTIVE_JOBS = 4).
WAIT_FACTOR = 3.0
MAX_CHILDREN = 3  # intermediate ingredients one machine's input chest can take (west, north, south)
MAX_DEPTH = 4
FEED_CAP = 100  # items of one kind per input chest (one plate stack)
FEED_BATCH = PLATE_BATCH  # never carry fewer plates than a plate collection batch
FEED_RESERVE = 50  # plates a job's own upkeep feed leaves in the character's hands
PROOF_UNITS = 2  # first batch: enough for two products of each kind
OUTPUT_STACKS = STOCK_STACKS
SITE_PITCH = 8  # candidate origins on an 8-tile grid around the powered pole
SITE_REACH = 6  # ... up to 48 tiles each way, nearest first
MAX_SITE_CANDIDATES = 40
PROGRESS_WRITE_TICKS = 600  # at most one progress write per 10 game seconds
BUILD_PRIORITY = -52  # just below produce rows (-51)
RETIRE_PRIORITY = -1000  # demolition is never on the critical path
NODE_ID = "auto_supply_cell"
RETIRE_NODE_ID = "auto_supply_cell_retire"
_GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


def _raw_items():
    from .progression import RAW_ITEMS

    return RAW_ITEMS


def _out_amount(recipe, item):
    from .progression import _product_amount

    return _product_amount(recipe, item)


# ----- recipe tree ---------------------------------------------------------
def recipe_tree(catalog, item, units=1.0, *, enabled=None, depth=0):
    """``{item, recipe, energy, crafts, raw, children}`` per product unit, from catalog recipes only.

    ``crafts`` is crafts of the recipe per product unit, ``raw`` the hand-fed
    items this machine's input chest needs per product unit. Raises
    ValueError when the tree does not fit the cell (fluid, no recipe, a
    category assembling-machine-1 cannot craft, too deep or too wide, locked
    recipe when ``enabled`` is given).
    """
    if depth > MAX_DEPTH:
        raise ValueError(f"supply cell recipe tree too deep at {item}")
    recipe = catalog.recipe_for_product(item)
    if not recipe:
        raise ValueError(f"no recipe for {item}")
    categories = set((catalog.entities.get(MACHINE) or {}).get("crafting_categories") or ())
    if not set(recipe.get("categories") or [recipe.get("category")]) & categories:
        raise ValueError(f"{MACHINE} cannot craft {recipe['name']}")
    if enabled is not None and recipe["name"] not in enabled:
        raise ValueError(f"recipe {recipe['name']} is locked")
    crafts = units / _out_amount(recipe, item)
    raw, children = {}, []
    for row in recipe.get("ingredients") or ():
        if row.get("type", "item") != "item":
            raise ValueError(f"{recipe['name']} needs a fluid")
        need = row["amount"] * crafts
        if row["name"] in _raw_items():
            raw[row["name"]] = raw.get(row["name"], 0.0) + need
        else:
            children.append(recipe_tree(catalog, row["name"], need, enabled=enabled, depth=depth + 1))
    if len(children) > MAX_CHILDREN:
        raise ValueError(f"{recipe['name']} has more than {MAX_CHILDREN} intermediate ingredients")
    return {
        "item": item,
        "recipe": recipe["name"],
        "energy": float(recipe.get("energy") or 0.5),
        "units": units,
        "crafts": crafts,
        "raw": raw,
        "children": children,
    }


def _nodes(tree):
    yield tree
    for child in tree["children"]:
        yield from _nodes(child)


def hand_seconds(catalog, item, count=1.0):
    """Character crafting time for ``count`` items, intermediates included (catalog energy)."""
    if item in _raw_items():
        return 0.0
    recipe = catalog.recipe_for_product(item)
    if not recipe:
        return 0.0
    crafts = count / _out_amount(recipe, item)
    total = crafts * float(recipe.get("energy") or 0.5) / HAND_CRAFT_SPEED
    for row in recipe.get("ingredients") or ():
        if row.get("type", "item") == "item":
            total += hand_seconds(catalog, row["name"], row["amount"] * crafts)
    return total


def units_per_minute(catalog, tree):
    """Product units/min: the slowest machine or chest inserter of the tree."""
    speed = float((catalog.entities.get(MACHINE) or {}).get("crafting_speed") or 0.5)
    rates = []
    for node in _nodes(tree):
        rates.append(60.0 * speed / node["energy"] / node["crafts"])
        moved_in = sum(node["raw"].values()) + sum(child["units"] for child in node["children"])
        rates.append(CHEST_INSERTER_PER_MINUTE / moved_in)
        rates.append(CHEST_INSERTER_PER_MINUTE / node["units"])
    return min(rates)


def hardware(tree):
    """Items the tree places: one machine and two inserters per node, one input chest per node, one output chest."""
    machines = sum(1 for _ in _nodes(tree))
    return {MACHINE: machines, INSERTER: 2 * machines, "chest": machines + 1}


def evaluate(catalog, item, count, *, enabled=None, chest="wooden-chest"):
    """Whether a cell for ``count`` items pays: the rule ``candidates`` applies per item."""
    row = {"item": item, "count": count, "qualifies": False}
    try:
        tree = recipe_tree(catalog, item, enabled=enabled)
    except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
        return dict(row, reason=str(exc)[:120])
    hand = hand_seconds(catalog, item, count)
    rate = units_per_minute(catalog, tree)
    machine = 60.0 * count / rate
    parts = hardware(tree)
    build = sum(hand_seconds(catalog, name if name != "chest" else chest, n) for name, n in parts.items())
    row.update(
        hand_seconds=round(hand, 1),
        machine_seconds=round(machine, 1),
        build_hand_seconds=round(build, 1),
        units_per_minute=round(rate, 2),
    )
    if count <= HANDCRAFT_LIMIT:
        return dict(row, reason="demand within the hand-craft limit")
    if hand < build:
        return dict(row, reason="hand crafting the demand is cheaper than hand crafting the cell")
    if machine > WAIT_FACTOR * hand:
        return dict(row, reason="machine time exceeds the wait factor over hand time")
    return dict(row, qualifies=True, reason="cell pays for itself")


def candidates(catalog, jobs, observation):
    """Receipt field ``supply_cell_candidates``: per-item demand rows and the qualifying products."""
    from .progression import bulk_item_demand

    demand = bulk_item_demand(catalog, jobs, observation, horizon=DEMAND_HORIZON)
    enabled = _enabled(observation)
    chest = choose_chest(enabled) or CHESTS[0]
    rows = {item: evaluate(catalog, item, row["count"], enabled=enabled, chest=chest) for item, row in demand.items()}
    return {
        "demand": {item: row["count"] for item, row in demand.items()},
        "products": sorted(item for item, row in rows.items() if row["qualifies"]),
        "rows": rows,
    }


def _enabled(observation):
    value = (observation or {}).get("enabled_recipes") or {}
    if isinstance(value, dict):
        return {name for name, on in value.items() if on}
    return set(value)


def choose_chest(enabled):
    return next((name for name in CHESTS if name in enabled), None)


# ----- layout --------------------------------------------------------------
def _xy(e):
    return (e["position"]["x"], e["position"]["y"])


def _direction(vector):
    return next(d for d, v in VECTORS.items() if v == tuple(vector))


def _add(p, v, k=1):
    return (p[0] + k * v[0], p[1] + k * v[1])


def _place(tree, chest_at, d, chest, rows):
    """Machine of ``tree`` west of ``chest_at`` (direction ``d``), its input chest, then its children."""
    facing = _direction(d)
    machine_at, input_at = _add(chest_at, d, 3), _add(chest_at, d, 6)
    rows["entities"] += [
        entity(INSERTER, *_add(chest_at, d, 1), facing),
        entity(MACHINE, *machine_at, recipe=tree["recipe"]),
        entity(INSERTER, *_add(chest_at, d, 5), facing),
        entity(chest, *input_at),
    ]
    rows["nodes"].append(
        {
            "item": tree["item"],
            "machine": {"x": machine_at[0], "y": machine_at[1]},
            "chest": {"x": input_at[0], "y": input_at[1]},
            "raw": dict(tree["raw"]),
        }
    )
    side = (d[1], -d[0])
    for child, way in zip(tree["children"], (d, side, (-side[0], -side[1]))):
        _place(child, input_at, way, chest, rows)


def _tiles(entities):
    return set().union(*[_GEOMETRY._occupied_by_plan([e]) for e in entities]) if entities else set()


def _tree_rows(tree, origin, chest):
    rows = {"entities": [entity(chest, *origin)], "nodes": []}
    _place(tree, origin, (-1, 0), chest, rows)
    tiles = [_GEOMETRY._occupied_by_plan([e]) for e in rows["entities"]]
    if sum(map(len, tiles)) != len(set().union(*tiles)):
        raise ValueError(f"supply cell tree of {tree['item']} overlaps itself")
    return rows


def _covers(catalog, pole, e):
    reach = pole_radius(catalog, POLE) - 0.5
    px, py = pole
    return any(abs(tx - px) <= reach + 1e-6 and abs(ty - py) <= reach + 1e-6 for tx, ty in _tiles([e]))


def place_poles(catalog, entities):
    """Greedy poles: most uncovered machines/inserters first, every new pole within wire reach of one before."""
    needy = [e for e in entities if e["name"] in (MACHINE, INSERTER)]
    taken = _tiles(entities)
    xs = [t[0] for t in taken]
    ys = [t[1] for t in taken]
    free = [
        (x + 0.5, y + 0.5)
        for x in range(math.floor(min(xs)) - 1, math.floor(max(xs)) + 2)
        for y in range(math.floor(min(ys)) - 1, math.floor(max(ys)) + 2)
        if (x + 0.5, y + 0.5) not in taken
    ]
    wire = pole_reach(catalog, POLE)
    poles, uncovered = [], list(needy)
    while uncovered:
        near = [t for t in free if not poles or any(math.dist(t, p) <= wire for p in poles)]
        scored = sorted(near, key=lambda t: (-sum(_covers(catalog, t, e) for e in uncovered), t))
        if not scored:
            raise ValueError("supply cell poles cannot reach every machine")
        best = scored[0]
        if not any(_covers(catalog, best, e) for e in uncovered):
            # A relay towards the nearest uncovered entity keeps the chain connected.
            goal = min((_xy(e) for e in uncovered), key=lambda g: min(math.dist(g, p) for p in poles))
            best = min(near, key=lambda t: (math.dist(t, goal), t))
        poles.append(best)
        free.remove(best)
        uncovered = [e for e in uncovered if not _covers(catalog, best, e)]
        if len(poles) > len(needy) + 8:
            raise ValueError("supply cell pole placement does not converge")
    return [entity(POLE, x, y) for x, y in poles]


def layout(catalog, products, *, chest, enabled=None, origin=(0.5, 0.5)):
    """The cell plan for ``products`` around ``origin`` (first product chest): entities, ports, nodes, outputs."""
    entities, ports, products_rows = [], [], []
    top = None
    for item in products:
        tree = recipe_tree(catalog, item, enabled=enabled)
        probe = _tree_rows(tree, (0.5, 0.5), chest)
        ys = [t[1] for t in _tiles(probe["entities"])]
        offset = 0 if top is None else top - min(ys) + 2  # one free row between trees
        at = (origin[0], origin[1] + offset)
        rows = _tree_rows(tree, at, chest)
        top = max(t[1] for t in _tiles(rows["entities"]))
        entities += rows["entities"]
        output = {"x": at[0], "y": at[1]}
        ports.append({"item": item, "kind": "item", "direction": "output", "position": dict(output)})
        for node in rows["nodes"]:
            for raw in node["raw"]:
                ports.append({"item": raw, "kind": "item", "direction": "input", "position": dict(node["chest"])})
        products_rows.append(
            {
                "item": item,
                "output": output,
                "nodes": rows["nodes"],
                "units_per_minute": round(units_per_minute(catalog, tree), 3),
                "hand_seconds_per_unit": round(hand_seconds(catalog, item), 4),
            }
        )
    tiles = _tiles(entities)
    if len(tiles) != sum(len(_GEOMETRY._occupied_by_plan([e])) for e in entities):
        raise ValueError("supply cell trees overlap")
    entities += place_poles(catalog, entities)
    return {"entities": entities, "ports": ports, "products": products_rows, "chest": chest}


def _area(entities, margin=1):
    tiles = _tiles(entities)
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    return [[min(xs) - 0.5 - margin, min(ys) - 0.5 - margin], [max(xs) + 0.5 + margin, max(ys) + 0.5 + margin]]


def _shift(design, dx, dy):
    moved = deepcopy(design)

    def go(p):
        p["x"] += dx
        p["y"] += dy

    for e in moved["entities"]:
        go(e["position"])
    for port in moved["ports"]:
        go(port["position"])
    for row in moved["products"]:
        go(row["output"])
        for node in row["nodes"]:
            go(node["machine"])
            go(node["chest"])
    return moved


def _overlaps(a, b):
    return a[0][0] < b[1][0] and b[0][0] < a[1][0] and a[0][1] < b[1][1] and b[0][1] < a[1][1]


SURVEY_LUA = """local plans=helpers.json_to_table(PAYLOAD);local legal={};local clearable={}
for i,p in ipairs(plans) do
 local ok=s.count_entities_filtered{area=p.reserved_area,type="resource"}==0
 if ok then for _,e in pairs(s.find_entities_filtered{area=p.reserved_area,force=f}) do
  if e.type~="character" and e.type~="electric-pole" then ok=false;break end end end
 local natural=false
 if ok then for _,e in ipairs(p.entities) do
  local q=e.position
  if s.count_entities_filtered{area={{q.x-.5,q.y-.5},{q.x+.5,q.y+.5}},type="resource"}>0 then ok=false;break end
  local old=target(q,e.name)
  if not (old and old.force==f and old.type=="electric-pole")
   and not s.can_place_entity{name=e.name,position=q,direction=e.direction,force=f} then
   if s.can_place_entity{name=e.name,position=q,direction=e.direction,force=f,
    build_check_type=defines.build_check_type.manual_ghost,forced=true} then natural=true else ok=false;break end
  end
 end end
 if ok and natural then clearable[#clearable+1]=i elseif ok then legal[#legal+1]=i end
end
return {ok=true,legal=legal,clearable=clearable}"""


def _indices(value):
    return list(value.values()) if isinstance(value, dict) else list(value or [])


# ----- controller ------------------------------------------------------------
class SupplyCell:
    """One cell per map; state ``supply-cell.json``, board block ``supply_cell``."""

    def __init__(self, game, bootstrap, builder, catalog, *, site=None, rows_stock=None):
        self.game, self.bootstrap, self.builder, self.catalog = game, bootstrap, builder, catalog
        self.site = site
        self.rows_stock = rows_stock  # produce rows hook: a producing row is the permanent source
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE

    # state
    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _save(self, state):
        _atomic_json(self.path, state)

    def _live(self, obs):
        """The saved state when it belongs to this world and catalog, else {}."""
        state = self.state()
        if state.get("world_id") != obs.get("world_id") or state.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {}
        return state

    def _jobs(self, obs):
        path = self.root / "jobs.json"
        if not path.exists():
            return []
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("world_id") != obs.get("world_id"):
            return []
        return [job for job in ledger.get("jobs", {}).values() if job.get("world_id") == obs.get("world_id")]

    def demand(self, obs):
        from .progression import bulk_item_demand

        rows = bulk_item_demand(self.catalog, self._jobs(obs), obs, horizon=DEMAND_HORIZON)
        return {item: row["count"] for item, row in rows.items()}

    # site
    def prepare(self, obs, products):
        enabled = _enabled(obs)
        chest = choose_chest(enabled)
        if chest is None:
            return {"ok": False, "reason": "supply_cell_chest_recipe_locked"}
        try:
            canonical = layout(self.catalog, products, chest=chest, enabled=enabled)
        except ValueError as exc:
            return {"ok": False, "reason": "supply_cell_design_unsupported", "detail": str(exc)[:200]}
        networks = {
            e.get("electric_network_id")
            for e in obs.get("entities", [])
            if e.get("name") == "steam-engine" and e.get("energy", 0) > 0
        }
        poles = [
            e for e in obs.get("entities", []) if e.get("name") == POLE and e.get("electric_network_id") in networks
        ]
        if not poles:
            return {"ok": False, "reason": "no_energized_source_network"}
        board = Board(self.game, self.builder, self.catalog)
        if board.state and board.state.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "board_world_changed"}
        blocks = board.state.get("blocks", {})
        planned = planned_unbuilt_hardware(self.builder, obs, blocks)
        zones = self._zones(obs)
        if isinstance(zones, dict):
            return zones
        reference = poles[0]["position"]
        bx, by = math.floor(reference["x"]) + 0.5, math.floor(reference["y"]) + 0.5
        reach = range(-SITE_REACH, SITE_REACH + 1)
        offsets = sorted(
            ((SITE_PITCH * i, SITE_PITCH * j) for i in reach for j in reach), key=lambda o: (math.hypot(*o), o)
        )
        found = []
        for dx, dy in offsets:
            design = _shift(canonical, bx + dx - 0.5, by + dy - 0.5)
            area = _area(design["entities"])
            if any(_overlaps(area, b["reserved_area"]) for b in blocks.values() if b.get("reserved_area")):
                continue
            if any(_overlaps(area, z) for z in zones):
                continue
            cell_poles = [e for e in design["entities"] if e["name"] == POLE]
            target = min(cell_poles, key=lambda e: min(math.dist(_xy(e), _xy(p)) for p in poles))
            source = min(poles, key=lambda p: math.dist(_xy(p), _xy(target)))
            for horizontal_first in (True, False):
                chain = pole_chain(source["position"], target["position"], horizontal_first=horizontal_first)
                entities = design["entities"] + [e for e in chain if e["position"] != target["position"]]
                tiles = [self.builder._occupied_by_plan([e]) for e in entities]
                if sum(map(len, tiles)) != len(set().union(*tiles)):
                    continue
                if any(_overlaps(_area([e], 0), z) for e in chain for z in zones):
                    continue
                if hardware_conflicts(self.builder, entities, planned):
                    continue
                found.append(dict(design, entities=entities, reserved_area=area))
                break
            if len(found) >= MAX_SITE_CANDIDATES:
                break
        if not found:
            return {"ok": False, "reason": "no_supply_cell_site_off_ore_and_plan_zones"}
        payload = json.dumps(json.dumps(found, separators=(",", ":")))
        surveyed = self.game.query(SURVEY_LUA.replace("PAYLOAD", payload))
        if not isinstance(surveyed, dict) or not surveyed.get("ok"):
            return {"ok": False, "reason": (surveyed or {}).get("reason", "supply_cell_site_survey_failed")}
        chosen, clear = None, None
        legal = _indices(surveyed.get("legal"))
        if legal:
            chosen = found[legal[0] - 1]
        else:
            from .planning import preflight

            for index in _indices(surveyed.get("clearable")):
                check = preflight(self.game, self.builder, obs, found[index - 1]["entities"])
                if check.get("ok"):
                    chosen, clear = found[index - 1], check["clear_obstacles"]
                    break
        if chosen is None:
            return {"ok": False, "reason": "no_clear_supply_cell_site", "candidates": len(found)}
        extra = {"clear_obstacles": clear, "clearance_world_id": obs["world_id"]} if clear else {}
        plan = _plan(
            chosen["entities"],
            chosen["ports"],
            template=TEMPLATE,
            layout_policy="pcb-block-v1",
            reserved_area=chosen["reserved_area"],
            **extra,
        )
        return {
            "ok": True,
            "schema_version": 1,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "products": chosen["products"],
            "plan": plan,
            "phase": "build",
        }

    def _zones(self, obs):
        """Site-plan zone rects (bus band, mining, smelting, corridors, allocations) as areas."""
        if self.site is None:
            return []
        from .site.planmodel import SiteError

        try:
            world = self.site.world(obs["world_id"])
        except SiteError as exc:
            return {"ok": False, "reason": "supply_cell_site_plan_unavailable", "detail": str(exc)[:200]}
        return [[[r.x0, r.y0], [r.x1, r.y1]] for _, r in world.zones()]

    def _publish(self, obs, state):
        board = Board(self.game, self.builder, self.catalog)
        board.refresh(force=True)
        if not board.state:
            board.state = {
                "schema_version": 1,
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "blocks": {},
            }
        if (
            board.state.get("world_id") != obs["world_id"]
            or board.state.get("catalog_fingerprint") != self.catalog.fingerprint
        ):
            return {"status": "blocked", "reason": "supply_cell_board_identity_changed"}
        if board.state["blocks"].get(BLOCK) != state["plan"]:
            board.state["blocks"][BLOCK] = state["plan"]
            _atomic_json(board.path, board.state)
        return None

    # execution
    def step(self, obs, parameters):
        validate_parameters(parameters)
        if parameters.get("retire"):
            return self._retire(obs)
        state = self.state()
        if not state:
            prepared = self.prepare(obs, parameters["products"])
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            prepared.pop("ok")
            state = prepared
            self._save(state)
        if not self._live(obs):
            return {"status": "blocked", "reason": "supply_cell_world_or_catalog_changed"}
        if state.get("phase") in ("retiring", "retired"):
            return {"status": "blocked", "reason": "supply_cell_retired"}
        locked = sorted(self._recipes(state) - _enabled(obs))
        if locked:
            return {"status": "blocked", "reason": "supply_cell_recipes_locked", "missing": locked}
        published = self._publish(obs, state)
        if published:
            return published
        result = self.builder.ensure_plan(obs, state["plan"])
        if result.get("status") != "succeeded":
            return result
        bar = self._bar(obs, state)
        if bar is not None:
            return bar
        evidence = self.proof(obs)
        if evidence is not None:
            state["phase"] = "verified"
            self._save(state)
            return {
                "status": "succeeded",
                "reason": "hand-fed supply cell stocks machine-made items",
                "evidence": evidence,
            }
        feed = self.feed_action(obs, reserve=0, acquire=True, minimum=PROOF_UNITS)
        if feed is not None:
            return feed
        return {"status": "waiting", "reason": "waiting for machine-made products in the supply cell"}

    def _recipes(self, state):
        return {e["recipe"] for e in state["plan"]["entities"] if e.get("recipe")}

    def _bar(self, obs, state):
        from .production_rows import BAR_LUA

        done = set(state.get("bars", []))
        for row in state["products"]:
            key = f"{row['output']['x']},{row['output']['y']}"
            if key in done:
                continue
            name = self._chest_name(state)
            payload = json.dumps(json.dumps({"name": name, "position": row["output"]}))
            native = self.game.query(BAR_LUA.replace("PAYLOAD", payload))
            if not isinstance(native, dict) or not native.get("ok"):
                return {"status": "blocked", **(native if isinstance(native, dict) else {"reason": "bar_query_failed"})}
            if native.get("slots") != OUTPUT_STACKS:
                return {"type": "bar", "name": name, "position": dict(row["output"]), "slots": OUTPUT_STACKS}
            done.add(key)
            state["bars"] = sorted(done)
            self._save(state)
        return None

    def _chest_name(self, state):
        output = state["products"][0]["output"]
        return next(e["name"] for e in state["plan"]["entities"] if e["position"] == output)

    def _find(self, obs, name, position):
        return next(
            (e for e in obs.get("entities", []) if e.get("name") == name and e.get("position") == position), None
        )

    def proof(self, obs):
        """Every product: its machines exist with the recipe and have finished products, its chest holds stock."""
        state = self._live(obs)
        if not state or state.get("phase") == "retired":
            return None
        chest = self._chest_name(state)
        rows = []
        for row in state["products"]:
            machines = [self._find(obs, MACHINE, node["machine"]) for node in row["nodes"]]
            if any(m is None or m.get("products_finished", 0) < 1 for m in machines):
                return None
            if any(m.get("recipe") != node_recipe for m, node_recipe in zip(machines, self._node_recipes(state, row))):
                return None
            box = self._find(obs, chest, row["output"])
            stock = int(((box or {}).get("inventory") or {}).get(row["item"], 0))
            if stock < 1:
                return None
            rows.append(
                {
                    "item": row["item"],
                    "chest_unit": box.get("unit_number"),
                    "chest_stock": stock,
                    "machine_products_finished": [m.get("products_finished") for m in machines],
                }
            )
        return {
            "tick": obs.get("tick"),
            "products": rows,
            "scope": "machine output in the capped chest of each product",
        }

    def _node_recipes(self, state, row):
        by_position = {(e["position"]["x"], e["position"]["y"]): e.get("recipe") for e in state["plan"]["entities"]}
        return [by_position.get((n["machine"]["x"], n["machine"]["y"])) for n in row["nodes"]]

    # feeding
    def _active_products(self, obs, state):
        """Products still fed: no producing permanent row for them."""
        items = []
        for row in state["products"]:
            permanent = self.rows_stock(obs, row["item"]) if self.rows_stock is not None else None
            if permanent is None or permanent.get("status") != "producing":
                items.append(row)
        return items

    def feed_targets(self, obs, state, *, minimum=0, demand=None):
        """``[(deficit, item, chest position)]`` from the demand of each fed product, largest first.

        need = max(minimum, demand - output stock - carried) product units;
        target = min(FEED_CAP, ceil(need x raw per unit)) per input chest and item.
        """
        only = None if demand is None else set(demand)  # an explicit demand feeds only its products
        demand = self.demand(obs) if demand is None else demand
        chest = self._chest_name(state)
        carried = obs.get("inventory") or {}
        rows = []
        for row in self._active_products(obs, state):
            if only is not None and row["item"] not in only:
                continue
            box = self._find(obs, chest, row["output"])
            stock = int(((box or {}).get("inventory") or {}).get(row["item"], 0))
            need = max(minimum, demand.get(row["item"], 0) - stock - carried.get(row["item"], 0))
            for node in row["nodes"]:
                inside = (self._find(obs, chest, node["chest"]) or {}).get("inventory") or {}
                for item, per_unit in node["raw"].items():
                    target = min(FEED_CAP, math.ceil(need * per_unit - 1e-9))
                    deficit = target - int(inside.get(item, 0))
                    if deficit > 0 and deficit >= min(FEED_BATCH, target):
                        rows.append((deficit, item, node["chest"]))
        return sorted(rows, key=lambda r: (-r[0], r[1], r[2]["x"], r[2]["y"]))

    def feed_action(self, obs, *, reserve, acquire=False, collect=False, minimum=0, demand=None):
        """One insert into the hungriest input chest, or the collection that precedes it, or None."""
        state = self._live(obs)
        if not state or state.get("phase") in ("retiring", "retired"):
            return None
        chest = self._chest_name(state)
        for deficit, item, position in self.feed_targets(obs, state, minimum=minimum, demand=demand):
            held = int((obs.get("inventory") or {}).get(item, 0)) - reserve
            if held >= min(deficit, FEED_BATCH):
                return {
                    "type": "insert",
                    "name": chest,
                    "position": dict(position),
                    "inventory": "chest",
                    "item": item,
                    "count": min(deficit, held),
                    "reason": f"feed the supply cell {item}",
                }
            if acquire:
                return self.bootstrap.ensure_item(obs, item, reserve + deficit)
            if collect:
                taken = self.bootstrap._take_output(obs, item, deficit)
                if taken is not None:
                    return taken
        return None

    def upkeep(self, obs):
        """Shared-upkeep feed: only plates beyond FEED_RESERVE in the character's hands."""
        state = self._live(obs)
        if state.get("phase") != "verified":
            return None
        return self.feed_action(obs, reserve=FEED_RESERVE)

    def idle_feed(self, obs):
        """Idle lane: may also collect furnace plates (batched) to feed the cell."""
        state = self._live(obs)
        if state.get("phase") != "verified":
            return None
        return self.feed_action(obs, reserve=0, collect=True)

    # construction stock hook
    def stock(self, obs, item):
        """``machine_stock`` hook result for ``item`` (see ``machine_stock``), or None when the cell does not make it.

        producing: collect, wait above HANDCRAFT_LIMIT. starved (+ ``feed``): no
        plates inside and no recent progress. slow: the time rule prefers
        hand crafting. stalled/broken: hand crafting resumes.
        """
        state = self._live(obs)
        row = next((r for r in state.get("products", []) if r["item"] == item), None)
        if row is None or state.get("phase") == "retired":
            return None
        if state.get("phase") not in ("verified", "retiring"):
            return {"status": "building", "item": item}
        chest = self._chest_name(state)
        box = self._find(obs, chest, row["output"])
        machines = [self._find(obs, MACHINE, node["machine"]) for node in row["nodes"]]
        if box is None or any(m is None for m in machines):
            return {"status": "broken", "item": item, "reason": "supply cell chest or machine missing"}
        stock = int((box.get("inventory") or {}).get(item, 0))
        finished = sum(int(m.get("products_finished") or 0) for m in machines)
        idle = self._idle_ticks(obs, state, finished)
        result = {
            "item": item,
            "stock": stock,
            "chest": {"name": chest, "position": dict(row["output"])},
            # time rule inputs for ensure_item (construction_supply._waiting_pays)
            "wait_seconds_per_unit": round(60.0 / row["units_per_minute"], 4),
            "hand_seconds_per_unit": row["hand_seconds_per_unit"],
            "wait_factor": WAIT_FACTOR,
        }
        if stock > 0:
            return dict(result, status="producing")
        if state.get("phase") == "retiring":
            return dict(result, status="stalled")
        fed = all(
            int(((self._find(obs, chest, node["chest"]) or {}).get("inventory") or {}).get(raw, 0)) >= math.ceil(per)
            for node in row["nodes"]
            for raw, per in node["raw"].items()
        )
        if not fed and idle > PROGRESS_WRITE_TICKS:
            missing = max(PROOF_UNITS, HANDCRAFT_LIMIT + 1)
            feed = self.feed_action(obs, reserve=0, demand={item: missing}, minimum=missing)
            return dict(result, status="starved", **({"feed": feed} if feed else {}))
        if idle > STALL_TICKS:
            return dict(result, status="stalled", ticks_without_progress=idle)
        return dict(result, status="producing", ticks_without_progress=idle)

    def _idle_ticks(self, obs, state, finished):
        tick = int(obs.get("tick") or 0)
        seen = state.get("progress") or {}
        key = ",".join(r["item"] for r in state["products"])
        last = seen.get(key)
        if last is None or finished > last["finished"]:
            if last is None or tick - last.get("written", -PROGRESS_WRITE_TICKS) >= PROGRESS_WRITE_TICKS:
                state.setdefault("progress", {})[key] = {"finished": finished, "tick": tick, "written": tick}
                self._save(state)
            return 0
        return max(0, tick - last["tick"])

    # retirement
    def _retire(self, obs):
        state = self.state()
        if not state:
            return {"status": "succeeded", "reason": "no supply cell to retire", "evidence": {"tick": obs.get("tick")}}
        if not self._live(obs):
            return {"status": "blocked", "reason": "supply_cell_world_or_catalog_changed"}
        if state.get("phase") != "retired":
            if state.get("phase") != "retiring":
                state["phase"] = "retiring"  # feeding stops now
                self._save(state)
            products = {r["item"] for r in state["products"]}
            dependent = [
                job.get("key")
                for job in self._jobs(obs)
                if job.get("capability") != CAPABILITY
                and job.get("status") in ("running", "waiting", "pending")
                and any(f"machine-made {p}" in str(job.get("reason", "")) for p in products)
            ]
            if dependent:
                return {
                    "status": "waiting",
                    "reason": "supply_cell_retire_waits_for_dependent_jobs",
                    "evidence": {"jobs": dependent},
                }
            for e in self._demolition_order(state):
                live = self._find(obs, e["name"], e["position"])
                if live is None or type(live.get("unit_number")) is not int:
                    continue
                return {
                    "type": "mine",
                    "name": e["name"],
                    "position": dict(e["position"]),
                    "count": 1,
                    "expected_entity_unit": live["unit_number"],
                    "expected_entity_world_id": obs["world_id"],
                    "reason": "demolish the retired temporary supply cell",
                }
            board = Board(self.game, self.builder, self.catalog)
            board.refresh(force=True)
            if board.state.get("world_id") == obs["world_id"] and BLOCK in board.state.get("blocks", {}):
                del board.state["blocks"][BLOCK]
                _atomic_json(board.path, board.state)
            state["phase"] = "retired"
            state["retired_tick"] = obs.get("tick")
            self._save(state)
        evidence = self.retire_proof(obs)
        if evidence is None:
            return {"status": "waiting", "reason": "supply_cell_demolition_not_observed_yet"}
        return {
            "status": "succeeded",
            "reason": "temporary supply cell demolished and its block released",
            "evidence": evidence,
        }

    @staticmethod
    def _demolition_order(state):
        rank = {INSERTER: 0, MACHINE: 1}
        inside = [e for e in state["plan"]["entities"] if e["name"] != POLE]
        return sorted(inside, key=lambda e: rank.get(e["name"], 2))

    def retire_proof(self, obs):
        state = self.state()
        if not state:
            return {"tick": obs.get("tick"), "retired": "never built"}
        if state.get("phase") != "retired" or state.get("world_id") != obs.get("world_id"):
            return None
        left = [e["name"] for e in self._demolition_order(state) if self._find(obs, e["name"], e["position"])]
        board = Board(self.game, self.builder, self.catalog)
        if left or BLOCK in board.state.get("blocks", {}):
            return None
        return {
            "tick": obs.get("tick"),
            "retired_tick": state.get("retired_tick"),
            "products": [r["item"] for r in state["products"]],
        }


def chain_stock(rows_hook, cell_hook):
    """The ``machine_stock`` hook: a producing ``produce`` row wins, else the supply cell, else the row answer."""

    def hook(observation, item):
        row = rows_hook(observation, item) if rows_hook is not None else None
        if row is not None and row.get("status") == "producing":
            return row
        cell = cell_hook(observation, item)
        return cell if cell is not None else row

    return hook


def validate_parameters(parameters):
    """``{"products": [1..6 distinct item names]}`` to build, ``{"retire": true}`` to demolish."""
    if parameters == {"retire": True}:
        return
    if not isinstance(parameters, dict) or set(parameters) != {"products"}:
        raise ValueError("supply_cell takes products (list of items) or retire: true")
    products = parameters["products"]
    if (
        not isinstance(products, list)
        or not 1 <= len(products) <= 6
        or len(set(products)) != len(products)
        or not all(isinstance(p, str) and 0 < len(p) <= 64 for p in products)
    ):
        raise ValueError("supply_cell products must be 1..6 distinct item names")


# ----- planner -------------------------------------------------------------
def _rows(jobs, queue_nodes):
    return [r for r in list(jobs) + list(queue_nodes) if r.get("capability") == CAPABILITY]


def supply_cell_nodes(jobs, queue_nodes, observation, capabilities):
    """Build once when products qualify; retire once when every product's demand ended or a produce row took over."""
    if CAPABILITY not in capabilities:
        return []
    jobs = list(jobs)
    succeeded = {job.get("capability") for job in jobs if job.get("status") == "succeeded"}
    if not set(capabilities[CAPABILITY].get("requires", [])) <= succeeded:
        return []
    found = observation.get("supply_cell_candidates")
    if not isinstance(found, dict):
        return []
    mine = _rows(jobs, queue_nodes)
    if not mine:
        if MACHINE not in _enabled(observation) or not found.get("products"):
            return []
        products = list(found["products"])[:6]
        rows = found.get("rows") or {}
        note = ", ".join(f"{p} {rows.get(p, {}).get('count', '?')}" for p in products)
        return [
            {
                "id": NODE_ID,
                "capability": CAPABILITY,
                "parameters": {"products": products},
                "priority": BUILD_PRIORITY,
                "depends_on": [],
                "reason": f"temporary hand-fed supply cell for upcoming hand crafts: {note}"[:300],
            }
        ]
    if any((r.get("parameters") or {}).get("retire") for r in mine):
        return []
    built = [j for j in jobs if j.get("capability") == CAPABILITY and j.get("status") == "succeeded"]
    if not built:
        return []
    products = (built[0].get("parameters") or {}).get("products") or []
    demand = found.get("demand") or {}
    permanent = {
        (j.get("parameters") or {}).get("item")
        for j in jobs
        if j.get("capability") == "produce" and j.get("status") == "succeeded"
    }
    if not all(demand.get(p, 0) <= HANDCRAFT_LIMIT or p in permanent for p in products):
        return []
    return [
        {
            "id": RETIRE_NODE_ID,
            "capability": CAPABILITY,
            "parameters": {"retire": True},
            "priority": RETIRE_PRIORITY,
            "depends_on": [],
            "reason": "supply cell products have a permanent source or no remaining demand: drain and demolish",
        }
    ]
