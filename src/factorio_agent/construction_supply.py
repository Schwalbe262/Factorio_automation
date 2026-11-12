"""Bounded, ordinary-stock construction collection for the v2 executor.

The observation chooses candidates; a single native operation rechecks their
identities and live normal-quality stocks.  No source ingredients, fuel,
science inventory, raw ore or mining output are eligible for this fast path.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_bootstrap import DeterministicBootstrap


MAX_SOURCES = 16
MAX_ITEMS = 50
OUTPUT_NAMES = frozenset(
    {
        "stone-furnace",
        "steel-furnace",
        "electric-furnace",
        "assembling-machine-1",
        "assembling-machine-2",
        "assembling-machine-3",
    }
)
CHEST_NAMES = frozenset({"wooden-chest", "iron-chest", "steel-chest"})
CONSTRUCTION_ITEMS = frozenset(
    {
        "iron-plate",
        "copper-plate",
        "steel-plate",
        "stone-brick",
        "iron-gear-wheel",
        "electronic-circuit",
        "advanced-circuit",
        "processing-unit",
        "copper-cable",
        "transport-belt",
        "fast-transport-belt",
        "express-transport-belt",
        "underground-belt",
        "fast-underground-belt",
        "express-underground-belt",
        "splitter",
        "fast-splitter",
        "express-splitter",
        "inserter",
        "fast-inserter",
        "long-handed-inserter",
        "bulk-inserter",
        "stack-inserter",
        "burner-inserter",
        "gun-turret",
        "pipe",
        "pipe-to-ground",
    }
)
AMMO_ITEMS = frozenset(
    {
        "artillery-shell",
        "atomic-bomb",
        "cannon-shell",
        "capture-robot-rocket",
        "explosive-cannon-shell",
        "explosive-rocket",
        "explosive-uranium-cannon-shell",
        "firearm-magazine",
        "flamethrower-ammo",
        "piercing-rounds-magazine",
        "piercing-shotgun-shell",
        "railgun-ammo",
        "rocket",
        "shotgun-shell",
        "tesla-ammo",
        "uranium-cannon-shell",
        "uranium-rounds-magazine",
    }
)


def _position(value):
    return (
        isinstance(value, dict)
        and set(value) == {"x", "y"}
        and all(type(value[k]) in (int, float) and math.isfinite(value[k]) for k in ("x", "y"))
    )


def validate_take_many(action):
    """Reject the entire malformed envelope before any native I/O."""
    if (
        not isinstance(action, dict)
        or set(action) - {"type", "item", "count", "world_id", "sources", "reason"}
        or action.get("type") != "take_many"
        or not isinstance(action.get("item"), str)
        or action["item"] not in CONSTRUCTION_ITEMS
        or type(action.get("count")) is not int
        or not 1 <= action["count"] <= MAX_ITEMS
        or not isinstance(action.get("world_id"), str)
        or not action["world_id"]
        or not isinstance(action.get("sources"), list)
        or not 1 <= len(action["sources"]) <= MAX_SOURCES
        or ("reason" in action and not isinstance(action["reason"], str))
    ):
        raise ValueError("take_many requires a constructed item, global count 1..50, world and 1..16 sources")
    units = set()
    positions = set()
    for row in action["sources"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "position", "unit_number", "kind"}
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("kind"), str)
            or type(row.get("unit_number")) is not int
            or row["unit_number"] < 1
            or not _position(row.get("position"))
        ):
            raise ValueError("take_many sources require exact name, finite position, unit identity and stock kind")
        if not (
            (row["kind"] == "output" and row["name"] in OUTPUT_NAMES)
            or (row["kind"] == "chest" and row["name"] in CHEST_NAMES)
            or (row["kind"] == "belt" and row["name"] == "transport-belt")
        ):
            raise ValueError("take_many accepts only machine outputs, chests and ordinary transport belts")
        position = row["position"]["x"], row["position"]["y"]
        if row["unit_number"] in units or position in positions:
            raise ValueError("take_many sources must have distinct unit identities and positions")
        units.add(row["unit_number"])
        positions.add(position)


BURNERS = frozenset({"burner-mining-drill", "stone-furnace"})
FUELS = ("coal", "wood")
FUEL_BATCH = 8  # coal pieces worth one fetch-and-fuel trip
COLLECT_BATCH = 10  # construction items worth one collection trip
FUEL_SKIP_STOCK = 50  # finished items in a cell that make more fuel pointless
# Hand-craft cap: when more than this many finished items of one kind are
# missing and a verified, producing ``produce`` row makes that item
# (``machine_stock.MachineStock``), ``ensure_item`` collects the row's stock
# (batched ``take_many``) or waits for it instead of queueing a large hand
# craft. At or below the cap, or when the row is building, stalled or broken,
# or no row exists, hand crafting proceeds as before (never a deadlock); a
# fallback above the cap names the row state in the craft reason.
HANDCRAFT_LIMIT = 10


def _entity_key(entity):
    position = entity.get("position") or {}
    return (entity.get("name"), position.get("x"), position.get("y"))


def _produced_coal(observation, coal_cells):
    """Coal lying in the receivers (chests) of the given coal cells."""
    places = {
        (
            c["receiver"].get("name"),
            (c["receiver"].get("position") or {}).get("x"),
            (c["receiver"].get("position") or {}).get("y"),
        )
        for c in coal_cells
    }
    return sum(
        (e.get("inventory") or {}).get("coal", 0) for e in observation.get("entities", []) if _entity_key(e) in places
    )


class V2Bootstrap(DeterministicBootstrap):
    def _unreserved_stock_observation(self, observation, item):
        root = getattr(getattr(getattr(self, "game", None), "cfg", None), "runtime_dir", None)
        if not isinstance(root, (str, Path)):
            return observation
        path = Path(root) / "pcb-board.json"
        if not path.exists():
            return observation
        world = observation.get("world_id")
        fingerprint = getattr(self.catalog, "fingerprint", None)
        raw = path.read_bytes()
        version = (world, fingerprint, hashlib.sha256(raw).digest())
        cached = getattr(self, "_stock_ownership_cache", None)
        if cached is None or cached[0] != version:
            board = json.loads(raw.decode("utf-8"))
            if board.get("world_id") != world or board.get("catalog_fingerprint") != fingerprint:
                raise ValueError("construction stock ownership world or catalog changed")
            blocks = board.get("blocks", {})
            if not isinstance(blocks, dict):
                raise ValueError("invalid registered construction stock ownership")
            groups = list(blocks.items())
            if board.get("shared_main_bus"):
                groups.append(("shared_main_bus", board["shared_main_bus"]))
            ownership = {}
            for name, block in groups:
                for entity in block.get("entities", []):
                    p = entity.get("position")
                    if not _position(p):
                        raise ValueError("invalid registered stock position")
                    key = (entity["name"], p["x"], p["y"])
                    # Finite startup batches own their whole cell, including
                    # intermediates and the output used by their own courier.
                    public = {
                        port["item"]
                        for port in block.get("ports", [])
                        if name != "startup_science" and port.get("direction") == "output" and port.get("position") == p
                    }
                    ownership[key] = public if key not in ownership else ownership[key] & public
            self._stock_ownership_cache = (version, ownership)
        ownership = self._stock_ownership_cache[1]

        def available(entity):
            p = entity.get("position")
            if not _position(p):
                return True  # ordinary candidate validation still rejects malformed rows
            allowed = ownership.get((entity.get("name"), p["x"], p["y"]))
            return allowed is None or item in allowed

        return dict(observation, entities=[e for e in observation.get("entities", []) if available(e)])

    def maintain_fuel(self, observation):
        """Hand-fuel burners only where it pays, and only in batches.

        Rules, in order:
        - A burner whose output is backed up, or whose cell already holds
          FUEL_SKIP_STOCK finished items, gets no fuel: burning coal to make
          plates nobody is taking starves the coal drills and the boiler.
        - Only a burner that is out of fuel (or about to be) is urgent.
        - Coal producers are fuelled before coal consumers.
        - With no coal in hand, wait until FUEL_BATCH coal has piled up at a
          running coal drill instead of fetching it one piece at a time. The
          primary job keeps working meanwhile.
        """
        skipped = None

        def urgent(entity):
            if entity.get("name") not in BURNERS:
                return True
            if entity.get("status_name") in {"full_output", "waiting_for_space", "waiting_for_space_in_destination"}:
                return False
            fuel = entity.get("inventory") or {}
            if fuel.get("coal", 0) + fuel.get("wood", 0) > 0:
                return False
            if entity.get("remaining_burning_fuel", 0) > 1_000_000:
                return False
            return _entity_key(entity) not in skipped

        def hungry(rows):
            return any(
                e.get("name") in BURNERS and sum((e.get("inventory") or {}).get(item, 0) for item in FUELS) < 2
                for e in rows
            )

        skipped = set()
        if not hungry([e for e in observation.get("entities", []) if urgent(e)]):
            return None
        skipped = self._well_stocked_burners(observation)
        entities = [e for e in observation.get("entities", []) if urgent(e)]
        if not hungry(entities):
            return None
        observation = dict(observation, entities=entities)
        survey = self._existing_cells("coal")
        if not survey.get("ok"):
            return {"status": "blocked", "reason": "starter_coal_priority_survey_failed"}
        coal_cells = [
            c
            for c in survey.get("cells", [])
            if not c.get("electric") and c.get("receiver") and c.get("remaining", 0) > 0
        ]
        producers = {(c["drill"]["name"], c["drill"]["position"]["x"], c["drill"]["position"]["y"]) for c in coal_cells}
        carried = self._count(observation, "coal")
        if carried < FUEL_BATCH and any(c.get("fuel", 0) > 0 or c.get("burning") for c in coal_cells):
            # Less than a batch in hand while coal is still being mined: top up
            # in one trip once a batch has piled up, otherwise leave the few
            # pieces alone. Spending them one at a time is the loop to avoid.
            if _produced_coal(observation, coal_cells) < FUEL_BATCH:
                return None
            return self.ensure_item(observation, "coal", carried + 3 * FUEL_BATCH)
        # Keep a real coal producer running before spending the last carried
        # fuel on its consumers. The base routine retains ordinary partial
        # transfers and its existing bounded emergency mining behavior.
        ordered = sorted(entities, key=lambda e: _entity_key(e) not in producers)
        return super().maintain_fuel(dict(observation, entities=ordered))

    def _well_stocked_burners(self, observation):
        """Drills and furnaces of non-coal cells that already hold enough finished output."""
        by_place = {
            (e.get("name"), (e.get("position") or {}).get("x"), (e.get("position") or {}).get("y")): e
            for e in observation.get("entities", [])
        }
        stocked = set()
        for e in observation.get("entities", []):
            if e.get("name") == "stone-furnace" and sum((e.get("output_inventory") or {}).values()) >= FUEL_SKIP_STOCK:
                stocked.add(_entity_key(e))
        for resource in observation.get("resources") or {}:
            if resource in FUELS:
                continue
            survey = self._existing_cells(resource)
            if not survey.get("ok"):
                continue
            for cell in survey.get("cells", []):
                receiver = cell.get("receiver") or {}
                place = receiver.get("position") or {}
                held = by_place.get((receiver.get("name"), place.get("x"), place.get("y")))
                if held is None:
                    continue
                finished = held.get("output_inventory")
                if finished is None:
                    finished = {k: v for k, v in (held.get("inventory") or {}).items() if k not in FUELS}
                if sum(finished.values()) >= FUEL_SKIP_STOCK:
                    drill = cell["drill"]
                    stocked.add((drill["name"], drill["position"]["x"], drill["position"]["y"]))
        return stocked

    def _ensure_cell(self, observation, resource, receiver_name):
        result = super()._ensure_cell(observation, resource, receiver_name)
        cleanup = getattr(self, "cell_site_cleanup", None)
        if result and result.get("type") == "build" and cleanup is not None:
            recovery = cleanup(observation, {"entities": []}, result)
            if recovery is not None:
                return recovery
        return result

    def ensure_item(self, observation, item, count, _stack=()):
        source = getattr(self, "construction_stock_source", None)
        if source is not None and self._count(observation, item) < count:
            supplied = source(observation, item, count)
            if supplied is not None:
                if not isinstance(supplied, dict) or not (
                    supplied.get("type") == "take_mall_stock"
                    or (not supplied.get("type") and supplied.get("status") in {"waiting", "blocked"})
                ):
                    raise ValueError("construction stock provider requires guarded mall collection or an explicit wait")
                if supplied.get("type") == "take_mall_stock" and (
                    supplied.get("item") != item
                    or type(supplied.get("count")) is not int
                    or not 1 <= supplied["count"] <= min(50, count - self._count(observation, item))
                ):
                    raise ValueError("mall collection must match the requested missing stock")
                return supplied
        missing = count - self._count(observation, item)
        hook = getattr(self, "machine_stock", None)
        machine = hook(observation, item) if hook is not None and missing > 0 else None
        if machine is not None and machine.get("status") == "producing":
            collect = self._take_output(observation, item, missing)
            if collect is not None:
                return collect
            if missing > HANDCRAFT_LIMIT:
                return {
                    "status": "waiting",
                    "reason": f"waiting for machine-made {item}",
                    "evidence": {"item": item, "missing": missing, "limit": HANDCRAFT_LIMIT, "machine": machine},
                }
        result = super().ensure_item(observation, item, count, _stack)
        if (
            machine is not None
            and machine.get("status") != "producing"
            and missing > HANDCRAFT_LIMIT
            and isinstance(result, dict)
            and result.get("type") == "craft"
        ):
            note = f"; machine-made {item} {machine.get('status')}: hand-craft fallback"
            result = dict(result, reason=(str(result.get("reason") or "") + note)[:300])
        return result

    def _fuel_is_piling_up(self, item):
        """True while some drill is still filling a chest with this fuel.

        Waiting for a batch only makes sense if the batch can arrive. When
        every chest-feeding drill is out of fuel, the few pieces left are the
        only way to restart one; on a live map all eleven burners sat idle
        beside a chest of five coal. A survey failure counts as not piling up.
        """
        survey = self._existing_cells(item)
        if not isinstance(survey, dict) or not survey.get("ok"):
            return False
        return any(
            cell.get("receiver") and (cell.get("fuel", 0) > 0 or cell.get("burning") or cell.get("operating"))
            for cell in survey.get("cells", [])
        )

    def _take_output(self, observation, item, count):
        catalog_item = getattr(self.catalog, "items", {}).get(item, {})
        if item in AMMO_ITEMS or catalog_item.get("type") == "ammo":
            # Generic item-count transfers erase remaining-round metadata. Let
            # ensure_item use ordinary engine crafting for fresh full magazines.
            # Even a machine output could contain a manually stored used stack.
            return None
        observation = self._unreserved_stock_observation(observation, item)
        if item not in CONSTRUCTION_ITEMS:
            action = super()._take_output(observation, item, count)
            if (
                item in FUELS
                and action is not None
                and type(action.get("count")) is int
                and action["count"] < min(count, FUEL_BATCH)
                and self._fuel_is_piling_up(item)
            ):
                # Any job that needs fuel waits for a batch instead of taking
                # single pieces; callers then report the ordinary supply wait.
                return None
            return action
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
            verified = (
                kind == "chest"
                and buffer.get("world_id") == world
                and buffer.get("unit_number") == unit
                and buffer.get("name") == name
                and buffer.get("position") == position
            )
            # Include a verified buffer even when its snapshot was empty: it may
            # have filled, and an empty live buffer never stops the rest of the sweep.
            if stock > 0 or verified:
                candidates.append(
                    (
                        not verified,
                        -stock,
                        unit,
                        {"name": name, "position": dict(position), "unit_number": unit, "kind": kind},
                    )
                )
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
        # Fetching each plate as it leaves the furnace costs one action per
        # plate. Let a batch pile up unless less than a batch is needed or a
        # registered buffer chest (whose snapshot may be stale) is involved.
        on_hand = sum(-row[1] for row in candidates)
        if on_hand < min(count, COLLECT_BATCH) and all(row[0] for row in candidates):
            return None
        return {
            "type": "take_many",
            "item": item,
            "count": min(count, MAX_ITEMS),
            "world_id": world,
            "sources": sources,
            "reason": f"collect live construction stocks {item}",
        }


TAKE_MANY_LUA = r"""
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
"""


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
    receipt = game.query(
        "local x=helpers.json_to_table("
        + encoded
        + ");local character_backend="
        + ("true" if backend == "character" else "false")
        + ";"
        + TAKE_MANY_LUA
    )
    if not receipt.get("ok"):
        return receipt
    rows = receipt.get("sources")
    if rows == {}:  # Factorio encodes an empty Lua array as an object.
        rows = []
        receipt = dict(receipt, sources=rows)
    moved = receipt.get("moved")
    allowed = {s["unit_number"] for s in action["sources"]}
    if (
        type(moved) is not int
        or not 0 <= moved <= action["count"]
        or not isinstance(rows, list)
        or len(rows) > len(allowed)
        or any(
            not isinstance(row, dict)
            or row.get("unit_number") not in allowed
            or type(row.get("moved")) is not int
            or row["moved"] < 0
            for row in rows
        )
        or len({row["unit_number"] for row in rows}) != len(rows)
        or sum(row["moved"] for row in rows) != moved
        or receipt.get("status") != ("succeeded" if moved else "waiting")
    ):
        # The operation may have happened. Never conceal or automatically retry
        # an inconsistent/unknown result; the job owner must reconcile inventory.
        raise RuntimeError("take_many receipt violates aggregate conservation; reconcile before retry")
    return receipt
