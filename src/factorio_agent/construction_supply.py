"""Bounded, ordinary-stock construction collection for the v2 executor.

The observation chooses candidates; a single native operation rechecks their
identities and live normal-quality stocks.  No source ingredients, fuel,
science inventory, raw ore or mining output are eligible for this fast path.
"""
from __future__ import annotations

import json
import math

from factorio_ai.deterministic_bootstrap import DeterministicBootstrap


MAX_SOURCES = 16
MAX_ITEMS = 50
OUTPUT_NAMES = frozenset({"stone-furnace", "steel-furnace", "electric-furnace",
                          "assembling-machine-1", "assembling-machine-2", "assembling-machine-3"})
CHEST_NAMES = frozenset({"wooden-chest", "iron-chest", "steel-chest"})
CONSTRUCTION_ITEMS = frozenset({
    "iron-plate", "copper-plate", "steel-plate", "stone-brick", "iron-gear-wheel",
    "electronic-circuit", "advanced-circuit", "processing-unit", "copper-cable",
    "transport-belt", "fast-transport-belt", "express-transport-belt",
    "underground-belt", "fast-underground-belt", "express-underground-belt",
    "splitter", "fast-splitter", "express-splitter", "inserter", "fast-inserter",
    "long-handed-inserter", "bulk-inserter", "stack-inserter", "burner-inserter", "gun-turret",
    "pipe", "pipe-to-ground",
})
AMMO_ITEMS = frozenset({
    "artillery-shell", "atomic-bomb", "cannon-shell", "capture-robot-rocket",
    "explosive-cannon-shell", "explosive-rocket", "explosive-uranium-cannon-shell",
    "firearm-magazine", "flamethrower-ammo", "piercing-rounds-magazine",
    "piercing-shotgun-shell", "railgun-ammo", "rocket", "shotgun-shell",
    "tesla-ammo", "uranium-cannon-shell", "uranium-rounds-magazine",
})


def _position(value):
    return (isinstance(value, dict) and set(value) == {"x", "y"}
            and all(type(value[k]) in (int, float) and math.isfinite(value[k]) for k in ("x", "y")))


def validate_take_many(action):
    """Reject the entire malformed envelope before any native I/O."""
    if (not isinstance(action, dict)
            or set(action)-{"type", "item", "count", "world_id", "sources", "reason"}
            or action.get("type") != "take_many"
            or not isinstance(action.get("item"), str) or action["item"] not in CONSTRUCTION_ITEMS
            or type(action.get("count")) is not int or not 1 <= action["count"] <= MAX_ITEMS
            or not isinstance(action.get("world_id"), str) or not action["world_id"]
            or not isinstance(action.get("sources"), list) or not 1 <= len(action["sources"]) <= MAX_SOURCES
            or ("reason" in action and not isinstance(action["reason"], str))):
        raise ValueError("take_many requires a constructed item, global count 1..50, world and 1..16 sources")
    units = set()
    positions = set()
    for row in action["sources"]:
        if (not isinstance(row, dict) or set(row) != {"name", "position", "unit_number", "kind"}
                or not isinstance(row.get("name"), str) or not isinstance(row.get("kind"), str)
                or type(row.get("unit_number")) is not int or row["unit_number"] < 1
                or not _position(row.get("position"))):
            raise ValueError("take_many sources require exact name, finite position, unit identity and stock kind")
        if not ((row["kind"] == "output" and row["name"] in OUTPUT_NAMES)
                or (row["kind"] == "chest" and row["name"] in CHEST_NAMES)
                or (row["kind"] == "belt" and row["name"] == "transport-belt")):
            raise ValueError("take_many accepts only machine outputs, chests and ordinary transport belts")
        position = row["position"]["x"], row["position"]["y"]
        if row["unit_number"] in units or position in positions:
            raise ValueError("take_many sources must have distinct unit identities and positions")
        units.add(row["unit_number"])
        positions.add(position)


class V2Bootstrap(DeterministicBootstrap):
    def ensure_item(self, observation, item, count, _stack=()):
        source = getattr(self, 'construction_stock_source', None)
        if source is not None and self._count(observation, item) < count:
            supplied = source(observation, item, count)
            if supplied is not None:
                if (not isinstance(supplied, dict)
                        or not (supplied.get('type') == 'take_mall_stock'
                                or (not supplied.get('type') and supplied.get('status') in {'waiting', 'blocked'}))):
                    raise ValueError('construction stock provider requires guarded mall collection or an explicit wait')
                if supplied.get('type') == 'take_mall_stock' and (
                        supplied.get('item') != item or type(supplied.get('count')) is not int
                        or not 1 <= supplied['count'] <= min(50, count-self._count(observation, item))):
                    raise ValueError('mall collection must match the requested missing stock')
                return supplied
        return super().ensure_item(observation, item, count, _stack)

    def _take_output(self, observation, item, count):
        catalog_item = getattr(self.catalog, "items", {}).get(item, {})
        if item in AMMO_ITEMS or catalog_item.get("type") == "ammo":
            # Generic item-count transfers erase remaining-round metadata. Let
            # ensure_item use ordinary engine crafting for fresh full magazines.
            # Even a machine output could contain a manually stored used stack.
            return None
        if item not in CONSTRUCTION_ITEMS:
            return super()._take_output(observation, item, count)
        if type(count) is not int or count < 1:
            return None
        world = observation.get("world_id")
        if not isinstance(world, str) or not world:
            return None
        buffer = self.construction_buffers.get(item) or {}
        candidates = []
        for e in observation.get("entities", []):
            name = e.get("name")
            if name in OUTPUT_NAMES:
                kind, inventory = "output", e.get("output_inventory") or {}
            elif name in CHEST_NAMES:
                kind, inventory = "chest", e.get("inventory") or {}
            elif name == "transport-belt":
                kind, inventory = "belt", e.get("belt_inventory") or {}
            else:
                continue
            unit, position = e.get("unit_number"), e.get("position")
            if type(unit) is not int or unit < 1 or not _position(position):
                continue
            stock = inventory.get(item, 0)
            if type(stock) not in (int, float) or not math.isfinite(stock) or stock < 0:
                continue
            verified = (kind == "chest" and buffer.get("world_id") == world
                        and buffer.get("unit_number") == unit and buffer.get("name") == name
                        and buffer.get("position") == position)
            # Include a verified buffer even when its snapshot was empty: it may
            # have filled, and an empty live buffer never stops the rest of the sweep.
            if stock > 0 or verified:
                candidates.append((not verified, -stock, unit, {"name": name, "position": dict(position),
                                                               "unit_number": unit, "kind": kind}))
        candidates.sort(key=lambda row: row[:3])
        sources = []
        units, positions = set(), set()
        for _, _, unit, source in candidates:
            position = source["position"]["x"], source["position"]["y"]
            if unit in units or position in positions:
                continue
            sources.append(source)
            units.add(unit)
            positions.add(position)
            if len(sources) == MAX_SOURCES:
                break
        if not sources:
            return None
        return {"type": "take_many", "item": item, "count": min(count, MAX_ITEMS),
                "world_id": world, "sources": sources,
                "reason": f"collect live construction stocks {item}"}


TAKE_MANY_LUA = r'''
if not d or d.world_id~=x.world_id then return failure("take_many_world_changed") end
if not a or not a.valid or a.force~=f then return failure("agent_dead_or_force_changed") end
local inv=a.get_main_inventory()
if not inv or not inv.valid then return failure("character_main_inventory_missing") end
if not prototypes.item[x.item] then return failure("take_many_item_missing") end
local id={name=x.item,quality="normal"}
local total=0;local rows={}
for _,spec in ipairs(x.sources) do
 if total>=x.count or inv.get_insertable_count(id)<1 then break end
 local row={name=spec.name,unit_number=spec.unit_number,position=spec.position,moved=0}
 rows[#rows+1]=row
 local e=target(spec.position,spec.name)
 if not e or not e.valid or e.unit_number~=spec.unit_number or e.force~=f or e.surface~=s
    or e.position.x~=spec.position.x or e.position.y~=spec.position.y then
  row.status="skipped";row.reason="source_identity_changed"
 elseif character_backend and not a.can_reach_entity(e) then
  row.status="skipped";row.reason="out_of_reach"
 else
  local source=nil;local first=nil;local second=nil;local available=0
  if spec.kind=="belt" and e.type=="transport-belt" then
   first=e.get_transport_line(1);second=e.get_transport_line(2)
   available=first.get_item_count(id)+second.get_item_count(id)
  elseif spec.kind=="output" and (e.type=="furnace" or e.type=="assembling-machine") then
   source=e.get_output_inventory()
   if source then available=source.get_item_count(id) end
  elseif spec.kind=="chest" and e.type=="container" then
   source=e.get_inventory(defines.inventory.chest)
   if source then available=source.get_item_count(id) end
  end
  row.available=available
  local n=math.min(x.count-total,inv.get_insertable_count(id),available)
  if n<1 then row.status="skipped";row.reason="no_normal_stock_or_capacity"
  else
   local inserted=inv.insert{name=x.item,quality="normal",count=n}
   local removed=0
   if inserted>0 and first then
    removed=first.remove_item{name=x.item,quality="normal",count=inserted}
    if removed<inserted then removed=removed+second.remove_item{name=x.item,quality="normal",count=inserted-removed} end
   elseif inserted>0 and source then removed=source.remove{name=x.item,quality="normal",count=inserted} end
   if removed<inserted then
    local rolled=inv.remove{name=x.item,quality="normal",count=inserted-removed}
    if rolled~=inserted-removed then return failure("take_many_conservation_rollback_failed") end
   end
   row.moved=removed;row.status=removed>0 and "succeeded" or "skipped"
   total=total+removed
  end
 end
end
return success{status=total>0 and "succeeded" or "waiting",moved=total,requested=x.count,sources=rows}
'''


def execute_take_many(game, action):
    """One native query; no retry after an uncertain mutation or transport error."""
    validate_take_many(action)
    expected = getattr(game, "expected_world", None)
    if expected is not None and expected != action["world_id"]:
        raise ValueError("take_many must target the bound world")
    backend = getattr(game, "backend", "assisted")
    if backend not in ("assisted", "character"):
        raise ValueError("take_many requires an ordinary supported backend")
    encoded = json.dumps(json.dumps(action, separators=(",", ":")))
    receipt = game.query("local x=helpers.json_to_table("+encoded+");local character_backend="
                         +("true" if backend == "character" else "false")+";"+TAKE_MANY_LUA)
    if not receipt.get("ok"):
        return receipt
    rows = receipt.get("sources")
    if rows == {}:  # Factorio encodes an empty Lua array as an object.
        rows = []
        receipt = dict(receipt, sources=rows)
    moved = receipt.get("moved")
    allowed = {s["unit_number"] for s in action["sources"]}
    if (type(moved) is not int or not 0 <= moved <= action["count"] or not isinstance(rows, list)
            or len(rows) > len(allowed)
            or any(not isinstance(row, dict) or row.get("unit_number") not in allowed
                   or type(row.get("moved")) is not int or row["moved"] < 0 for row in rows)
            or len({row["unit_number"] for row in rows}) != len(rows)
            or sum(row["moved"] for row in rows) != moved
            or receipt.get("status") != ("succeeded" if moved else "waiting")):
        # The operation may have happened. Never conceal or automatically retry
        # an inconsistent/unknown result; the job owner must reconcile inventory.
        raise RuntimeError("take_many receipt violates aggregate conservation; reconcile before retry")
    return receipt
