"""The last three roadmap stages: ``rocket_silo``, then ``rocket_parts``, then ``rocket_launch``.

Shared here: the silo facts from the catalog (``silo_spec``), the stage supply
(``StageSupply``: collect an item's hand-craft leaves from published stock, wait
with a named item and producer, then one normal craft), the ``rocket_stages``
observe receipt that gates the autopilot (``receipt``), and the ``rocket_silo``
controller (``RocketSilo``). ``rocket_feed`` holds ``rocket_parts`` and
``rocket_launch``.

``rocket_silo`` (``RocketSilo``):

1. site: ``SiteService.allocate_candidates("production", 13, 13)``: the 9x9
   silo in the middle of a reserved ring of ``RING`` tiles (stage-ii feed:
   inserter row, belt ports), turned so the feed side faces the block's tap
   band (``site.fit.orientations``); a corner pole bridged to the grid
   (``manufacturing.connect_power_groups``), ``planning.preflight`` (trees are
   cleared, ore and water rejected). State and the board block are saved before
   the first action.
2. power: the grid's measured headroom (``POWER_LUA``) must carry the silo's
   crafting load plus a reserve; the launch peak (prototype
   ``active_energy_usage``) is reported. Short: the job waits
   ``rocket_silo_power_headroom_short`` with the shortfall (no capability can
   grow the plant again today: ``steam_expansion`` adds one pair once).
3. supply: the silo item's hand-craft leaves (steel, processing units, engines,
   concrete, plates for pipes) are collected from published stock in batches
   (``V2Bootstrap.ensure_item``: ``take_many`` / ``take``); a leaf without stock
   is a named wait (``rocket_silo_waits_for_<item>``, evidence: item, missing,
   producer capability and its status); then one normal hand craft.
4. build: ``builder.ensure_plan`` places pole, bridge and silo (paid items).
   The silo's recipe is fixed (prototype ``fixed_recipe``): no recipe action.
5. proof: the silo stands at the planned position, is connected and charged,
   and its live recipe is the part recipe.

No body here writes ``rocket_parts``, a silo status or an item count: the
only mutations are the ordinary paid actions of ``DeterministicGame.act``.
"""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
import re

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json

from . import plan_needs, remedy, rocket_plan
from .board import entity
from .manufacturing import connect_power_groups
from .planning import overlap, preflight
from .production_site import foreign_keepouts
from .site.fit import orientations
from .site.placement import block_wants, settle
from .site.transform import apply as apply_transform
from .smelting import merge_entities

STATE_FILE = "rocket-silo.json"
BLOCK = "rocket_silo"
LAYOUT_POLICY = "rocket-silo-v1"
SILO_TILES = 9
RING = 2  # reserved tiles around the silo: inserter row + belt port row (stage-ii feed)
RING_MIN = 1  # hard minimum of the relax rung (inserters inside, belt ports just outside)
CANDIDATES = 4
CANDIDATES_MAX = 12  # hard maximum of the site rung
POLE = "small-electric-pole"
POWER_RESERVE_WATTS = 300000.0
POWER_RESERVE_SHARE = 0.2
POWER_RECHECK_TICKS = 3600
FEED_COLUMNS = (3, 5, 7)  # canonical feed inserter columns (tiles from the block corner)
FEED_POLE_COLUMN = 6
STAGES = ("rocket_silo", "rocket_parts", "rocket_launch")


def _snake(item: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(item).lower()).strip("_")[:60]


def _ingredients(recipe: dict) -> dict[str, float]:
    return {
        row["name"]: float(row["amount"])
        for row in recipe.get("ingredients") or ()
        if row.get("type", "item") == "item"
    }


def silo_spec(catalog) -> dict:
    """The silo, its placing item and recipes, the part recipe and the payload, from the catalog."""
    name, row = rocket_plan.silo_entity(catalog)
    item = rocket_plan.silo_item(catalog, row)
    part = rocket_plan.part_recipe(catalog, row)
    silo_recipe = catalog.recipe_for_product(item)
    payload = rocket_plan.launch_payload()
    usage = row.get("energy_usage")
    return {
        "entity": name,
        "item": item,
        "parts_required": int(row["rocket_parts_required"]),
        "part_recipe": part["name"],
        "part_ingredients": _ingredients(part),
        "silo_recipe": (silo_recipe or {}).get("name"),
        "silo_ingredients": _ingredients(silo_recipe or {}),
        "payload": payload,
        "payload_recipe": (catalog.recipe_for_product(payload) or {}).get("name") if payload else None,
        "crafting_watts": float(usage) * 60 if isinstance(usage, (int, float)) and usage > 0 else 0.0,
    }


# ----- producers and stock ------------------------------------------------------------------
_PRODUCERS: dict[tuple, dict] = {}


def producer_of(catalog, item: str) -> dict:
    """``{"capability", "status"}`` of the block that makes ``item`` (``rocket_plan``'s classification)."""
    key = (getattr(catalog, "fingerprint", None), item)
    if key in _PRODUCERS:
        return _PRODUCERS[key]
    from .jobs import CAPABILITIES

    try:
        recipe = catalog.recipe_for_product(item)
        if recipe is None:
            row = rocket_plan._raw_producer(catalog, ("item", item))
        else:
            declared = rocket_plan._capability_products(catalog)
            row = declared.get(recipe["name"]) or rocket_plan._generic_producer(catalog, recipe, ("item", item), set())
        status = rocket_plan._status(row["capability"], row.get("parameters") or {}, CAPABILITIES, row.get("module"))
        found = {"capability": row["capability"], "status": status}
    except Exception as exc:  # noqa: BLE001 - a missing classification only weakens the wait reason
        found = {"capability": "unknown", "status": f"unclassified: {type(exc).__name__}"[:60]}
    if len(_PRODUCERS) > 512:
        _PRODUCERS.clear()
    _PRODUCERS[key] = found
    return found


def published_ports(board: dict | None, item: str) -> list[str]:
    """Board blocks with an output port (belt or stock chest) carrying ``item``."""
    found = []
    for name, block in ((board or {}).get("blocks") or {}).items():
        if not isinstance(block, dict):
            continue
        if any(p.get("direction") == "output" and p.get("item") == item for p in block.get("ports") or ()):
            found.append(name)
    bus = (board or {}).get("shared_main_bus")
    if isinstance(bus, dict) and any(t.get("item") == item for t in bus.get("trunks") or () if isinstance(t, dict)):
        found.append("shared_main_bus")
    return found


def _handcraft_recipe(catalog, obs, item: str) -> dict | None:
    """The enabled, solid, hand-craftable recipe of ``item`` with a whole yield, else None."""
    recipe = catalog.recipe_for_product(item)
    if recipe is None or not (obs.get("enabled_recipes") or {}).get(recipe["name"]):
        return None
    hand = set((catalog.entities.get("character") or {}).get("crafting_categories") or ())
    if not hand & set(recipe.get("categories") or ()):
        return None
    if any(r.get("type", "item") != "item" for r in list(recipe["ingredients"]) + list(recipe["products"])):
        return None
    out = sum(float(p.get("amount") or 0) for p in recipe["products"] if p["name"] == item)
    if out <= 0 or not float(out).is_integer():
        return None
    return recipe


class StageSupply:
    """Collect, wait for or craft one stage item (module doc, step 3)."""

    def __init__(self, bootstrap, catalog, root: Path):
        self.bootstrap, self.catalog, self.root = bootstrap, catalog, Path(root)

    def board(self, obs) -> dict | None:
        path = self.root / "pcb-board.json"
        if not path.exists():
            return None
        try:
            board = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if board.get("world_id") != obs.get("world_id"):
            return None
        return board

    def stock(self, obs, item: str) -> int:
        """Units of ``item`` in stock others may collect (published output ports, unowned chests)."""
        from .construction_supply import CHEST_NAMES, OUTPUT_NAMES

        try:
            view = self.bootstrap._unreserved_stock_observation(obs, item)
        except (ValueError, OSError, KeyError, TypeError):
            return 0
        total = 0
        for e in view.get("entities", []):
            name = e.get("name")
            if name in CHEST_NAMES:
                total += int((e.get("inventory") or {}).get(item, 0))
            elif name in OUTPUT_NAMES:
                total += int((e.get("output_inventory") or {}).get(item, 0))
        return total

    def walk(self, obs, item: str, count: int, board=None, *, collect_top: bool = False) -> dict:
        """Leaves of ``item`` x ``count``: ``held``, ``collect``, ``missing`` (walk order) per item.

        ``collect_top``: ``item`` itself may come from stock (a fed part ingredient); otherwise it is crafted.
        """
        from .construction_supply import HANDCRAFT_LIMIT

        held_left = Counter({k: int(v) for k, v in (obs.get("inventory") or {}).items() if isinstance(v, (int, float))})
        stock_left: dict[str, int] = {}
        held, collect, missing = Counter(), Counter(), Counter()
        order: list[str] = []

        def visit(name: str, units: int, depth: int) -> None:
            if units <= 0:
                return
            if name not in order:
                order.append(name)
            take = min(units, held_left[name])
            held_left[name] -= take
            held[name] += take
            units -= take
            if units <= 0:
                return
            stocked = depth > 0 or collect_top
            if stocked:
                if name not in stock_left:
                    stock_left[name] = self.stock(obs, name)
                got = min(units, stock_left[name])
                stock_left[name] -= got
                collect[name] += got
                units -= got
                if units <= 0:
                    return
            recipe = _handcraft_recipe(self.catalog, obs, name) if depth < 16 else None
            # Hand crafting stops above HANDCRAFT_LIMIT where a row publishes the item (AGENTS.md).
            row = stocked and bool(published_ports(board, name))
            if recipe is None or (row and units > HANDCRAFT_LIMIT):
                missing[name] += units
                return
            out = sum(float(p["amount"]) for p in recipe["products"] if p["name"] == name)
            runs = math.ceil(units / out)
            for ingredient in recipe["ingredients"]:
                visit(ingredient["name"], math.ceil(float(ingredient["amount"]) * runs), depth + 1)

        visit(item, int(count), 0)
        return {"order": order, "held": dict(held), "collect": dict(collect), "missing": dict(missing)}

    def next(self, obs, item: str, count: int, *, stage: str, collect_top: bool = False) -> dict:
        """One collect/craft action, or a named wait (module doc, step 3)."""
        if int((obs.get("inventory") or {}).get(item, 0)) >= count:
            return {"status": "succeeded", "reason": f"{item} held"}
        board = self.board(obs)
        walk = self.walk(obs, item, count, board, collect_top=collect_top)
        needs = {
            k: walk["held"].get(k, 0) + walk["collect"].get(k, 0) + walk["missing"].get(k, 0) for k in walk["order"]
        }
        needs = {k: v for k, v in needs.items() if v > 0 and (k != item or collect_top)}
        waits = []
        for name in walk["order"]:
            if walk["collect"].get(name):
                target = int((obs.get("inventory") or {}).get(name, 0)) + walk["collect"][name]
                result = self.bootstrap.ensure_item(obs, name, target)
                if isinstance(result, dict) and result.get("type"):
                    return result
                waits.append((name, result))
        for name in walk["order"]:
            if walk["missing"].get(name):
                producer = producer_of(self.catalog, name)
                ports = published_ports(board, name)
                return {
                    "status": "waiting",
                    "reason": f"{stage}_waits_for_{_snake(name)}",
                    "evidence": {
                        "item": name,
                        "need": walk["missing"][name],
                        "held": int((obs.get("inventory") or {}).get(name, 0)),
                        "producer": producer["capability"],
                        "producer_status": "published" if ports else producer["status"],
                        "producer_blocks": ports[:4],
                        "needs": needs,
                        "for": f"{count} {item}",
                    },
                }
        if waits:
            name, result = waits[0]
            return {
                "status": "waiting",
                "reason": f"{stage}_collecting_{_snake(name)}",
                "evidence": {"item": name, "detail": str((result or {}).get("reason", ""))[:160], "needs": needs},
            }
        result = self.bootstrap.ensure_item(obs, item, count)
        if isinstance(result, dict) and result.get("type"):
            return result
        return {
            "status": "waiting",
            "reason": f"{stage}_crafting_{_snake(item)}",
            "evidence": {"item": item, "detail": str((result or {}).get("reason", ""))[:160], "needs": needs},
        }


# ----- observe receipt: are a stage's inputs produced anywhere? --------------------------------
def stage_items(spec: dict) -> dict[str, dict[str, int]]:
    """What each stage consumes: the silo item, the parts for one rocket, the payload."""
    parts = spec["parts_required"]
    return {
        "rocket_silo": {spec["item"]: 1},
        "rocket_parts": {k: math.ceil(v * parts) for k, v in spec["part_ingredients"].items()},
        "rocket_launch": {spec["payload"]: 1} if spec.get("payload") else {},
    }


CRAFTED_STAGES = ("rocket_silo", "rocket_launch")  # their item is hand-crafted; the parts' inputs are fed


def inputs_ready(catalog, obs, board, items: dict, *, crafted: bool = True) -> dict:
    """``{"ready", "missing": [{item, producer, status}]}``: every hand-craft leaf is held or published.

    ``crafted``: the listed items are crafted by the stage itself (their own stock is not looked at).
    """
    missing = []
    held = {k: int(v) for k, v in (obs.get("inventory") or {}).items() if isinstance(v, (int, float))}
    seen = set()

    def visit(name: str, units: int, depth: int) -> None:
        if name in seen or units <= held.get(name, 0):
            return
        seen.add(name)
        if depth > 0 and published_ports(board, name):
            return
        recipe = _handcraft_recipe(catalog, obs, name) if depth < 16 else None
        if recipe is None:
            producer = producer_of(catalog, name)
            missing.append({"item": name, "producer": producer["capability"], "status": producer["status"]})
            return
        out = sum(float(p["amount"]) for p in recipe["products"] if p["name"] == name)
        runs = math.ceil(units / out)
        for ingredient in recipe["ingredients"]:
            visit(ingredient["name"], math.ceil(float(ingredient["amount"]) * runs), depth + 1)

    for name, units in items.items():
        visit(name, units, 0 if crafted else 1)
    return {"ready": not missing, "missing": missing[:6]}


def receipt(root, obs, catalog) -> dict:
    """``rocket_stages`` observe receipt: per stage ``ready`` / ``missing`` (``progression`` gate)."""
    root = Path(root)
    spec = silo_spec(catalog)
    path = root / "pcb-board.json"
    board = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if board.get("world_id") not in (None, obs.get("world_id")):
        board = {}
    out = {}
    for stage, items in stage_items(spec).items():
        out[stage] = inputs_ready(catalog, obs, board, items, crafted=stage in CRAFTED_STAGES)
    state_path = root / STATE_FILE
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        power = state.get("power") if state.get("world_id") == obs.get("world_id") else None
        if isinstance(power, dict) and not power.get("ok"):
            out["rocket_silo"]["power_short_watts"] = power.get("short_watts")
    return out


# ----- native bodies -----------------------------------------------------------------------
# Read-only: the grid's offered and requested power (one line, no comments; see deterministic_game).
POWER_LUA = r"""
local x=helpers.json_to_table(PAYLOAD)
local e=target(x.position,x.name)
if not e or not e.valid or e.force~=f then return {ok=false,reason="rocket_power_source_missing"} end
local sub=e.electric_network
local net=sub and sub.parent_network
if not net or not net.valid then return {ok=false,reason="rocket_power_network_unavailable"} end
local flow=net.flow_last_tick
local offered=(flow.primary_output+flow.secondary_output+flow.solar_output)*60
local demand=(flow.primary_demand+flow.secondary_demand)*60
local peak=0
local proto=prototypes.entity[x.silo]
if proto then
 local good,value=pcall(function() return proto.active_energy_usage end)
 if good and type(value)=="number" then peak=value*60 end
end
return {ok=true,offered_watts=offered,demand_watts=demand,headroom_watts=offered-demand,launch_peak_watts=peak,
 satisfaction=flow.consumption_satisfaction,tick=game.tick}
"""


def power_payload(lua: str, args: dict) -> str:
    return lua.replace("PAYLOAD", json.dumps(json.dumps(args, separators=(",", ":"))))


# ----- the silo ------------------------------------------------------------------------------
def silo_layout(spec: dict, x0: int, y0: int, ring: int = RING) -> dict:
    """Canonical block: the silo in a ``ring``-tile reserved border, feed side north (module doc)."""
    size = SILO_TILES + 2 * ring
    centre = x0 + ring + SILO_TILES / 2
    silo = entity(spec["entity"], centre, y0 + ring + SILO_TILES / 2)
    inner = y0 + ring - 0.5  # row next to the silo: pole and feed inserters (pick up north)
    pole = entity(POLE, x0 + ring - 0.5, inner)
    offset = ring - RING
    items = sorted(spec["part_ingredients"])
    inserters = [entity("inserter", x0 + c + offset + 0.5, inner, 0) for c in FEED_COLUMNS[: len(items)]]
    feed_pole = entity(POLE, x0 + FEED_POLE_COLUMN + offset + 0.5, inner)
    ports = [
        {"item": item, "position": {"x": e["position"]["x"], "y": inner - 1}, "facing": 8, "direction": "input"}
        for item, e in zip(items, inserters)
    ]
    return {
        "entities": [pole, silo],
        "silo": silo,
        "pole": pole,
        "feed": {"entities": inserters + [feed_pole], "ports": ports},
        "feed_ports": ports,
        "reserved_area": [[x0, y0], [x0 + size, y0 + size]],
        "ring": ring,
    }


def live(obs, wanted: dict) -> dict | None:
    p = wanted["position"]
    for e in obs.get("entities", []):
        if e.get("name") == wanted["name"] and (e.get("position") or {}) == {"x": p["x"], "y": p["y"]}:
            return e
    return None


def _pole_source(obs) -> dict | None:
    networks = {
        e.get("electric_network_id")
        for e in obs.get("entities", [])
        if e.get("name") == "steam-engine" and e.get("energy", 0) > 0
    }
    poles = sorted(
        (e for e in obs.get("entities", []) if e.get("name") == POLE and e.get("electric_network_id") in networks),
        key=lambda e: e.get("unit_number") or 0,
    )
    return poles[0] if poles else None


class RocketSilo:
    """``rocket_silo`` (module doc). State: ``rocket-silo.json``; board block ``rocket_silo``."""

    def __init__(self, game, builder, bootstrap, catalog, *, site=None):
        self.game, self.builder, self.bootstrap, self.catalog, self.site = game, builder, bootstrap, catalog, site
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE
        self.supply = StageSupply(bootstrap, catalog, self.root)
        self.last_proof_failure = None

    def state(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def save(self, state: dict) -> None:
        _atomic_json(self.path, state)

    def spec(self) -> dict:
        return silo_spec(self.catalog)

    # ----- planning (pure: reads only, site allocations aside) -------------------------
    def prepare(self, obs, parameters=None) -> dict:
        spec = self.spec()
        board = self.supply.board(obs)
        if board is None or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"ok": False, "reason": "rocket_silo_board_identity_changed"}
        if self.site is None:
            return {"ok": False, "reason": "rocket_silo_requires_site_service"}
        ring = remedy.option("rocket_silo_ring", RING)
        ring = max(RING_MIN, min(RING, int(ring))) if isinstance(ring, (int, float)) else RING
        count = remedy.option("rocket_silo_candidates", CANDIDATES)
        count = max(1, min(CANDIDATES_MAX, int(count))) if isinstance(count, (int, float)) else CANDIDATES
        size = SILO_TILES + 2 * ring
        try:
            blocks = self.site.allocate_candidates(
                "production", size, size, name=BLOCK, count=count, returns_to_bus=False, world_id=obs["world_id"]
            )
        except Exception as exc:  # noqa: BLE001 - the allocator reports, never crashes the job
            return {"ok": False, "reason": "rocket_silo_site_allocation_failed", "detail": str(exc)[:200]}

        def build(x0, y0):
            return silo_layout(spec, x0, y0, ring)

        keepouts = foreign_keepouts(self.builder, obs, board, BLOCK)
        failures = []
        for block in blocks:
            wants = block_wants(block, lambda plan: plan["feed_ports"], lambda plan: [])
            for placement in orientations(build, (size, size), block.rect, wants, allowed=lambda t: t.mirror is None)[
                :2
            ]:
                layout = placement.plan
                if any(
                    overlap(layout["reserved_area"], b["reserved_area"])
                    for name, b in board.get("blocks", {}).items()
                    if name != BLOCK and isinstance(b, dict) and "reserved_area" in b
                ):
                    failures.append({"allocation": block.name, "reason": "board_reservation_overlap"})
                    continue
                reserved = (
                    layout["entities"]
                    + layout["feed"]["entities"]
                    + [
                        entity("transport-belt", p["position"]["x"], p["position"]["y"], p["facing"])
                        for p in layout["feed_ports"]
                    ]
                )
                power = connect_power_groups(
                    self.game, self.builder, obs, [{"entities": [layout["pole"]]}], reserved + keepouts
                )
                if not power.get("ok"):
                    failures.append({"allocation": block.name, "reason": power.get("reason", "power_bridge_failed")})
                    continue
                entities = merge_entities(power["entities"], layout["entities"])
                check = preflight(self.game, self.builder, obs, entities)
                if not check.get("ok"):
                    failures.append({"allocation": block.name, "reason": check.get("reason", "preflight_failed")})
                    continue
                plan = _plan(
                    entities,
                    [],
                    reserved_area=layout["reserved_area"],
                    clear_obstacles=list(check.get("clear_obstacles") or []),
                    clearance_world_id=obs["world_id"],
                    layout_policy=LAYOUT_POLICY,
                    product=spec["item"],
                )
                settle(self.site, obs["world_id"], BLOCK, block.name)
                return {
                    "ok": True,
                    "world_id": obs["world_id"],
                    "catalog_fingerprint": self.catalog.fingerprint,
                    "allocation": block.name,
                    "transform": placement.transform.to_dict(),
                    "layout": layout,
                    "canonical": apply_transform(placement.transform.inverse(), layout),
                    "plan": plan,
                    "spec": spec,
                    "phase": "supply",
                }
        settle(self.site, obs["world_id"], BLOCK, None)
        rejected = Counter(str(f.get("reason")) for f in failures)
        reason = "no_rocket_silo_site" if not blocks else "no_rocket_silo_site_with_power_and_preflight"
        return {
            "ok": False,
            "reason": reason,
            "candidates": len(blocks),
            "failures": failures[-6:],
            "plan_needs": plan_needs.make(
                "site",
                reason,
                block=BLOCK,
                tried=len(blocks),
                rejected_by=dict(rejected),
                size=f"{size}x{size}",
                change=f"a free {size}x{size} production block with a pole chain and an ore-free footprint",
            ),
        }

    # ----- execution ------------------------------------------------------------------
    def _publish(self, obs, state) -> dict | None:
        board = self.supply.board(obs)
        if board is None or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "rocket_silo_board_identity_changed"}
        block = dict(state["plan"])
        feed = state.get("feed_plan")
        if feed:
            block = dict(block, entities=merge_entities(block["entities"], feed["entities"]), ports=feed["ports"])
        changed = board["blocks"].get(BLOCK) != block
        if changed:
            board["blocks"][BLOCK] = block
        for connection in (feed or {}).get("connections", []):
            if connection not in board.setdefault("connections", []):
                board["connections"].append(connection)
                changed = True
        if changed:
            _atomic_json(self.root / "pcb-board.json", board)
        return None

    def _power(self, obs, state) -> dict | None:
        """None when the grid carries the silo; else a wait naming the shortfall (module doc, step 2)."""
        saved = state.get("power")
        tick = obs.get("tick", 0)
        if isinstance(saved, dict) and saved.get("ok"):
            return None
        if isinstance(saved, dict) and 0 <= tick - saved.get("tick", -POWER_RECHECK_TICKS) < POWER_RECHECK_TICKS:
            return {"status": "waiting", "reason": "rocket_silo_power_headroom_short", "evidence": saved}
        source = _pole_source(obs)
        if source is None:
            return {"status": "waiting", "reason": "rocket_silo_waits_for_a_supplied_grid"}
        native = self.game.query(
            power_payload(
                POWER_LUA, {"position": source["position"], "name": source["name"], "silo": state["spec"]["entity"]}
            )
        )
        if not isinstance(native, dict) or not native.get("ok"):
            return {"status": "waiting", "reason": "rocket_silo_power_query_failed", "evidence": dict(native or {})}
        offered = float(native.get("offered_watts") or 0)
        need = state["spec"]["crafting_watts"] + max(POWER_RESERVE_WATTS, offered * POWER_RESERVE_SHARE)
        headroom = float(native.get("headroom_watts") or 0)
        row = {
            "ok": headroom >= need,
            "tick": tick,
            "headroom_watts": round(headroom),
            "needed_watts": round(need),
            "short_watts": max(0, round(need - headroom)),
            "launch_peak_watts": round(float(native.get("launch_peak_watts") or 0)),
            "launch_peak_covered": headroom >= float(native.get("launch_peak_watts") or 0),
        }
        state["power"] = row
        self.save(state)
        if row["ok"]:
            return None
        return {"status": "waiting", "reason": "rocket_silo_power_headroom_short", "evidence": row}

    def step(self, obs, parameters=None) -> dict:
        state = self.state()
        if not state:
            prepared = self.prepare(obs, parameters)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            state = prepared
            self.save(state)
        if state["world_id"] != obs["world_id"] or state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "rocket_silo_world_or_catalog_changed"}
        published = self._publish(obs, state)
        if published is not None:
            return published
        spec = state["spec"]
        silo = live(obs, state["layout"]["silo"])
        if silo is None:
            if not (obs.get("enabled_recipes") or {}).get(spec["silo_recipe"]):
                return {"status": "blocked", "reason": "rocket_silo_recipe_locked", "recipe": spec["silo_recipe"]}
            power = self._power(obs, state)
            if power is not None:
                return power
            if int((obs.get("inventory") or {}).get(spec["item"], 0)) < 1:
                return self.supply.next(obs, spec["item"], 1, stage="rocket_silo")
        result = self.builder.ensure_plan(obs, state["plan"])
        if result.get("status") != "succeeded":
            return result
        evidence = self.proof(obs, parameters)
        if evidence is None:
            failure = self.last_proof_failure or {}
            if failure.get("reason") == "rocket_silo_recipe_missing" and silo is not None:
                return {
                    "type": "recipe",
                    "name": spec["entity"],
                    "position": state["layout"]["silo"]["position"],
                    "recipe": spec["part_recipe"],
                }
            return {"status": "waiting", "reason": "rocket_silo_waiting_for_power", "evidence": failure}
        state["phase"] = "verified"
        self.save(state)
        return {
            "status": "succeeded",
            "reason": "rocket silo placed, powered, building rocket parts",
            "evidence": evidence,
        }

    def proof(self, obs, parameters=None) -> dict | None:
        self.last_proof_failure = None
        state = self.state()
        if not state or state.get("world_id") != obs.get("world_id"):
            self.last_proof_failure = {"reason": "rocket_silo_state_missing"}
            return None
        silo = live(obs, state["layout"]["silo"])
        if silo is None:
            self.last_proof_failure = {"reason": "rocket_silo_missing"}
            return None
        if not silo.get("electric_network_connected") or (silo.get("energy") or 0) <= 0:
            self.last_proof_failure = {"reason": "rocket_silo_unpowered"}
            return None
        if silo.get("recipe") != state["spec"]["part_recipe"]:
            self.last_proof_failure = {"reason": "rocket_silo_recipe_missing", "recipe": silo.get("recipe")}
            return None
        return {
            "tick": obs["tick"],
            "silo_unit": silo.get("unit_number"),
            "position": silo.get("position"),
            "recipe": silo.get("recipe"),
            "energy": silo.get("energy"),
            "electric_network_id": silo.get("electric_network_id"),
            "rocket_silo_status": silo.get("rocket_silo_status"),
            "power": state.get("power"),
            "scope": "silo standing, connected and charged, crafting rocket parts",
        }

    def silo(self, obs) -> tuple[dict | None, dict]:
        """The live silo of the saved plan and the state (``rocket_feed``)."""
        state = self.state()
        if not state or state.get("world_id") != obs.get("world_id"):
            return None, state
        return live(obs, state["layout"]["silo"]), state
