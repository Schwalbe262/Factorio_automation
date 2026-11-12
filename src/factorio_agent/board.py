"""PCB-style block reservation and bounded, whole-layout native preflight."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.core.deterministic_state import _atomic_json


def entity(name, x, y, direction=0, recipe=None):
    result = {"name": name, "position": {"x": x, "y": y}, "direction": direction}
    if recipe:
        result["recipe"] = recipe
    return result


MAX_STARTUP_SCIENCE_MACHINES = 2
# Labs the startup courier may feed: the starter lab plus up to three slots.
MAX_STARTUP_LABS = 4
# Lab slot centres and their extra poles, relative to the cell origin. The
# slots sit in the free east columns of the 16x16 reservation for both layouts.
LAB_SLOT_OFFSETS = ((4, -3), (4, 0), (4, 3))
LAB_SLOT_POLES = {1: ((2, -3),), MAX_STARTUP_SCIENCE_MACHINES: ()}
# The pole a grid chain connects to, per layout.
POWER_TARGET_OFFSETS = {1: (2, 1), MAX_STARTUP_SCIENCE_MACHINES: (2, 2)}


def startup_science_cell(x, y, *, science_machines=1):
    """A fixed-port, normally powered gear -> red-science machine cell.

    Three chest ports are finite bootstrap buffers, not automated raw supply.
    All transfers inside the cell use real powered inserters.

    ``science_machines=1`` is the original layout (unchanged). With two, a
    shared hub chest (the copper port) receives gears from the gear machine
    and copper from funding; one inserter feeds each science machine from it
    and both write into the one output chest (see ``_hub_science_cell``).
    """
    if science_machines == MAX_STARTUP_SCIENCE_MACHINES:
        return _hub_science_cell(x, y)
    if science_machines != 1:
        raise ValueError("startup science cell holds 1 or 2 science machines")
    entities = [
        entity("assembling-machine-1", x, y - 4, recipe="iron-gear-wheel"),
        entity("assembling-machine-1", x, y, recipe="automation-science-pack"),
        entity("wooden-chest", x - 3, y - 4),
        entity("wooden-chest", x - 3, y),
        entity("wooden-chest", x, y + 3),
        entity("inserter", x - 2, y - 4, 12),
        entity("inserter", x - 2, y, 12),
        entity("inserter", x, y - 2, 0),
        entity("inserter", x, y + 2, 0),
        entity("small-electric-pole", x - 2, y - 2),
        entity("small-electric-pole", x + 2, y + 1),
    ]
    ports = [
        {"item": "iron-plate", "kind": "item", "direction": "input", "position": {"x": x - 3, "y": y - 4}},
        {"item": "copper-plate", "kind": "item", "direction": "input", "position": {"x": x - 3, "y": y}},
        {"item": "automation-science-pack", "kind": "item", "direction": "output", "position": {"x": x, "y": y + 3}},
    ]
    return _plan(entities, ports, template="startup_science_cell", layout_policy="pcb-block-v1")


def _hub_science_cell(x, y):
    """Two science machines around one hub chest at ``(x, y)``.

    Geometry (offsets from the hub; inserter direction = pickup side,
    0 north, 4 east, 8 south, 12 west):

    - iron chest (-6, 0) -> inserter (-5, 0) dir 12 -> gear machine (-3, 0)
    - gear machine -> inserter (-1, 0) dir 12 -> hub chest (0, 0), which is
      also the copper port
    - hub -> inserter (0, -1) dir 8 -> science machine (0, -3)
    - hub -> inserter (0, 1) dir 0 -> science machine (0, 3)
    - science (0, -3) -> inserter (1, -1) dir 0 -> output chest (1, 0)
    - science (0, 3) -> inserter (1, 1) dir 8 -> output chest (1, 0)
    - poles (-5, -1), (-1, -1), (2, -2), (2, 2): every machine and inserter
      overlaps a 5x5 supply area; hops are at most 4.1 tiles.

    Wrong ingredients never move: an inserter only feeds what the recipe
    takes, so gears and copper share the hub safely. One gear machine makes
    60 gears/min, ten times what two science machines use.
    """
    red = "automation-science-pack"
    entities = [
        entity("assembling-machine-1", x - 3, y, recipe="iron-gear-wheel"),
        entity("assembling-machine-1", x, y - 3, recipe=red),
        entity("assembling-machine-1", x, y + 3, recipe=red),
        entity("wooden-chest", x - 6, y),
        entity("wooden-chest", x, y),
        entity("wooden-chest", x + 1, y),
        entity("inserter", x - 5, y, 12),
        entity("inserter", x - 1, y, 12),
        entity("inserter", x, y - 1, 8),
        entity("inserter", x, y + 1, 0),
        entity("inserter", x + 1, y - 1, 0),
        entity("inserter", x + 1, y + 1, 8),
        entity("small-electric-pole", x - 5, y - 1),
        entity("small-electric-pole", x - 1, y - 1),
        entity("small-electric-pole", x + 2, y - 2),
        entity("small-electric-pole", x + 2, y + 2),
    ]
    ports = [
        {"item": "iron-plate", "kind": "item", "direction": "input", "position": {"x": x - 6, "y": y}},
        {"item": "copper-plate", "kind": "item", "direction": "input", "position": {"x": x, "y": y}},
        {"item": red, "kind": "item", "direction": "output", "position": {"x": x + 1, "y": y}},
    ]
    return _plan(
        entities,
        ports,
        template="startup_science_cell",
        layout_policy="pcb-block-v1",
        science_machines=MAX_STARTUP_SCIENCE_MACHINES,
        cell_origin={"x": x, "y": y},
    )


def science_machine_count(plan):
    """Science machines of a saved cell; a cell without the field is the original one-machine cell."""
    count = plan.get("science_machines", 1)
    return count if type(count) is int and 1 <= count <= MAX_STARTUP_SCIENCE_MACHINES else None


def cell_origin(plan):
    """The layout origin: saved for new cells, else the original cell's science machine."""
    origin = plan.get("cell_origin")
    if isinstance(origin, dict):
        return origin
    red = next(
        (e for e in plan.get("entities", []) if e.get("recipe") == "automation-science-pack"),
        None,
    )
    return red["position"] if red else None


def lab_slot_entities(plan):
    """Planned extra labs (and the poles that power them) for a cell, in slot order.

    Pure geometry: slots that collide with the cell's own entities (including
    its grid pole chain) or leave the reserved area are dropped. Placement is
    still checked natively by the builder before anything is built.
    """
    count = science_machine_count(plan)
    origin = cell_origin(plan)
    if count is None or origin is None:
        return []
    x, y = origin["x"], origin["y"]
    geometry = FactoryBuilder.__new__(FactoryBuilder)
    taken = set().union(*[geometry._occupied_by_plan([e]) for e in plan.get("entities", [])] or [set()])
    area = plan.get("reserved_area")
    slots = []
    poles = [entity("small-electric-pole", x + dx, y + dy) for dx, dy in LAB_SLOT_POLES[count]]
    for pole in poles:
        if geometry._occupied_by_plan([pole]) & taken:
            return []
    for dx, dy in LAB_SLOT_OFFSETS:
        lab = entity("lab", x + dx, y + dy)
        tiles = geometry._occupied_by_plan([lab])
        if tiles & taken:
            continue
        if area and not all(_inside(area, tile) for tile in tiles):
            continue
        slots.append(lab)
        taken |= tiles
    return [{"lab": lab, "poles": poles} for lab in slots]


def _inside(area, tile):
    """Tile centre ``tile`` lies wholly inside ``area`` ([[x0, y0], [x1, y1]])."""
    (x0, y0), (x1, y1) = area
    tx, ty = tile
    return x0 <= tx - 0.5 and tx + 0.5 <= x1 and y0 <= ty - 0.5 and ty + 0.5 <= y1


def lab_slot_plan(plan, count):
    """A builder plan for the first ``count`` lab slots of a cell, or None when there are none."""
    slots = lab_slot_entities(plan)[: max(0, count)]
    if not slots:
        return None
    entities = list(slots[0]["poles"]) + [slot["lab"] for slot in slots]
    return _plan(entities, [], template="startup_science_labs", layout_policy="pcb-block-v1")


def pole_chain(source, destination, *, horizontal_first=True):
    """Straight wire hops of at most five tiles; wire itself may cross machines."""
    x, y = source["x"], source["y"]
    result = []
    for axis in (0, 1) if horizontal_first else (1, 0):
        target = destination["x"] if axis == 0 else destination["y"]
        current = x if axis == 0 else y
        while abs(target - current) > 1e-6:
            current += max(-5, min(5, target - current))
            if axis == 0:
                x = current
            else:
                y = current
            result.append(entity("small-electric-pole", x, y))
    return result


class Board:
    def __init__(self, game, builder, catalog):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.path = Path(game.cfg.runtime_dir) / "pcb-board.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def reserve_startup_science(self, obs, *, science_machines=1):
        """Reserve the startup science cell once per world; later calls return the saved plan.

        ``science_machines`` (1 or 2) sizes a new cell only. Lab slots are
        surveyed with the cell; a site whose slots are all placeable wins,
        else the first legal site is used and blocked slots stay unbuilt.
        """
        if self.state and self.state.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "board_world_changed"}
        if not self.state:
            self.state = {
                "schema_version": 1,
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "blocks": {},
            }
        if self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"ok": False, "reason": "board_catalog_changed_requires_revalidation"}
        if "startup_science" in self.state["blocks"]:
            return deepcopy(self.state["blocks"]["startup_science"])
        networks = {
            e.get("electric_network_id")
            for e in obs["entities"]
            if e["name"] == "steam-engine" and e.get("energy", 0) > 0
        }
        poles = [
            e
            for e in obs["entities"]
            if e["name"] == "small-electric-pole" and e.get("electric_network_id") in networks
        ]
        if not poles:
            return {"ok": False, "reason": "no_energized_source_network"}
        reference = poles[0]["position"]
        bx = math.floor((reference["x"] - 8) / 8) * 8 + 0.5
        by = math.floor(reference["y"] / 8) * 8 + 0.5
        offsets = [(0, 0), (-16, 0), (0, 16), (-16, 16), (0, -16), (-16, -16), (-32, 0), (-32, 16), (16, 0), (16, 16)]
        candidates = []
        tx, ty = POWER_TARGET_OFFSETS[science_machines]
        for dx, dy in offsets:
            x, y = bx + dx, by + dy
            base = startup_science_cell(x, y, science_machines=science_machines)
            target = {"x": x + tx, "y": y + ty}
            source = min(
                poles, key=lambda e: abs(e["position"]["x"] - target["x"]) + abs(e["position"]["y"] - target["y"])
            )
            for horizontal_first in (True, False):
                plan = deepcopy(base)
                plan["entities"] += pole_chain(source["position"], target, horizontal_first=horizontal_first)
                unique = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in plan["entities"]}
                extra = {k: base[k] for k in ("science_machines", "cell_origin") if k in base}
                plan = _plan(
                    list(unique.values()),
                    plan["ports"],
                    template="startup_science_cell",
                    layout_policy="pcb-block-v1",
                    reserved_area=[[x - 8.5, y - 8.5], [x + 7.5, y + 7.5]],
                    **extra,
                )
                # Whole prospective footprint must not overlap itself.
                footprints = [self.builder._occupied_by_plan([e]) for e in plan["entities"]]
                if sum(map(len, footprints)) != len(set().union(*footprints)):
                    continue
                candidates.append(plan)
        surveys = []
        for plan in candidates:
            slots = lab_slot_entities(plan)
            slot_entities = (slots[0]["poles"] if slots else []) + [slot["lab"] for slot in slots]
            surveys.append(dict(plan, slots=slot_entities))
        payload = json.dumps(json.dumps(surveys, separators=(",", ":")))
        surveyed = self.game.query(
            """
local plans=helpers.json_to_table("""
            + payload
            + """);local out={};local roomy={}
for i,p in ipairs(plans) do
 local legal=true
 if s.count_entities_filtered{area=p.reserved_area,type="resource"}>0 then legal=false end
 if legal then
  for _,e in pairs(s.find_entities_filtered{area=p.reserved_area,force=f}) do
   local q=e.position;local lo=p.reserved_area[1];local hi=p.reserved_area[2]
   if e.type~="character" and e.type~="electric-pole" and q.x>lo[1]+.5 and q.x<hi[1]-.5 and q.y>lo[2]+.5 and q.y<hi[2]-.5 then legal=false;break end
  end
 end
 if legal then for _,e in ipairs(p.entities) do
  local old=target(e.position,e.name)
  if not (old and old.force==f and old.type=="electric-pole")
   and not s.can_place_entity{name=e.name,position=e.position,direction=e.direction,force=f} then legal=false;break end
 end end
 if legal then
  out[#out+1]=i
  local free=true
  for _,e in ipairs(p.slots or {}) do
   if not s.can_place_entity{name=e.name,position=e.position,direction=e.direction,force=f} then free=false;break end
  end
  if free then roomy[#roomy+1]=i end
 end
end
return {ok=true,legal=out,roomy=roomy}
"""
        )
        if not surveyed.get("ok"):
            return {"ok": False, "reason": surveyed.get("reason", "block_survey_failed")}
        indices = surveyed.get("legal", [])
        if not indices:
            return {"ok": False, "reason": "no_clear_reserved_science_slot"}
        roomy = [i for i in surveyed.get("roomy") or [] if i in indices]
        plan = candidates[(roomy or indices)[0] - 1]
        self.state["blocks"]["startup_science"] = plan
        _atomic_json(self.path, self.state)
        return deepcopy(plan)
