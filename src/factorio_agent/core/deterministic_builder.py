"""Observation-driven, resource-backed block construction and starter power.

Plans persist per world, but construction and completion are always reobserved.
The builder returns one ordinary game action or an explicit status report. It
never grants items, researches technologies, or treats placement as production.
"""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
from typing import Any

from .deterministic_game import BUILD_BATCH_LIMIT, BUILD_BATCH_NAMES
from .deterministic_state import _atomic_json, stop_requested
from .factory_templates import COAL_ENTRIES, DIRECTIONS, build_template, coal_entry_detour, route_orthogonal
from .deterministic_underground import underground_fields


def _report(status: str, reason: str, **evidence: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence}


def _reserved_view(observation: dict, reserve: Counter) -> dict:
    """``observation`` with ``reserve`` units taken out of the carried inventory (build-ahead view)."""
    if not reserve:
        return observation
    inventory = dict(observation.get("inventory") or {})
    for name, units in reserve.items():
        if name in inventory:
            inventory[name] = max(0, int(inventory[name]) - int(units))
    return dict(observation, inventory=inventory)


def power_flow_ready(evidence: dict[str, Any]) -> bool:
    return bool(
        evidence.get("ok")
        and evidence.get("water", 0) > 0
        and evidence.get("steam", 0) > 0
        and evidence.get("boiler_fuel", 0) > 0
        and (evidence.get("drill_fuel", 0) > 0 or evidence.get("drill_powered") is True)
        and evidence.get("coal_on_belts", 0) > 0
        and evidence.get("connected_engines") == 2
    )


# Two-phase starter power (docs/v2/PLANNERS.md "Starter power in two phases"):
# ``power`` builds the plant and runs it on a hand-fed boiler; the later
# ``power_coal_feed`` step builds the dedicated coal drill and belt. The saved
# ``power_mode`` says which phase owns the boiler's fuel.
HAND_FED = "hand_fed"
BELT_FED = "belt_fed"
COAL_FEED_REPLANS = 2  # new routes ensure_coal_feed may choose when the reserved one got built over
# ensure_plan reasons meaning another owner's entity stands on an unbuilt feed's tile.
COAL_FEED_TAKEN_REASONS = (
    "reserved block placement is obstructed",
    "existing entity direction differs from reserved plan",
    "existing underground endpoint differs from reserved plan",
)
# Resource-policy refusals of a saved, wholly unbuilt power plan: it is dropped and planned again.
POWER_REPLAN_REASONS = (
    "new_entity_over_resource_or_mining_reservation",
    "new_underground_tunnel_over_resource_or_mining_reservation",
)
# planning.preflight reasons meaning the same (resource-policy reasons are not a taken tile).
COAL_FEED_PREFLIGHT_TAKEN = (
    "block_protected_entity_obstruction",
    "block_terrain_or_immovable_obstruction",
    "owned_block_entity_configuration_changed",
)
# Tiles (relative to a north-facing entity, before its rotation) where a pipe joins a steam
# box: a water pipe there mixes water into steam and the plant never runs (fleet aud-1114-0703
# seed 477133967: the pump's pipe passed the free end of the second engine, power never came).
STEAM_PORT_TILES = {"steam-engine": ((0, -3), (0, 3)), "boiler": ((0, -1.5),)}


def steam_port_tiles(entities: list[dict]) -> set[tuple[float, float]]:
    """Tile centres where a pipe would join a steam engine's or boiler's steam box."""
    found = set()
    for e in entities:
        for dx, dy in STEAM_PORT_TILES.get(e.get("name"), ()):
            direction = e.get("direction", 0) % 16
            for _ in range(direction // 4):
                dx, dy = -dy, dx
            found.add((e["position"]["x"] + dx, e["position"]["y"] + dy))
    return found


def coal_feed_mode(state: dict[str, Any]) -> str:
    """``BELT_FED`` once the dedicated coal feed is proven, else ``HAND_FED``.

    A state saved before the split has a ``coal_plan`` but no ``power_mode``:
    its power job built the feed together with the plant, so it is belt-fed.
    """
    mode = state.get("power_mode")
    if mode in (HAND_FED, BELT_FED):
        return mode
    return BELT_FED if "coal_plan" in state else HAND_FED


def coal_fuel_value(catalog: Any) -> float:
    """Joules in one coal from the catalog (4 MJ in the base game)."""
    value = (getattr(catalog, "items", None) or {}).get("coal", {}).get("fuel_value")
    return float(value) if isinstance(value, (int, float)) and value > 0 else 4_000_000.0


def boiler_coal_per_minute(catalog: Any) -> float:
    """Coal/minute a boiler burns at full load: catalog ``energy_usage`` (J/tick) x 3600 / coal fuel value.

    Base game: 30 kJ/tick = 1.8 MW -> 27 coal/min. Without a catalog row the
    base boiler's 30 kJ/tick is assumed.
    """
    usage = (getattr(catalog, "entities", None) or {}).get("boiler", {}).get("energy_usage")
    usage = float(usage) if isinstance(usage, (int, float)) and usage > 0 else 30_000.0
    return round(usage * 3600 / coal_fuel_value(catalog), 3)


def hand_fed_power_ready(evidence: dict[str, Any]) -> bool:
    """``power_flow_ready`` without the coal drill and belt: the boiler is fuelled by hand."""
    return bool(
        evidence.get("ok")
        and evidence.get("water", 0) > 0
        and evidence.get("steam", 0) > 0
        and evidence.get("boiler_fuel", 0) > 0
        and evidence.get("connected_engines") == 2
    )


def _tile_key(position: dict) -> tuple[float, float]:
    return (round(float(position["x"]), 3), round(float(position["y"]), 3))


def _position(x: float, y: float) -> dict[str, float]:
    return {"x": round(x, 3), "y": round(y, 3)}


def _same_position(a: dict, b: dict) -> bool:
    return abs(a["x"] - b["x"]) < 0.2 and abs(a["y"] - b["y"]) < 0.2


def _find(observation: dict, entity: dict) -> dict | None:
    return next(
        (
            e
            for e in observation.get("entities", [])
            if e.get("name") == entity["name"] and _same_position(e["position"], entity["position"])
        ),
        None,
    )


def _plan_lookup(observation: dict):
    """Index only one plan's synchronous inspection, before any external query.

    Keep row order and the original strict position comparison. Rebuild for
    every call; mutable observations and their entity lists are never cached.
    """
    fallback = lambda entity: _find(observation, entity)
    buckets = {}
    try:
        for ordinal, row in enumerate(observation.get("entities", [])):
            position = row["position"]
            if abs(position["x"]) > 2**40 or abs(position["y"]) > 2**40:
                return fallback  # Preserve mixed int/float comparisons outside precise tile arithmetic.
            key = (row.get("name"), math.floor(position["x"]), math.floor(position["y"]))
            buckets.setdefault(key, []).append((ordinal, row))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        # An unrelated malformed row must not introduce a new lookup error.
        return fallback

    def find(entity: dict) -> dict | None:
        try:
            position = entity["position"]
            if abs(position["x"]) > 2**40 or abs(position["y"]) > 2**40:
                return fallback(entity)
            xs = range(math.floor(position["x"] - 0.2), math.floor(position["x"] + 0.2) + 1)
            ys = range(math.floor(position["y"] - 0.2), math.floor(position["y"] + 0.2) + 1)
            name = entity["name"]
            found, first = None, math.inf
            for x in xs:
                for y in ys:
                    for ordinal, row in buckets.get((name, x, y), ()):
                        if ordinal < first and _same_position(row["position"], position):
                            found, first = row, ordinal
            return found
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return fallback(entity)

    return find


def _direction_matches(name: str, actual: int, planned: int) -> bool:
    # These square entities have no directional intake or output geometry.
    if name in {"gun-turret", "stone-furnace", "steel-furnace"}:
        return True
    # These generators have two_direction_only in the real prototype: the game
    # normalizes south to north and west to east without changing fluid geometry.
    if name in {"steam-engine", "steam-turbine"}:
        return actual % 8 == planned % 8
    return actual == planned


def _built_as_planned(observation: dict, entity: dict) -> bool:
    """An observed entity of the plan row's name, position and direction (another owner's drill
    on the same tile facing elsewhere is not the planned one)."""
    actual = _find(observation, entity)
    return actual is not None and _direction_matches(
        entity["name"], actual.get("direction", 0), entity.get("direction", 0)
    )


def plan_observed(observation: dict, plan: dict) -> bool:
    """Check integrity without issuing construction, recipe, or fuel actions."""
    if not plan.get("ok") or not plan.get("entities"):
        return False
    find = _plan_lookup(observation)
    for entity in plan["entities"]:
        try:
            role = underground_fields(entity)
        except ValueError:
            return False
        found = find(entity)
        if found is None or (entity.get("recipe") and found.get("recipe") != entity["recipe"]):
            return False
        if role and (
            found.get("belt_to_ground_type") != role["belt_to_ground_type"] or found["position"] != entity["position"]
        ):
            return False
        if entity["name"] not in {"pipe", "small-electric-pole", "wooden-chest"} and not _direction_matches(
            entity["name"], found.get("direction", 0), entity.get("direction", 0)
        ):
            return False
    return True


def _rotate(x: float, y: float, direction: int) -> tuple[float, float]:
    for _ in range(direction // 4):
        x, y = -y, x
    return x, y


CONTAINERS = frozenset({"wooden-chest", "iron-chest", "steel-chest"})
INSERTER_NAMES = frozenset({"burner-inserter", "inserter", "long-handed-inserter", "fast-inserter"})


def containers_before_their_inserters(entities: list[dict]) -> list[dict]:
    """Plan order with every chest moved ahead of the first inserter that reaches its tile.

    An inserter built before its target chest drops its items on the ground
    there and the chest can no longer be placed (seeds 1024125955 /
    1188507449: the magazine row's output chest). A chest within an inserter's
    reach (1 tile, 2 for a long-handed one) on the inserter's axis is built
    first; every other row keeps its order.
    """
    inserters = [(i, e) for i, e in enumerate(entities) if e.get("name") in INSERTER_NAMES]
    if not inserters:
        return list(entities)
    moved: dict[int, int] = {}
    for j, chest in enumerate(entities):
        if chest.get("name") not in CONTAINERS:
            continue
        cx, cy = chest["position"]["x"], chest["position"]["y"]
        for i, arm in inserters:
            if i >= j:
                break
            reach = 2 if arm["name"] == "long-handed-inserter" else 1
            ax, ay = arm["position"]["x"], arm["position"]["y"]
            vertical = (arm.get("direction", 0) or 0) % 8 == 0
            if (abs(cx - ax) < 0.01 and abs(abs(cy - ay) - reach) < 0.01 and vertical) or (
                abs(cy - ay) < 0.01 and abs(abs(cx - ax) - reach) < 0.01 and not vertical
            ):
                moved[j] = i
                break
    if not moved:
        return list(entities)
    ahead: dict[int, list[dict]] = {}
    for j, i in moved.items():
        ahead.setdefault(i, []).append(entities[j])
    out = []
    for index, row in enumerate(entities):
        if index in moved:
            continue
        out.extend(ahead.get(index, ()))
        out.append(row)
    return out


def _plan(entities: list[dict], ports: list[dict], **extra: Any) -> dict:
    unique = {}
    for entity in entities:
        p = entity["position"]
        unique[(entity["name"], p["x"], p["y"])] = entity
    entities = list(unique.values())
    return {
        "ok": True,
        "reason": "",
        "entities": entities,
        "ports": ports,
        "required_items": dict(Counter(e.get("item") or e["name"] for e in entities)),
        **extra,
    }


# Coal in the 3x3 tiles around a dedicated power drill, in items, tried in
# this order. An unchecked fringe site ran dry in twenty minutes on a live
# map; a single high floor left no fringe site at all on another.
COAL_RICHNESS_TIERS = (4000, 2000, 1000, 400, 0)
# Build ahead (``FactoryBuilder.ensure_plan``): while the next plan entity waits
# for its item, a later entity whose item is carried is placed instead. These
# entity types keep plan order; at most this many placement checks per call.
BUILD_AHEAD_KEEP_ORDER = frozenset({"mining-drill", "inserter"})
KEEP_ORDER_REASON = "keeps plan order behind deferred entities"
BUILD_AHEAD_CHECKS = 4


class FactoryBuilder:
    def __init__(self, game: Any, bootstrap: Any, catalog: Any):
        self.game, self.bootstrap, self.catalog = game, bootstrap, catalog
        self.construction_materials: Any = None
        self.path = Path(game.cfg.runtime_dir) / "factory-builder.json"
        self.state: dict[str, Any] = {}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("schema_version") != 1:
                raise ValueError("unsupported factory builder checkpoint")

    def _sync(self, observation: dict) -> None:
        world_id = observation.get("world_id")
        if not world_id:
            raise ValueError("builder observation requires world_id")
        if self.state.get("world_id") != world_id:
            self.state = {"schema_version": 1, "world_id": world_id, "seeds": {}}
            self._save()
        tick = int(observation.get("tick", 0))
        fingerprint = getattr(self.catalog, "fingerprint", None)
        catalog_changed = "catalog_fingerprint" in self.state and self.state["catalog_fingerprint"] != fingerprint
        if tick < self.state.get("last_tick", 0) or catalog_changed:
            self.state.pop("power_sample_tick", None)
            self.state.pop("coal_feed_sample_tick", None)
            self.state.pop("power_verified_once", None)
            self.state["seeds"] = {}
        self.state["catalog_fingerprint"] = fingerprint
        self.state["last_tick"] = tick

    def _save(self) -> None:
        _atomic_json(self.path, self.state)

    def _placement_item(self, name: str) -> str:
        rows = getattr(self.catalog, "entities", {}).get(name, {}).get("items_to_place_this") or []
        return str(rows[0]["name"]) if rows else name

    def owns_automated_burner(self, entity: dict) -> bool:
        """Exclude these burners from repeated bootstrap inventory hand-feeding."""
        coal = self.state.get("coal_plan", {}).get("drill")
        if coal and entity.get("name") == coal["name"] and _same_position(entity["position"], coal["position"]):
            return True
        return any(
            e["name"] == "boiler"
            and entity.get("name") == "boiler"
            and _same_position(entity["position"], e["position"])
            for e in self.state.get("power_plan", {}).get("entities", [])
        )

    def ensure_plan(self, observation: dict, plan: dict, *, defer: Any = None) -> dict:
        """Return one material/move/build/recipe action, or a construction status.
        A succeeded result means the listed entities and recipes are observed; ports
        must still be connected and operating flow verified by the caller.
        ``defer`` (``early_start.Deferral``, early start) leaves entities for
        later instead of returning their wait; then the result is ``waiting``.
        """
        self._sync(observation)
        if not plan.get("ok") or not plan.get("entities"):
            return _report("blocked", plan.get("reason") or "empty or invalid block plan")
        try:
            for entity in plan["entities"]:
                underground_fields(entity)
        except ValueError as error:
            return _report("blocked", str(error))
        # A powered drill can spill ore into an unfinished receiver footprint.
        # This also applies to persisted cells reserved by an older version.
        entities = (
            sorted(plan["entities"], key=lambda row: row["name"] == "electric-mining-drill")
            if plan.get("resource_cell")
            else containers_before_their_inserters(plan["entities"])
        )
        find = _plan_lookup(observation)
        deferred: dict[str, str] = {}  # early start: item -> why its entities wait (see _deferred)
        for index, entity in enumerate(entities):
            existing = find(entity)
            if existing is not None:
                if underground_fields(entity) and (
                    existing.get("belt_to_ground_type") != entity["belt_to_ground_type"]
                    or existing["position"] != entity["position"]
                ):
                    return _report("blocked", "existing underground endpoint differs from reserved plan", entity=entity)
                direction_matches = _direction_matches(
                    entity["name"], existing.get("direction", 0), entity.get("direction", 0)
                )
                if entity.get("recipe") and (existing.get("recipe") != entity["recipe"] or not direction_matches):
                    if entity["recipe"] not in observation.get("enabled_recipes", {}):
                        return _report("blocked", "block recipe is locked", recipe=entity["recipe"])
                    move = self._move(observation, entity["position"])
                    return move or {
                        "type": "recipe",
                        "name": entity["name"],
                        "position": entity["position"],
                        "recipe": entity["recipe"],
                        "direction": entity.get("direction", 0),
                    }
                if entity["name"] not in {"pipe", "small-electric-pole", "wooden-chest"} and not direction_matches:
                    return _report("blocked", "existing entity direction differs from reserved plan", entity=entity)
                continue
            item = entity.get("item") or self._placement_item(entity["name"])
            if defer is not None and self._deferred(observation, entity, item, defer, deferred):
                continue
            if int(observation.get("inventory", {}).get(item, 0)) < 1:
                entity_type = self.catalog.entities.get(entity["name"], {}).get("type", entity["name"])
                infrastructure = {
                    "transport-belt",
                    "underground-belt",
                    "splitter",
                    "pipe",
                    "pipe-to-ground",
                    "electric-pole",
                    "wall",
                    "gate",
                }
                cap = (
                    32
                    if entity_type in infrastructure
                    else 8
                    if entity_type == "inserter"
                    else 4
                    if entity_type in {"container", "logistic-container"}
                    else 2
                )
                missing = set()
                for wanted in plan["entities"]:
                    placement_item = wanted.get("item") or self._placement_item(wanted["name"])
                    if placement_item == item and find(wanted) is None:
                        p = wanted["position"]
                        missing.add((wanted["name"], p["x"], p["y"]))
                wanted_count = min(cap, len(missing))
                result = self.bootstrap.ensure_item(observation, item, wanted_count)
                if self.construction_materials is not None:
                    result = self.construction_materials.ensure(observation, result)
                if defer is not None and (why := defer.after(item, result)):
                    deferred[item] = why
                    continue
                if isinstance(result, dict) and not result.get("type") and result.get("status") == "waiting":
                    # Waiting for this item must not hold back entities whose
                    # items are already carried: place those instead, all in one
                    # round trip on the assisted backend (eval-4177156614: runs of
                    # single "build a carried plan entity" belts, one pass each).
                    # Build ahead never spends what the waiting item's own craft
                    # needs (craft_reserve): fleet all-1114-0256 placed the belts
                    # collected for the district's underground pair on later routes
                    # 11 of 12 times, so the first bank smelted only at ~25 min.
                    ahead_view = _reserved_view(observation, self.craft_reserve(item, wanted_count))
                    if self.game.backend == "assisted":
                        batch = self._carried_batch(
                            ahead_view, entities[index + 1 :], skipped=frozenset(deferred) | {item}
                        )
                        if len(batch) > 1:
                            return {
                                "type": "build_many",
                                "actions": batch,
                                "reason": "build carried plan entities while an earlier one waits for its item",
                            }
                    ahead = self._build_ahead(ahead_view, plan, entities[index + 1 :], find)
                    if ahead is not None:
                        return ahead
                return result
            placement = self.can_place([entity])
            if not placement.get("ok"):
                if self.game.backend == "character":
                    from .deterministic_navigation import BUILD_FOOTPRINT_LUA

                    encoded = json.dumps(json.dumps(entity, separators=(",", ":")))
                    obstruction = self.game.query(
                        BUILD_FOOTPRINT_LUA
                        + """
local x=helpers.json_to_table("""
                        + encoded
                        + """)
local left,top,right,bottom=build_box(x)
local own_actor=false;local other=false
for _,e in pairs(s.find_entities_filtered{area={{left,top},{right,bottom}}}) do
 if e==a then own_actor=true elseif e.type~="resource" then other=true end
end
local terrain=s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f,type=x.belt_to_ground_type,
 build_check_type=defines.build_check_type.script,forced=false}
return {ok=true,only_actor=own_actor and not other and terrain}
"""
                    )
                    if obstruction.get("ok") and obstruction.get("only_actor"):
                        return {
                            "type": "build",
                            "name": entity["name"],
                            "item": item,
                            "position": entity["position"],
                            "direction": entity.get("direction", 0),
                            **underground_fields(entity),
                            "reason": "step outside the reserved build footprint before placement",
                        }
                recovery = self._recover_resource_cell_obstruction(observation, plan, entity)
                if recovery is None and not plan.get("resource_cell"):
                    recovery = self._recover_ground_items(observation, entity)
                if recovery is not None:
                    return recovery
                return _report(
                    "blocked",
                    "reserved block placement is obstructed",
                    blockers=placement.get("blocked"),
                    entity=entity,
                )
            # New character builds use the navigator's live build reach. A
            # fixed approach radius can send an actor back into the footprint
            # after belts carry it away from the chosen standing position.
            if self.game.backend == "assisted" and entity["name"] in BUILD_BATCH_NAMES:
                # Every carried entity of the plan in one round trip; deferred items are passed over.
                batch = self._carried_batch(observation, entities[index:], skipped=frozenset(deferred))
                if len(batch) > 1 and batch[0]["position"] == entity["position"] and batch[0]["name"] == entity["name"]:
                    return {"type": "build_many", "actions": batch}
            return {
                "type": "build",
                "name": entity["name"],
                "item": item,
                "position": entity["position"],
                "direction": entity.get("direction", 0),
                **underground_fields(entity),
            }
        if deferred:
            reasons = "; ".join(f"{item}: {why}" for item, why in sorted(deferred.items()))
            # Early start "parked" (early_start.parked): everything else stands and
            # the rest waits only for coming research, so the job holds no job slot.
            research = [why.startswith("waiting for research") for why in deferred.values()]
            order = [why == KEEP_ORDER_REASON for why in deferred.values()]
            return _report(
                "waiting",
                ("waiting for deferred plan entities: " + reasons)[:300],
                deferred=sorted(deferred),
                research_only=any(research) and all(r or o for r, o in zip(research, order)),
            )
        return _report(
            "succeeded",
            "block entities and recipes observed",
            constructed=len(plan["entities"]),
            ports=plan.get("ports", []),
            flow_verified=False,
        )

    def _deferred(self, observation: dict, entity: dict, item: str, defer: Any, deferred: dict) -> bool:
        """True when ``entity`` is left for later (early start, ``early_start.Deferral``).

        Its item already waits, its recipe waits for coming research
        (``defer.before``), or it is a mining drill or inserter behind a
        deferred entity (they keep plan order, as in build ahead).
        """
        if item in deferred:
            return True
        entity_type = self.catalog.entities.get(entity["name"], {}).get("type", entity["name"])
        if deferred and entity_type in BUILD_AHEAD_KEEP_ORDER:
            deferred[item] = KEEP_ORDER_REASON
            return True
        if int(observation.get("inventory", {}).get(item, 0)) >= 1:
            return False
        why = defer.before(item)
        if why:
            deferred[item] = why
            return True
        return False

    def _recover_resource_cell_obstruction(self, observation: dict, plan: dict, entity: dict) -> dict | None:
        """Recover a partial cell through ordinary mining and conserved pickup."""
        if not plan.get("resource_cell") or entity["name"] not in {"stone-furnace", "wooden-chest"}:
            return None
        drills = [row for row in plan["entities"] if row["name"] == "electric-mining-drill"]
        if len(drills) != 1:
            return None
        observed = _find(observation, drills[0])
        payload = json.dumps(json.dumps({"receiver": entity, "drill": drills[0]}, separators=(",", ":")))
        survey = self.game.query(
            """
local args=helpers.json_to_table("""
            + payload
            + """);local x=args.receiver
if target(x.position,x.name) then return {ok=false} end
local box=prototypes.entity[x.name].collision_box
local function rotate(x,y)
 local direction=args.receiver.direction or 0
 if direction==4 then return -y,x elseif direction==8 then return -x,-y elseif direction==12 then return y,-x end
 return x,y
end
local x1,y1=rotate(box.left_top.x,box.left_top.y);local x2,y2=rotate(box.right_bottom.x,box.right_bottom.y)
local left,right=x.position.x+math.min(x1,x2),x.position.x+math.max(x1,x2)
local top,bottom=x.position.y+math.min(y1,y2),x.position.y+math.max(y1,y2)
local ground={}
for _,e in pairs(s.find_entities_filtered{area={{left,top},{right,bottom}}}) do
 if e~=a and e.type~="resource" then
  if e.type~="item-entity" or not e.stack.valid_for_read or e.stack.prototype.type~="item" then return {ok=false} end
  ground[#ground+1]={name=e.name,position=pos(e.position),item=e.stack.name,quality=e.stack.quality.name,count=e.stack.count}
 end
end
if #ground==0 or not s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f,
 build_check_type=defines.build_check_type.manual_ghost,forced=true} then return {ok=false} end
local drill=target(args.drill.position,args.drill.name);local emitter=nil
if drill then
 local p=drill.drop_position
 if drill.force~=f or not drill.minable or drill.direction~=(args.drill.direction or 0)
  or p.x<left or p.x>right or p.y<top or p.y>bottom then return {ok=false} end
 emitter={unit_number=drill.unit_number}
end
return {ok=true,world_id=d and d.world_id,emitter=emitter,ground=ground}
"""
        )
        if not survey.get("ok") or survey.get("world_id") != observation["world_id"]:
            return None
        emitter = survey.get("emitter")
        if emitter:
            unit = emitter.get("unit_number")
            if not observed or not unit or observed.get("unit_number") != unit:
                return None
            return {
                "type": "mine",
                "name": drills[0]["name"],
                "position": drills[0]["position"],
                "count": 1,
                "expected_entity_unit": unit,
                "expected_entity_world_id": observation["world_id"],
                "reason": "recover the owned electric drill until its reserved receiver is built",
            }
        if observed:
            return None
        ground = sorted(
            survey.get("ground") or [],
            key=lambda row: (row["position"]["x"], row["position"]["y"], row["item"], row["quality"]),
        )
        if not ground:
            return None
        row = ground[0]
        return {
            "type": "take",
            "name": row["name"],
            "position": row["position"],
            "item": row["item"],
            "quality": row["quality"],
            "count": min(50, row["count"]),
            "reason": "collect spilled ore before constructing the reserved resource receiver",
        }

    def _recover_ground_items(self, observation: dict, entity: dict) -> dict | None:
        """Pick up item stacks lying on a reserved tile when nothing else obstructs it.

        Seeds 1024125955 / 1188507449 (long-1114-0039): the magazine row's output
        inserter stood before its iron chest (the chest waited for its craft),
        dropped magazines on the chest tile and the chest blocked for good on
        "reserved block placement is obstructed". Only ``item-entity`` stacks
        (and the actor) may stand in the footprint and the tile must take the
        entity once they are gone; anything else stays a block.
        """
        payload = json.dumps(json.dumps(entity, separators=(",", ":")))
        survey = self.game.query(
            """
local x=helpers.json_to_table("""
            + payload
            + """)
if target(x.position,x.name) then return {ok=false} end
local proto=prototypes.entity[x.name];if not proto then return {ok=false} end
local box=proto.collision_box
local function rotate(px,py)
 local direction=x.direction or 0
 if direction==4 then return -py,px elseif direction==8 then return -px,-py elseif direction==12 then return py,-px end
 return px,py
end
local x1,y1=rotate(box.left_top.x,box.left_top.y);local x2,y2=rotate(box.right_bottom.x,box.right_bottom.y)
local left,right=x.position.x+math.min(x1,x2),x.position.x+math.max(x1,x2)
local top,bottom=x.position.y+math.min(y1,y2),x.position.y+math.max(y1,y2)
local ground={}
for _,e in pairs(s.find_entities_filtered{area={{left,top},{right,bottom}}}) do
 if e~=a and e.type~="resource" then
  if e.type~="item-entity" or not e.stack.valid_for_read or e.stack.prototype.type~="item" then return {ok=false} end
  ground[#ground+1]={name=e.name,position=pos(e.position),item=e.stack.name,quality=e.stack.quality.name,count=e.stack.count}
 end
end
if #ground==0 or not s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f,
 build_check_type=defines.build_check_type.manual_ghost,forced=true} then return {ok=false} end
return {ok=true,world_id=d and d.world_id,ground=ground}
"""
        )
        if not survey.get("ok") or survey.get("world_id") != observation.get("world_id"):
            return None
        ground = sorted(
            survey.get("ground") or [],
            key=lambda row: (row["position"]["x"], row["position"]["y"], row["item"], row["quality"]),
        )
        if not ground:
            return None
        row = ground[0]
        return {
            "type": "take",
            "name": row["name"],
            "position": row["position"],
            "item": row["item"],
            "quality": row["quality"],
            "count": min(50, row["count"]),
            "reason": f"pick up items lying on the reserved {entity['name']} tile before building it",
        }

    def craft_reserve(self, item: str, count: int) -> Counter:
        """Units of ``item``'s direct ingredients one hand craft of ``count`` needs.

        Build ahead plans against the carried inventory minus this reserve: a
        splitter waiting for circuits keeps its 4 belts per craft, an underground
        pair its 5 belts. A catalog without the recipe reserves nothing.
        """
        recipes = getattr(self.catalog, "recipes", None) or {}
        recipe = recipes.get(item) if hasattr(recipes, "get") else None
        if not isinstance(recipe, dict) or count <= 0:
            return Counter()
        made = sum(
            float(p.get("amount") or 0)
            for p in recipe.get("products") or ()
            if isinstance(p, dict) and p.get("name") == item
        )
        crafts = math.ceil(count / made) if made > 0 else count
        reserve = Counter()
        for ingredient in recipe.get("ingredients") or ():
            if isinstance(ingredient, dict) and ingredient.get("type", "item") == "item" and ingredient.get("name"):
                reserve[ingredient["name"]] += int(math.ceil(float(ingredient.get("amount") or 0) * crafts))
        return reserve

    def _build_ahead(self, observation: dict, plan: dict, later: list[dict], find) -> dict | None:
        """One later plan entity whose item is carried and whose footprint is free, or None.

        Used only while the in-order entity waits for its item. Mining drills
        (they would mine into an unfinished receiver) and inserters (they drop
        items on the ground without a target) keep their plan order. At most
        BUILD_AHEAD_CHECKS placement queries per call.
        """
        inventory = observation.get("inventory", {})
        checks = 0
        for entity in later:
            entity_type = self.catalog.entities.get(entity["name"], {}).get("type", entity["name"])
            if entity_type in BUILD_AHEAD_KEEP_ORDER or find(entity) is not None:
                continue
            item = entity.get("item") or self._placement_item(entity["name"])
            if int(inventory.get(item, 0)) < 1:
                continue
            if checks >= BUILD_AHEAD_CHECKS:
                return None
            checks += 1
            if not self.can_place([entity]).get("ok"):
                continue
            return {
                "type": "build",
                "name": entity["name"],
                "item": item,
                "position": entity["position"],
                "direction": entity.get("direction", 0),
                **underground_fields(entity),
                "reason": "build a carried plan entity while an earlier one waits for its item",
            }
        return None

    def _carried_batch(
        self, observation: dict, entities: list[dict], *, skipped: frozenset = frozenset()
    ) -> list[dict]:
        """``_affordable_builds`` minus the entities a native placement check rejects (one query)."""
        rows = self._affordable_builds(observation, entities, skipped=skipped)
        if len(rows) < 2:
            return rows
        check = self.can_place([{k: r[k] for k in ("name", "position", "direction")} for r in rows])
        if not check.get("ok"):
            blocked = {
                (b.get("name"), (b.get("position") or {}).get("x"), (b.get("position") or {}).get("y"))
                for b in check.get("blocked") or ()
            }
            if not blocked:
                return rows[:1]
            rows = [r for r in rows if (r["name"], r["position"]["x"], r["position"]["y"]) not in blocked]
        return rows

    def _affordable_builds(
        self, observation: dict, entities: list[dict], *, skipped: frozenset = frozenset()
    ) -> list[dict]:
        """Every carried plan entity that may share one ``build_many`` round trip, in plan order.

        An entity that cannot join (item not carried, item in ``skipped``, a recipe, an
        underground role, a type outside BUILD_BATCH_NAMES) is passed over, not a stop:
        the carried entities after it still join (eval-4177156614: 122 of 438 district
        placements were single builds, one pass each, because the old prefix stopped at
        the first missing item). Once anything was passed over (or ``skipped`` is not
        empty), BUILD_AHEAD_KEEP_ORDER types (drills, inserters) are passed over too, the
        build-ahead rule. An existing entity that differs from the plan stops the batch
        (``ensure_plan`` reports it). At most BUILD_BATCH_LIMIT rows.
        """
        stock = Counter(observation.get("inventory", {}))
        rows, seen = [], {}
        passed = bool(skipped)
        for entity in entities:
            existing = _find(observation, entity)
            if existing is not None:
                direction_matches = _direction_matches(
                    entity["name"], existing.get("direction", 0), entity.get("direction", 0)
                )
                if (
                    entity.get("recipe")
                    and (existing.get("recipe") != entity["recipe"] or not direction_matches)
                    or entity["name"] not in {"pipe", "small-electric-pole", "wooden-chest"}
                    and not direction_matches
                ):
                    break
                continue
            p = entity["position"]
            key = entity["name"], p["x"], p["y"]
            if key in seen:
                if entity["name"] != "small-electric-pole" and seen[key] != entity.get("direction", 0):
                    break
                continue
            item = entity.get("item") or self._placement_item(entity["name"])
            entity_type = self.catalog.entities.get(entity["name"], {}).get("type", entity["name"])
            if (
                entity["name"] not in BUILD_BATCH_NAMES
                or entity.get("recipe")
                or entity.get("belt_to_ground_type")
                or item in skipped
                or stock[item] < 1
                or (passed and entity_type in BUILD_AHEAD_KEEP_ORDER)
            ):
                passed = True
                continue
            stock[item] -= 1
            seen[key] = entity.get("direction", 0)
            rows.append(
                {
                    "type": "build",
                    "name": entity["name"],
                    "item": item,
                    "position": p,
                    "direction": entity.get("direction", 0),
                }
            )
            if len(rows) == BUILD_BATCH_LIMIT:
                break
        return rows

    def _move(self, observation: dict, position: dict) -> dict | None:
        if self.game.backend != "character":
            return None
        actor = observation.get("position") or {"x": 0, "y": 0}
        if math.hypot(actor["x"] - position["x"], actor["y"] - position["y"]) > 4:
            return {"type": "move", "position": position}
        return None

    def can_place(self, entities: list[dict]) -> dict:
        try:
            for entity in entities:
                underground_fields(entity)
        except ValueError as error:
            return {
                "ok": False,
                "reason": str(error),
                "blocked": [{"name": entity["name"], "position": entity["position"], "reason": str(error)}],
            }
        if stop_requested(self.path.parent / "stop.json"):
            raise InterruptedError("operator_stop_requested")
        payload = json.dumps(json.dumps(entities, separators=(",", ":")))
        return self.game.query(
            """
local specs=helpers.json_to_table("""
            + payload
            + """);local blocked={}
for _,x in ipairs(specs) do
 local old=target(x.position,x.name)
 if old then
  local mismatch=x.direction and old.direction~=x.direction
  if x.belt_to_ground_type and (old.belt_to_ground_type~=x.belt_to_ground_type or old.direction~=(x.direction or 0) or old.force~=f or old.surface~=s
   or old.position.x~=x.position.x or old.position.y~=x.position.y) then mismatch=true end
  if x.name=="gun-turret" and old.force==f then mismatch=false end
  if x.name=="steam-engine" or x.name=="steam-turbine" then mismatch=x.direction and old.direction%8~=x.direction%8 end
  if mismatch and not x.recipe and x.name~="pipe" and x.name~="small-electric-pole" then
   blocked[#blocked+1]={name=x.name,position=x.position,reason="existing_direction_mismatch"}
  end
 elseif not s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f,type=x.belt_to_ground_type} then
  blocked[#blocked+1]={name=x.name,position=x.position,reason="terrain_or_entity_collision"}
 end
end
return {ok=#blocked==0,blocked=blocked}
"""
        )

    def water_sites(self, radius: int = 192, limit: int = 24) -> list[dict]:
        result = self.game.query(
            """
local candidates={};local seen={}
local vectors={{0,-1,0},{1,0,4},{0,1,8},{-1,0,12}}
local tiles=s.find_tiles_filtered{position={0,0},radius="""
            + str(radius)
            + """,name={"water","deepwater","water-green","deepwater-green"},limit=60000}
for _,tile in pairs(tiles) do
 for _,v in ipairs(vectors) do
  local p={x=tile.position.x+0.5-v[1],y=tile.position.y+0.5-v[2]}
  local key=p.x..":"..p.y..":"..v[3]
  if not seen[key] and s.can_place_entity{name="offshore-pump",position=p,direction=v[3],force=f} then
   seen[key]=true;candidates[#candidates+1]={position=p,direction=v[3],water_out={x=p.x-v[1],y=p.y-v[2]},distance=p.x*p.x+p.y*p.y}
  end
 end
end
table.sort(candidates,function(a,b) return a.distance<b.distance end)
local out={};for i=1,math.min(#candidates,"""
            + str(limit)
            + """) do out[#out+1]=candidates[i] end
return success{sites=out}
"""
        )
        return result.get("sites", [])

    def _occupied_by_plan(self, entities: list[dict]) -> set[tuple[float, float]]:
        known = {
            "boiler": (3, 2),
            "steam-engine": (3, 5),
            "burner-mining-drill": (2, 2),
            "stone-furnace": (2, 2),
            "steel-furnace": (2, 2),
            "electric-mining-drill": (3, 3),
            "lab": (3, 3),
            "assembling-machine-1": (3, 3),
            "assembling-machine-2": (3, 3),
            "assembling-machine-3": (3, 3),
            "oil-refinery": (5, 5),
            "chemical-plant": (3, 3),
            "pumpjack": (3, 3),
            "storage-tank": (3, 3),
            "rocket-silo": (9, 9),
        }
        occupied = set()
        for e in entities:
            w, h = known.get(e["name"], (1, 1))
            w, h = e.get("_width", w), e.get("_height", h)
            if e.get("direction") in (4, 12):
                w, h = h, w
            x, y = e["position"]["x"], e["position"]["y"]
            for tx in range(math.floor(x - w / 2 + 1e-6), math.ceil(x + w / 2 - 1e-6)):
                for ty in range(math.floor(y - h / 2 + 1e-6), math.ceil(y + h / 2 - 1e-6)):
                    occupied.add((tx + 0.5, ty + 0.5))
        return occupied

    def route(
        self,
        source: dict,
        destination: dict,
        name: str,
        reserved: list[dict],
        *,
        start_direction: int | None = None,
        end_direction: int | None = None,
        margin: float = 12,
        ignore: list[dict] | None = None,
    ) -> dict:
        """A route avoiding ``reserved`` and the world; ``ignore``: our entities' tiles treated as free."""
        return self._route(
            source,
            destination,
            name,
            reserved,
            start_direction=start_direction,
            end_direction=end_direction,
            margin=margin,
            ignore=ignore,
        )

    def clear_route_obstacle(
        self,
        source: dict,
        destination: dict,
        reserved: list[dict],
        *,
        start_direction: int | None = None,
        end_direction: int | None = None,
        margin: float = 24,
    ) -> dict:
        """Return one ordinary mining action only after proving a clearable route.

        The alternate path is never exposed as a construction plan. The caller
        must reobserve and use normal collision checks after every mined entity.
        """
        result = self._route(
            source,
            destination,
            "transport-belt",
            reserved,
            margin=margin,
            start_direction=start_direction,
            end_direction=end_direction,
            clear_natural=True,
        )
        if not result.get("ok"):
            return {"ok": False, "reason": result.get("reason", "no clearable route")}
        clearable = {
            (row["position"]["x"], row["position"]["y"]): row["entities"] for row in result.get("clearable", [])
        }
        for point in result["path"]:
            for entity in clearable.get((point["x"], point["y"]), []):
                if (
                    entity.get("force") == "neutral"
                    and entity.get("minable")
                    and (
                        entity.get("type") == "tree"
                        or (
                            entity.get("type") == "simple-entity"
                            and entity.get("name") in {"big-rock", "huge-rock", "big-sand-rock"}
                        )
                    )
                ):
                    return {
                        "ok": True,
                        "action": {
                            "type": "mine",
                            "name": entity["name"],
                            "position": entity["position"],
                            "count": 1,
                            "reason": "clear an observed natural obstacle on a proven material route",
                        },
                        "planned_length": len(result["path"]),
                    }
        return {"ok": False, "reason": "route has no verified natural obstacle to clear"}

    def _route(
        self,
        source: dict,
        destination: dict,
        name: str,
        reserved: list[dict],
        *,
        start_direction: int | None = None,
        end_direction: int | None = None,
        margin: float = 12,
        clear_natural: bool = False,
        ignore: list[dict] | None = None,
    ) -> dict:
        if stop_requested(self.path.parent / "stop.json"):
            raise InterruptedError("operator_stop_requested")
        if (
            isinstance(margin, bool)
            or not isinstance(margin, (int, float))
            or not math.isfinite(margin)
            or margin < 1
            or int(margin) != margin
        ):
            return {"ok": False, "reason": "route margin must be a positive whole number of tiles"}
        bounds = {
            "min_x": min(source["x"], destination["x"]) - margin,
            "max_x": max(source["x"], destination["x"]) + margin,
            "min_y": min(source["y"], destination["y"]) - margin,
            "max_y": max(source["y"], destination["y"]) + margin,
        }
        cells = (bounds["max_x"] - bounds["min_x"] + 1) * (bounds["max_y"] - bounds["min_y"] + 1)
        if cells > 50000:
            return {"ok": False, "reason": "route survey exceeds 50000 tiles"}
        payload = json.dumps(
            json.dumps(
                {
                    "bounds": bounds,
                    "name": name,
                    "source": source,
                    "destination": destination,
                    "clear_natural": clear_natural,
                    "ignore": [{"x": q["x"], "y": q["y"]} for q in ignore or ()],
                },
                separators=(",", ":"),
            )
        )
        survey = self.game.query(
            """
local args=helpers.json_to_table("""
            + payload
            + """);local b=args.bounds;local blocked={};local clearable={}
local ignored={};for _,q in ipairs(args.ignore or {}) do ignored[q.x..":"..q.y]=true end
local function natural(p)
 if not args.clear_natural then return nil end
 --[[ Ghost checks retain terrain collision but may ignore buildings;
 independently reject every other entity before admitting this tile. ]]
 if not s.can_place_entity{name=args.name,position=p,force=f,
  build_check_type=defines.build_check_type.manual_ghost,forced=true} then return nil end
 local box=prototypes.entity[args.name].collision_box
 local area={{p.x+box.left_top.x,p.y+box.left_top.y},{p.x+box.right_bottom.x,p.y+box.right_bottom.y}}
 local out={}
 for _,e in pairs(s.find_entities_filtered{area=area}) do
  if e.type~="resource" and not e.prototype.collision_mask.colliding_with_tiles_only then
   local rock=e.type=="simple-entity" and (e.name=="big-rock" or e.name=="huge-rock" or e.name=="big-sand-rock")
   if e.force.name~="neutral" or not e.minable or not (e.type=="tree" or rock) then return nil end
   out[#out+1]={name=e.name,type=e.type,position=pos(e.position),force=e.force.name,minable=true}
  end
 end
 if #out>0 then return out end
end
for x=b.min_x,b.max_x,1 do for y=b.min_y,b.max_y,1 do
 local p={x=x,y=y}
 if not ignored[x..":"..y] and not s.can_place_entity{name=args.name,position=p,force=f} then
  local removable=natural(p)
  if removable then clearable[#clearable+1]={position=p,entities=removable} else blocked[#blocked+1]=p end
 end
end end
local function endpoint(p) return (p.x==args.source.x and p.y==args.source.y) or (p.x==args.destination.x and p.y==args.destination.y) end
for _,e in pairs(s.find_entities_filtered{area={{b.min_x-1,b.min_y-1},{b.max_x+1,b.max_y+1}},force=f}) do
 if not endpoint(e.position) and not ignored[e.position.x..":"..e.position.y] then
  if e.type=="transport-belt" then
   local vectors={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}};local v=vectors[e.direction]
   if v then blocked[#blocked+1]={x=e.position.x+v[1],y=e.position.y+v[2]} end
  elseif args.name=="pipe" and (e.type=="pipe" or e.type=="pipe-to-ground") then
   for _,v in ipairs({{0,-1},{1,0},{0,1},{-1,0}}) do blocked[#blocked+1]={x=e.position.x+v[1],y=e.position.y+v[2]} end
  end
 end
end
return success{blocked=blocked,clearable=clearable}
"""
        )
        if not survey.get("ok"):
            return {"ok": False, "reason": survey.get("reason") or "route survey failed"}
        occupied = self._occupied_by_plan(reserved)
        endpoints = {(source["x"], source["y"]), (destination["x"], destination["y"])}
        for entity in reserved:
            p = entity["position"]
            if (p["x"], p["y"]) in endpoints:
                continue
            if entity["name"] == "transport-belt":
                dx, dy = DIRECTIONS[entity.get("direction", 0)]
                occupied.add((p["x"] + dx, p["y"] + dy))
            elif name == "pipe" and entity["name"] in {"pipe", "pipe-to-ground"}:
                occupied.update((p["x"] + dx, p["y"] + dy) for dx, dy in DIRECTIONS.values())
        if name == "pipe":
            occupied.update(steam_port_tiles(reserved))
        occupied.update((p["x"], p["y"]) for p in survey.get("blocked", []))
        occupied.discard((source["x"], source["y"]))
        occupied.discard((destination["x"], destination["y"]))
        result = self.plan_route(
            name,
            source,
            destination,
            occupied=occupied,
            bounds=bounds,
            max_nodes=25000,
            start_direction=start_direction,
            end_direction=end_direction,
        )
        if clear_natural and result.get("ok"):
            result["clearable"] = survey.get("clearable", [])
        return result

    def plan_route(self, name: str, source: dict, destination: dict, **kwargs) -> dict:
        """Routing policy seam; v1 preserves its original orthogonal search."""
        return route_orthogonal(source, destination, **kwargs)

    def _choose_power_plan(self) -> dict:
        for site in self.water_sites():
            inward = (site["direction"] + 8) % 16
            dx, dy = DIRECTIONS[inward]
            rotation = (inward - 4) % 16
            px, py = _rotate(-4, 1, rotation)
            for distance in (3, 8, 14, 24):
                for sideways in (0, 10, -10):
                    desired = _position(
                        site["water_out"]["x"] + dx * distance - dy * sideways,
                        site["water_out"]["y"] + dy * distance + dx * sideways,
                    )
                    anchor = _position(desired["x"] - px, desired["y"] - py)
                    for bank in self._steam_bank_variants(anchor, rotation):
                        if not self.can_place(bank["entities"])["ok"]:
                            continue
                        pump = {"name": "offshore-pump", "position": site["position"], "direction": site["direction"]}
                        water = next(p for p in bank["ports"] if p["item"] == "water")
                        path = self.route(site["water_out"], water["position"], "pipe", bank["entities"] + [pump])
                        if not path.get("ok"):
                            continue
                        pipes = [{"name": "pipe", "position": p, "direction": 0} for p in path["path"]]
                        plan = _plan([pump] + bank["entities"] + pipes, bank["ports"], template="starter_power")
                        if self.can_place(plan["entities"])["ok"] and self._power_plan_clear(plan["entities"]):
                            return plan
        return {"ok": False, "reason": "no clear starter coast supports connected steam power"}

    def _power_plan_clear(self, entities: list[dict]) -> bool:
        """Resource policy hook for a power plan candidate (the v2 builder rejects ore and mining reservations).

        The native ``can_place`` lets a belt or pipe stand on ore, so a candidate passed
        here and the saved plan then blocked forever in ``ensure_plan``'s resource policy
        (fleet all-1113-2332 seed 1561968568: the boiler's coal stub on ore at 4 game min).
        """
        return True

    def _replan_power(self, observation: dict, power: dict, result: dict) -> dict | None:
        """Drop a saved, wholly unbuilt power plan the resource policy refuses; None otherwise."""
        if result.get("status") != "blocked" or result.get("reason") not in POWER_REPLAN_REASONS:
            return None
        if any(_find(observation, e) is not None for e in power.get("entities", [])):
            return None  # something stands: the built part may stand in the new way
        self.state.pop("power_plan", None)
        self._save()
        return _report(
            "waiting",
            "starter power plan met ore or a mining reservation before anything was built; planning again",
            refused={k: result.get(k) for k in ("reason", "entity", "position") if result.get(k) is not None},
        )

    def _steam_bank_variants(self, anchor: dict, rotation: int) -> list[dict]:
        """Steam bank layouts ordered so the coal port faces the coal source.

        The belt stub under the boiler inserter can enter from three sides.
        Rank each by how far a belt from the nearest coal must travel to reach
        its port in the port's own travel direction: the straight-line distance
        plus twice the distance it would have to overshoot and turn back.
        """
        if not hasattr(self, "_coal_origin"):
            sites = self.coal_sites()
            self._coal_origin = sites[0] if sites else None
        banks = [build_template("steam_bank", anchor=anchor, rotation=rotation, coal_entry=e) for e in COAL_ENTRIES]
        banks = [bank for bank in banks if bank["ok"]]
        coal = self._coal_origin
        if coal is None:
            return banks

        def detour(bank: dict) -> float:
            return coal_entry_detour(next(p for p in bank["ports"] if p["item"] == "coal"), coal)

        return sorted(banks, key=detour)

    def coal_sites_richest_first(self) -> list[dict]:
        """Coal sites ordered by richness tier, nearest first inside a tier.

        A dedicated power drill has to sit on the patch fringe, where tiles are
        thin. Filtering by one fixed amount either leaves no fringe site or
        admits one that runs dry in minutes, so candidates are ranked instead:
        the planner tries the richest sites first and only then thinner ones.
        """
        ordered: list[dict] = []
        seen: set[tuple] = set()
        for minimum in COAL_RICHNESS_TIERS:
            for site in self.coal_sites(minimum):
                key = (site["x"], site["y"])
                if key not in seen:
                    seen.add(key)
                    ordered.append(site)
        return ordered

    def coal_sites(self, minimum: int = 0) -> list[dict]:
        # A drill on the thin fringe of a patch runs dry within minutes and the
        # power feed then waits forever, so require real ore under the drill.
        result = self.game.query(
            "local MINIMUM="
            + str(int(minimum))
            + """
local out={};local seen={}
for _,e in pairs(s.find_entities_filtered{position={0,0},radius=192,name="coal",limit=3000}) do
 local p={x=math.floor(e.position.x+0.5),y=math.floor(e.position.y+0.5)};local key=p.x..":"..p.y
 if not seen[key] then
  seen[key]=true
  local mixed=false;local amount=0
  for _,r in pairs(s.find_entities_filtered{area={{p.x-1,p.y-1},{p.x+1,p.y+1}},type="resource"}) do
   if r.name~="coal" then mixed=true;break end
   amount=amount+r.amount
  end
  if not mixed and amount>=MINIMUM then out[#out+1]=p end
 end
end
table.sort(out,function(a,b) return a.x*a.x+a.y*a.y<b.x*b.x+b.y*b.y end)
return success{sites=out}
"""
        )
        return result.get("sites", [])

    @staticmethod
    def _coal_plan(position: dict) -> dict:
        x, y = position["x"], position["y"]
        drill = {"name": "burner-mining-drill", "position": position, "direction": 4}
        path = [
            (1.5, -0.5, 4),
            (2.5, -0.5, 0),
            (2.5, -1.5, 0),
            (2.5, -2.5, 12),
            (1.5, -2.5, 12),
            (0.5, -2.5, 12),
            (-0.5, -2.5, 12),
            (-1.5, -2.5, 12),
            (-2.5, -2.5, 8),
            (-2.5, -1.5, 8),
            (-2.5, -0.5, 8),
            (-2.5, 0.5, 8),
            (-2.5, 1.5, 8),
        ]
        entities = [drill]
        entities.extend(
            {"name": "transport-belt", "position": _position(x + dx, y + dy), "direction": direction}
            for dx, dy, direction in path
        )
        entities.append({"name": "burner-inserter", "position": _position(x - 1.5, y - 0.5), "direction": 12})
        return _plan(
            entities,
            [
                {
                    "kind": "item",
                    "item": "coal",
                    "direction": "output",
                    "position": _position(x - 2.5, y + 1.5),
                    "facing": 8,
                }
            ],
            drill=drill,
        )

    def _choose_coal_plan(self, power: dict) -> dict:
        destination = next(p for p in power["ports"] if p["item"] == "coal")
        for position in self.coal_sites_richest_first()[:256]:
            coal = self._coal_plan(position)
            if not self.can_place(coal["entities"])["ok"]:
                continue
            source = coal["ports"][0]
            path = self.route(
                source["position"],
                destination["position"],
                "transport-belt",
                power["entities"] + coal["entities"],
                start_direction=source["facing"],
                end_direction=destination["facing"],
            )
            if not path.get("ok"):
                continue
            belts = [{"name": "transport-belt", **segment} for segment in path["segments"]]
            plan = _plan(coal["entities"] + belts, coal["ports"], drill=coal["drill"], template="dedicated_coal_feed")
            if self.can_place(plan["entities"])["ok"]:
                return plan
        return {"ok": False, "reason": "no pure coal drill site has a clear dedicated boiler route"}

    def _seed(self, observation: dict, key: str, entity: dict, count: int) -> dict | None:
        actual = _find(observation, entity)
        if actual is None:
            return _report("blocked", "cannot seed a missing burner", entity=entity)
        if entity["name"] == "electric-mining-drill":
            return (
                None
                if actual.get("electric_network_connected") and actual.get("energy", 0) > 0
                else _report("waiting", "electric mining startup needs its real power connection")
            )
        state = self.state["seeds"].setdefault(key, {"attempts": 0, "observed": False})
        fuel = int(actual.get("inventory", {}).get("coal", 0))
        if fuel > 0 or actual.get("remaining_burning_fuel", 0) > 0:
            state["observed"] = True
            self._save()
            return None
        if state["observed"]:
            return None  # dedicated belt repair must restore fuel after startup
        if state["attempts"] >= 3:
            return _report("blocked", "startup fuel insertion was not observed after three attempts", target=key)
        available = int(observation.get("inventory", {}).get("coal", 0))
        if available < count:
            return self.bootstrap.ensure_item(observation, "coal", count)
        move = self._move(observation, entity["position"])
        if move:
            return move
        state["attempts"] += 1
        self._save()
        return {
            "type": "insert",
            "name": entity["name"],
            "position": entity["position"],
            "item": "coal",
            "inventory": "fuel",
            "count": count,
            "reason": "one-time power bootstrap fuel",
        }

    def power_evidence(self, power: dict, coal: dict | None) -> dict:
        """Native plant measurements; ``coal`` None (hand-fed boiler) leaves out the feed drill and belts."""
        targets = {
            "boiler": next(e for e in power["entities"] if e["name"] == "boiler"),
            "engines": [e for e in power["entities"] if e["name"] == "steam-engine"],
            "pole": next(e for e in power["entities"] if e["name"] == "small-electric-pole"),
            "drill": coal["drill"] if coal else None,
            "belts": [e for e in coal["entities"] if e["name"] == "transport-belt"] if coal else [],
        }
        payload = json.dumps(json.dumps(targets, separators=(",", ":")))
        return self.game.query(
            """
local t=helpers.json_to_table("""
            + payload
            + """)
local function fuel(e)
 if not e or not e.burner then return 0 end
 local joules=e.burner.remaining_burning_fuel or 0
 for _,row in pairs(e.burner.inventory.get_contents()) do
  joules=joules+row.count*(prototypes.item[row.name].fuel_value or 0)
 end
 return joules
end
local function fluid(e,name)
 if not e then return 0 end
 local contents=e.get_fluid_contents();return contents[name] or 0
end
local boiler=target(t.boiler.position,t.boiler.name);local drill=t.drill and target(t.drill.position,t.drill.name) or nil
local steam=0;local connected=0;local energized=0
for _,row in ipairs(t.engines) do local e=target(row.position,row.name)
 if e then steam=steam+fluid(e,"steam");if e.is_connected_to_electric_network() then connected=connected+1 end;if e.energy>0 then energized=energized+1 end end
end
local coal_on_belts=0
for _,row in ipairs(t.belts) do local e=target(row.position,row.name)
 if e then for lane=1,2 do coal_on_belts=coal_on_belts+e.get_transport_line(lane).get_item_count("coal") end end
end
local generation_kw=0;local total_energy=0;local pole=target(t.pole.position,t.pole.name)
if pole then
 local stats=pole.electric_network_statistics
 generation_kw=stats.get_flow_count{name="steam-engine",category="output",precision_index=defines.flow_precision_index.five_seconds}*60/1000
 total_energy=stats.get_output_count("steam-engine")
end
local coal_rate=0
if drill then
 local mined=prototypes.entity.coal.mineable_properties
 coal_rate=drill.prototype.mining_speed*60/mined.mining_time
end
return success{boiler_fuel=fuel(boiler),drill_fuel=fuel(drill),
 drill_powered=drill and not drill.burner and drill.energy>0 and drill.is_connected_to_electric_network() or false,
 water=fluid(boiler,"water"),steam=steam,
 connected_engines=connected,energized_engines=energized,generation_kw=generation_kw,total_energy_generated=total_energy,
 coal_on_belts=coal_on_belts,coal_mining_per_minute=coal_rate,coal_feed_needs_expansion_for_full_engine_capacity=true,tick=game.tick}
"""
        )

    def ensure_power(self, observation: dict, repair: bool = False) -> dict:
        """Build and verify starter power; ``repair`` (the power job itself): diagnose a built plant
        that never runs and repair it under the ladder's clear rung (``_power_fault``)."""
        self._sync(observation)
        if not observation.get("technologies", {}).get("steam-power") and "boiler" not in observation.get(
            "enabled_recipes", {}
        ):
            return _report("blocked", "steam power technology must unlock naturally before construction")
        if "power_plan" not in self.state:
            plan = self._choose_power_plan()
            if not plan.get("ok"):
                return _report("blocked", plan["reason"], diagnostics=plan)
            self.state["power_plan"] = plan
            self._save()
        if self.state.get("power_repair"):
            # The republished plan is in place; the replaced pipes go before anything new is built.
            step = self._continue_power_repair(observation)
            if step is not None:
                return step
        power = self.state["power_plan"]
        result = self.ensure_plan(observation, power)
        if result.get("status") != "succeeded":
            return self._replan_power(observation, power, result) or result
        if coal_feed_mode(self.state) == HAND_FED:
            result = self._ensure_hand_fed_power(observation, power)
        else:
            result = self._ensure_belt_fed_power(observation, power, "power_sample_tick")
        if repair and result.get("status") == "waiting" and isinstance(result.get("evidence"), dict):
            return self._power_fault(observation, power, result["evidence"]) or result
        return result

    def power_pipe_fluids(self, power: dict) -> dict:
        """Live fluids of the plan's pipes (plan order) and the boiler's water: the fault diagnosis read."""
        pipes = [{"name": e["name"], "position": e["position"]} for e in power["entities"] if e["name"] == "pipe"]
        boiler = next(e for e in power["entities"] if e["name"] == "boiler")
        payload = json.dumps(json.dumps({"pipes": pipes, "boiler": boiler}, separators=(",", ":")))
        return self.game.query(
            """
local t=helpers.json_to_table("""
            + payload
            + """)
local rows={}
for i,row in ipairs(t.pipes) do
 local e=target(row.position,row.name);local fluids={}
 if e then for name,amount in pairs(e.get_fluid_contents()) do fluids[name]=amount end end
 rows[i]={standing=e~=nil,fluids=fluids}
end
local boiler=target(t.boiler.position,t.boiler.name);local water=0
if boiler then water=boiler.get_fluid_contents()["water"] or 0 end
return success{pipes=rows,boiler_water=water}
"""
        )

    def _power_fault(self, observation: dict, power: dict, evidence: dict) -> dict | None:
        """A built plant that has fuel but no water or steam for REPAIR_AFTER_TICKS: diagnose, then block or repair.

        Only a plant that generates nothing is touched (no consumer can be cut off). A fault
        (``built_block_repair.water_network_faults``) blocks the job with ``built`` needs; the
        remedy ladder's clear rung re-runs it with ``repair_built_block`` and the repair starts.
        """
        from .. import built_block_repair as repair
        from .. import remedy

        tick = int(evidence.get("tick", observation.get("tick", 0)) or 0)
        healthy = evidence.get("water", 0) > 0 and evidence.get("steam", 0) > 0
        if healthy or evidence.get("generation_kw", 0) > 0 or evidence.get("boiler_fuel", 0) <= 0:
            if self.state.pop("power_fault_since", None) is not None:
                self._save()
            return None
        since = self.state.setdefault("power_fault_since", tick)
        self._save()
        if tick - since < repair.REPAIR_AFTER_TICKS:
            return None
        live = self.power_pipe_fluids(power)
        if not live.get("ok"):
            return None
        faults = repair.water_network_faults(power["entities"], live.get("pipes"), live.get("boiler_water"))
        if not faults:
            return None
        done = int(self.state.get("power_repairs", 0))
        if remedy.option(repair.OPTION) and done < repair.REPAIRS_MAX:
            return self._start_power_repair(observation, power, faults)
        out = _report("blocked", f"built_block_fault:power:{faults[0]['fault']}", faults=faults[:5], **evidence)
        out["plan_needs"] = repair.needs("power", faults, repairs=done)
        return out

    def _start_power_repair(self, observation: dict, power: dict, faults: list[dict]) -> dict:
        """Plan the water pipe again (the old pipes' tiles free), republish the plan, then remove the old pipes."""
        from .. import built_block_repair as repair

        pump = next(e for e in power["entities"] if e["name"] == "offshore-pump")
        site = next((w for w in self.water_sites() if _same_position(w["position"], pump["position"])), None)
        water = next((p for p in power.get("ports", []) if p.get("item") == "water"), None)
        old = repair.route_pipes(power["entities"], site["water_out"], water["position"]) if site and water else []
        if site is None or water is None or not old:
            out = _report("blocked", "built_block_repair_no_water_site:power", faults=faults[:5])
            missing = "water site" if site is None else "water port" if water is None else "routed pipe"
            out["plan_needs"] = repair.needs("power", faults, missing=missing)
            return out
        routed = {_tile_key(e["position"]) for e in old}
        keep = [e for e in power["entities"] if e["name"] != "pipe" or _tile_key(e["position"]) not in routed]
        bank = [e for e in keep if e["name"] != "offshore-pump"]
        path = self.route(
            site["water_out"], water["position"], "pipe", bank + [pump], ignore=[e["position"] for e in old]
        )
        if not path.get("ok"):
            out = _report(
                "blocked", "built_block_repair_route_failed:power", faults=faults[:5], route=path.get("reason")
            )
            out["plan_needs"] = repair.needs("power", faults, route=str(path.get("reason") or "")[:120])
            return out
        pipes = [{"name": "pipe", "position": q, "direction": 0} for q in path["path"]]
        fresh = {_tile_key(q["position"]) for q in pipes}
        plan = {**power, **_plan(keep + pipes, power["ports"], template=power.get("template", "starter_power"))}
        self.state["power_plan"] = plan  # republished: the board reads the builder state
        self.state["power_repair"] = {
            "remove": [e for e in old if _tile_key(e["position"]) not in fresh],
            "faults": faults[:5],
            "tick": observation.get("tick"),
        }
        self.state["power_repairs"] = int(self.state.get("power_repairs", 0)) + 1
        self.state.pop("power_fault_since", None)
        self._save()
        return self._continue_power_repair(observation) or _report(
            "waiting", "power water network replanned; building the new pipe", faults=faults[:5]
        )

    def _continue_power_repair(self, observation: dict) -> dict | None:
        """The next guarded mine of a replaced pipe, or None once every one is gone (the record is dropped)."""
        from ..obsolete import guarded_mine, room_action, slots_needed

        record = self.state.get("power_repair") or {}
        for entity in record.get("remove", []):
            found = _find(observation, entity)
            if found is None:
                continue
            room = room_action(observation, slots_needed(self.catalog, [found]))
            if room is not None:
                return room
            action = guarded_mine(
                found, observation.get("world_id"), "repair the power plant water network: remove a misrouted pipe"
            )
            return action or _report("blocked", "built_block_repair_entity_has_no_unit:power", entity=entity)
        self.state.pop("power_repair", None)
        self._save()
        return None

    def _reserve_coal_feed(self, power: dict) -> None:
        """Choose the dedicated coal feed once, right after the plant stands, but do not build it.

        The plan is saved as ``coal_plan`` so every later planner keeps its
        route free (``power_coal_feed.publish_planned_feed`` puts it on the
        board). A failed choice does not stop electricity; the
        ``power_coal_feed`` step chooses again.
        """
        if "coal_plan" in self.state or "coal_feed_reservation_failed" in self.state:
            return
        plan = self._choose_coal_plan(power)
        if plan.get("ok"):
            self.state["coal_plan"] = plan
        else:
            self.state["coal_feed_reservation_failed"] = str(plan.get("reason", "coal feed planning failed"))[:200]
        self._save()

    def _ensure_hand_fed_power(self, observation: dict, power: dict) -> dict:
        """Phase 1: the plant runs on a boiler that the upkeep fuels by hand.

        Succeeds on the native plant evidence of ``power_flow_ready`` minus the
        feed drill and belt (``hand_fed_power_ready``), verified for 30 game
        seconds. The boiler's first and every later coal comes from the
        boiler upkeep (``power_coal_feed.boiler_fuel_action``), which runs in
        shared upkeep before this method.
        """
        if self.state.get("power_mode") != HAND_FED:
            self.state["power_mode"] = HAND_FED
            self._save()
        self._reserve_coal_feed(power)
        boiler = next(e for e in power["entities"] if e["name"] == "boiler")
        actual = _find(observation, boiler) or {}
        held = actual.get("inventory") or {}
        if not any(int(held.get(f, 0)) for f in ("coal", "wood")) and actual.get("remaining_burning_fuel", 0) <= 0:
            self.state.pop("power_sample_tick", None)
            self._save()
            return _report("waiting", "hand-fed boiler waits for coal from the boiler upkeep", boiler=boiler)
        evidence = self.power_evidence(power, None)
        if not evidence.get("ok"):
            return _report("blocked", "power evidence query failed", diagnostics=evidence)
        evidence = dict(
            evidence,
            hand_fed=True,
            coal_feed="planned" if "coal_plan" in self.state else "unplanned",
            boiler_full_load_coal_per_minute=boiler_coal_per_minute(self.catalog),
            coal_fuel_value=coal_fuel_value(self.catalog),
        )
        if not hand_fed_power_ready(evidence):
            self.state.pop("power_sample_tick", None)
            self._save()
            return _report("waiting", "waiting for water, steam and a fuelled hand-fed boiler", **evidence)
        tick = int(evidence.get("tick", observation.get("tick", 0)))
        first = self.state.setdefault("power_sample_tick", tick)
        self._save()
        if tick - first < 1800:
            return _report("waiting", "verifying hand-fed power supply over 30 game seconds", **evidence)
        self.state["power_verified_once"] = True
        self._save()
        return _report(
            "succeeded",
            "steam power runs on a hand-fed boiler; the coal drill and belt follow in power_coal_feed",
            **evidence,
            flow_verified=True,
            verification_ticks=tick - first,
            power_ports=[p for p in power["ports"] if p["kind"] == "power"],
        )

    def ensure_coal_feed(self, observation: dict) -> dict:
        """Phase 2 (capability ``power_coal_feed``): build and prove the dedicated coal drill and belt.

        Uses the reserved ``coal_plan`` (chosen again when the reservation
        failed). Success switches ``power_mode`` to ``BELT_FED``, which stops
        boiler hand-fuelling. A belt-fed state (old maps) is only re-verified.
        """
        self._sync(observation)
        power = self.state.get("power_plan")
        if not power:
            return _report("blocked", "power_coal_feed requires the built power plant")
        result = self.ensure_plan(observation, power)
        if result.get("status") != "succeeded":
            return result
        if "coal_plan" not in self.state:
            self.state.pop("coal_feed_reservation_failed", None)
            self._reserve_coal_feed(power)
            if "coal_plan" not in self.state:
                reason = self.state.pop("coal_feed_reservation_failed", "coal feed planning failed")
                self._save()
                return _report("blocked", reason)
        if not self.state.get("coal_feed_started"):
            self.state["coal_feed_started"] = True  # the feed is now part of the construction bill
            self._save()
        replannable = (
            coal_feed_mode(self.state) == HAND_FED
            and self.state.get("coal_feed_replans", 0) < COAL_FEED_REPLANS
            and not any(_built_as_planned(observation, e) for e in self.state["coal_plan"]["entities"])
        )
        # The whole unbuilt feed is checked before its first entity is built:
        # ensure_plan stops at the first taken tile, and by then the drill and
        # half the belt stood (seed 2005199778: a lab on the route near the
        # boiler), so the route could no longer be re-planned.
        taken = self._coal_feed_taken(observation) if replannable else None
        result = (
            _report("blocked", "reserved block placement is obstructed", **taken)
            if taken
            else self._ensure_belt_fed_power(observation, power, "coal_feed_sample_tick")
        )
        if replannable and result.get("reason") in COAL_FEED_TAKEN_REASONS:
            # Something was built on the reserved route while the boiler was
            # hand-fed (a starter cell, a lab; or a pending starter cell's
            # drill stands on the feed drill's tile, "direction differs").
            # Nothing of the feed stands yet, so choose a new route instead of
            # blocking.
            self.state.pop("coal_plan")
            self.state["coal_feed_replans"] = self.state.get("coal_feed_replans", 0) + 1
            self._save()
            return _report(
                "waiting",
                "reserved coal feed route is obstructed; planning a new route",
                obstruction=result.get("evidence"),
            )
        if result.get("status") != "succeeded":
            return result
        if self.state.get("power_mode") != BELT_FED:
            self.state["power_mode"] = BELT_FED
            self._save()
        return _report(
            "succeeded",
            "steam power has a dedicated self-fueled coal feed; boiler hand-fuelling stopped",
            **result["evidence"],
        )

    def _coal_feed_taken(self, observation: dict) -> dict | None:
        """Evidence of the first saved-feed entity another entity now blocks, or None.

        Native ``planning.preflight`` over the whole saved ``coal_plan`` (trees
        and rocks are clearable, not taken). Only used while nothing of the
        feed stands, so every observed collider belongs to another owner.
        """
        from factorio_agent.planning import preflight

        try:
            check = preflight(self.game, self, observation, self.state["coal_plan"]["entities"])
        except (KeyError, TypeError, ValueError):
            return None
        if check.get("ok") or check.get("reason") not in COAL_FEED_PREFLIGHT_TAKEN:
            return None
        taken = {k: check[k] for k in ("entity", "position") if k in check}
        return dict(taken, preflight_reason=str(check.get("reason", "coal_feed_preflight_failed")))

    def _ensure_belt_fed_power(self, observation: dict, power: dict, sample_key: str) -> dict:
        """Build/repair the dedicated coal feed and verify belt-fed flow (``power_flow_ready``)."""
        if "coal_plan" not in self.state:
            plan = self._choose_coal_plan(power)
            if not plan.get("ok"):
                return _report("blocked", plan["reason"], diagnostics=plan)
            self.state["coal_plan"] = plan
            self._save()
        coal = self.state["coal_plan"]
        # The electric coal cutover mines the burner drill and then places its
        # electric replacement. Between the two, rebuilding the burner from the
        # still-unrewritten plan would strand the cutover; wait instead.
        cutover = self.state.get("electric_coal_cutover")
        if cutover and _find(observation, cutover["old"]) is None:
            return _report("waiting", "electric coal cutover owns the coal drill site", cutover=cutover)
        boiler = next(e for e in power["entities"] if e["name"] == "boiler")
        # Start the coal drill as soon as it stands, before its belt is
        # finished: it mines while the belt is laid and the belt fills as it
        # grows (a full output simply pauses the drill, no fuel is wasted).
        # The early seed has its own key, so the startup seed below can fuel
        # the drill again if the first load ran out before the feed closed.
        if _find(observation, coal["drill"]) is not None:
            early = self._seed(observation, "coal_drill_early", coal["drill"], 8)
            if early and early.get("type"):
                return early
        result = self.ensure_plan(observation, coal)
        if result.get("status") != "succeeded":
            return result
        for key, entity, count in [
            ("coal_drill", coal["drill"], 8),
            ("boiler", boiler, 8),
        ]:
            seed = self._seed(observation, key, entity, count)
            if seed:
                return seed
        evidence = self.power_evidence(power, coal)
        if not evidence.get("ok"):
            return _report("blocked", "power evidence query failed", diagnostics=evidence)
        ready = power_flow_ready(evidence)
        if not ready:
            self.state.pop(sample_key, None)
            self._save()
            return _report("waiting", "waiting for dedicated coal, water, and steam flow", **evidence)
        tick = int(evidence.get("tick", observation.get("tick", 0)))
        first = self.state.setdefault(sample_key, tick)
        self._save()
        if tick - first < 1800:
            return _report("waiting", "verifying power supply over 30 game seconds", **evidence)
        # Historical capability only: retain it through starvation so a restarted
        # supervisor can initialize the coal-repair controller. Current flow must
        # still pass every measurement above before this method succeeds.
        self.state["power_verified_once"] = True
        self._save()
        return _report(
            "succeeded",
            "steam power has a dedicated self-fueled coal feed",
            **evidence,
            flow_verified=True,
            verification_ticks=tick - first,
            power_ports=[p for p in power["ports"] if p["kind"] == "power"],
        )
