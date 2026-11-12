"""PCB-style block reservation and bounded, whole-layout native preflight."""

from __future__ import annotations

from copy import deepcopy
import hashlib
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


# --- growing a built one-machine cell -------------------------------------------
#
# The two-machine hub layout moves the gear machine and the chests, so it is
# not a superset of the original cell. An already built one-machine cell grows
# with the "west" extension instead: every original entity stays where it is
# and a second science machine is added west of the copper port.
#
# Offsets from the original science machine A at (0, 0); inserter direction is
# the pickup side (0 north, 4 east, 8 south, 12 west):
#
#          x: -7 -6 -5 -4 -3 -2 -1  0  1
#   y=-4       B  B  B  .  F  i  g  g  g    F iron port, g gear machine (0,-4)
#   y=-3       B  B  B  <  R  <  g  g  g    R gear relay chest; < new inserters (dir 4)
#   y=-2       B  B  B  .  .  P  .  i  .    B new science machine (-6,-3)
#   y=-1       P  v  ^  .  .  .  A  A  A    v B -> O (dir 0); ^ C -> B (dir 8)
#   y= 0       .  O  C  <  K  i  A  A  A    K copper port; C copper relay chest
#   y= 1       .  .  .  .  .  .  A  A  A    O second output chest; A science machine
#
# Arrows show where the new inserters move items; i marks an original
# inserter, P a pole (new (-7,-1), original (-2,-2)).
# Inserters only feed what the machine's recipe takes, so the relay chests hold
# only gears (R) and copper (C). The extension needs no new input port: funding
# still fills the iron and copper ports; it adds one output port (O).
SCIENCE_EXTENSION_LAYOUT = "west-science-v1"
SCIENCE_EXTENSION_OFFSETS = (
    ("assembling-machine-1", -6, -3, 0, "automation-science-pack"),
    ("wooden-chest", -3, -3, 0, None),
    ("wooden-chest", -5, 0, 0, None),
    ("wooden-chest", -6, 0, 0, None),
    ("inserter", -2, -3, 4, None),
    ("inserter", -4, -3, 4, None),
    ("inserter", -4, 0, 4, None),
    ("inserter", -5, -1, 8, None),
    ("inserter", -6, -1, 0, None),
    ("small-electric-pole", -7, -1, 0, None),
)
SCIENCE_EXTENSION_OUTPUT = (-6, 0)
# Keys of a saved cell plan that are geometry, not metadata.
_PLAN_BODY_KEYS = ("ok", "reason", "entities", "ports", "required_items")


def plan_hash(plan):
    """The same canonical SHA-256 the bound batch states record as ``plan_hash``."""
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _cell_layout(plan):
    """1 for the original layout (also when extended), 2 for the hub layout, None if unknown."""
    if plan.get("science_extension") == SCIENCE_EXTENSION_LAYOUT:
        return 1
    if plan.get("science_extension") is not None:
        return None
    return science_machine_count(plan)


def science_extension_entities(plan):
    """Entities the west extension adds to ``plan`` (its origin), in build order."""
    origin = cell_origin(plan)
    if origin is None:
        return []
    x, y = origin["x"], origin["y"]
    return [
        entity(name, x + dx, y + dy, direction, recipe) for name, dx, dy, direction, recipe in SCIENCE_EXTENSION_OFFSETS
    ]


def _entity_key(e):
    return (e["name"], e["position"]["x"], e["position"]["y"], e.get("direction", 0), e.get("recipe"))


def science_extension_error(plan):
    """Why ``plan`` cannot grow to two science machines, or None when it can.

    Rules: only an original one-machine cell grows (not the hub layout and not
    an already extended cell); its eleven original entities must be at their
    original offsets; the extension must not overlap any planned entity
    (including the grid pole chain and the lab slots with their poles) except
    an identical pole, and must lie inside the reserved area. Placement in the
    world is still checked natively by the builder before anything is built.
    """
    if not isinstance(plan, dict) or plan.get("template") != "startup_science_cell":
        return "science_cell_extension_unknown_plan"
    if plan.get("science_extension") is not None or science_machine_count(plan) != 1:
        return "science_cell_extension_not_a_one_machine_cell"
    origin = cell_origin(plan)
    if origin is None:
        return "science_cell_extension_origin_missing"
    original = startup_science_cell(origin["x"], origin["y"])
    present = {_entity_key(e) for e in plan.get("entities", [])}
    if any(_entity_key(e) not in present for e in original["entities"]):
        return "science_cell_extension_original_layout_differs"
    if plan.get("ports") != original["ports"]:
        return "science_cell_extension_original_ports_differ"
    geometry = FactoryBuilder.__new__(FactoryBuilder)
    extension = science_extension_entities(plan)
    slots = lab_slot_entities(plan)
    others = list(plan.get("entities", []))
    others += list(slots[0]["poles"]) if slots else []
    others += [slot["lab"] for slot in slots]
    planned = {_entity_key(e) for e in others}
    taken = set()
    for other in others:
        taken |= geometry._occupied_by_plan([other])
    area = plan.get("reserved_area")
    for new in extension:
        tiles = geometry._occupied_by_plan([new])
        if new["name"] == "small-electric-pole" and _entity_key(new) in planned:
            continue
        if tiles & taken:
            return "science_cell_extension_layout_collides"
        if area and not all(_inside(area, tile) for tile in tiles):
            return "science_cell_extension_outside_reserved_area"
    return None


def extended_science_cell(plan):
    """The two-machine plan grown from a one-machine ``plan`` (a strict superset), or None.

    Every original entity and port is kept unchanged and in order; the
    extension entities and one output port are appended. Metadata (template,
    reserved area, layout policy) is copied, and ``science_machines``,
    ``cell_origin`` and ``science_extension`` mark the new layout.
    """
    if science_extension_error(plan):
        return None
    origin = deepcopy(cell_origin(plan))
    extra = {k: deepcopy(v) for k, v in plan.items() if k not in _PLAN_BODY_KEYS}
    extra.update(
        science_machines=MAX_STARTUP_SCIENCE_MACHINES,
        cell_origin=origin,
        science_extension=SCIENCE_EXTENSION_LAYOUT,
    )
    dx, dy = SCIENCE_EXTENSION_OUTPUT
    port = {
        "item": "automation-science-pack",
        "kind": "item",
        "direction": "output",
        "position": {"x": origin["x"] + dx, "y": origin["y"] + dy},
    }
    entities = deepcopy(plan["entities"]) + science_extension_entities(plan)
    return _plan(entities, deepcopy(plan["ports"]) + [port], **extra)


def is_cell_growth(old, new):
    """True when ``new`` keeps every entity and port of ``old`` unchanged (same origin, same area).

    Growth never removes or moves an entity: the old entity list is a prefix
    of the new one, the old ports a prefix of the new ports, and all
    metadata except the layout markers is identical.
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if cell_origin(old) != cell_origin(new):
        return False
    count = len(old.get("entities", []))
    if new.get("entities", [])[:count] != old.get("entities", []) or len(new.get("entities", [])) <= count:
        return False
    if new.get("ports", [])[: len(old.get("ports", []))] != old.get("ports", []):
        return False
    markers = ("science_machines", "cell_origin", "science_extension")
    keys = set(old) | set(new)
    return all(old.get(k) == new.get(k) for k in keys if k not in _PLAN_BODY_KEYS and k not in markers)


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
    count = _cell_layout(plan)
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


def built_keys(obs):
    """``(name, x, y)`` of every observed entity with a native unit number (built hardware)."""
    return {
        (e["name"], e["position"]["x"], e["position"]["y"])
        for e in obs.get("entities", [])
        if type(e.get("unit_number")) is int and e["unit_number"] > 0
    }


def _inside_area(area, position):
    """True when ``position`` lies inside the ``[[x0, y0], [x1, y1]]`` area (False without an area)."""
    if not isinstance(area, list) or len(area) != 2:
        return False
    (x0, y0), (x1, y1) = area
    return x0 <= position["x"] <= x1 and y0 <= position["y"] <= y1


def external_hardware(blocks, *, exclude=()):
    """Planned entities of registered blocks that stand outside their own block's reserved area.

    Power bridges and routed belts leave a block's reserved area, so a site
    check that only compares reserved areas does not see them. A block
    without a reserved area contributes every entity. Blocks named in
    ``exclude`` are skipped. Each row is ``(block_name, entity)``.
    """
    rows = []
    for name, block in blocks.items():
        if name in exclude or not isinstance(block, dict):
            continue
        area = block.get("reserved_area")
        for e in block.get("entities", []):
            if isinstance(e, dict) and isinstance(e.get("position"), dict) and not _inside_area(area, e["position"]):
                rows.append((name, e))
    return rows


def planned_unbuilt_hardware(builder, obs, blocks, *, exclude=()):
    """Tiles of planned-but-unbuilt entities of every registered block, ``{tile: owner row}``.

    Jobs run concurrently: one block may be planned while another chooses its
    site. A site chosen without this map can claim a tile that the first
    block still needs (on a live map the startup science cell put an inserter
    on a mining bank's planned bridge pole and the bank blocked for good).
    Unlike :func:`transport.unbuilt_external_keepouts` (belt routing only)
    this covers mining blocks too and every planned entity, inside or outside
    its block's reserved area. An entity counts as built when an observed
    entity of the same name stands on its exact position with a unit number.
    Blocks named in ``exclude`` (the caller's own) are skipped. Each value is
    ``{"block", "name", "position"}`` of the planned entity on that tile.
    """
    built = built_keys(obs)
    tiles = {}
    for name, block in blocks.items():
        if name in exclude or not isinstance(block, dict):
            continue
        for e in block.get("entities", []):
            if not isinstance(e, dict) or not isinstance(e.get("position"), dict):
                continue
            if (e["name"], e["position"]["x"], e["position"]["y"]) in built:
                continue
            for tile in builder._occupied_by_plan([e]):
                tiles.setdefault(tile, {"block": name, "name": e["name"], "position": dict(e["position"])})
    return tiles


def hardware_conflicts(builder, entities, planned):
    """Entities of a candidate whose footprint hits a tile in ``planned`` (see :func:`planned_unbuilt_hardware`).

    Two plans may share one small electric pole: the same pole on the same
    position is not a conflict. Returns ``[{"entity", "tile", "block"}]``.
    """
    conflicts = []
    for e in entities:
        for tile in sorted(builder._occupied_by_plan([e])):
            owner = planned.get(tile)
            if owner is None:
                continue
            if (
                e["name"] == "small-electric-pole"
                and owner["name"] == "small-electric-pole"
                and owner["position"] == e["position"]
            ):
                continue
            conflicts.append({"entity": e["name"], "tile": tile, "block": owner["block"]})
    return conflicts


class Board:
    def __init__(self, game, builder, catalog):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.path = Path(game.cfg.runtime_dir) / "pcb-board.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self._stamp = self._file_stamp()

    def _file_stamp(self):
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def refresh(self, *, force=False):
        """Re-read the board when another writer changed the file since it was read.

        Long-lived controllers each hold a Board; without this a cell grown by
        one controller would stay invisible to another. An unchanged file keeps
        the in-memory state.
        """
        stamp = self._file_stamp()
        if not force and stamp == self._stamp:
            return
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if stamp is not None else {}
        self._stamp = stamp

    def startup_lineage(self):
        """Hashes of the earlier startup science plans the current cell grew from, oldest first."""
        rows = self.state.get("lineage", {}).get("startup_science", [])
        return [row for row in rows if isinstance(row, str)] if isinstance(rows, list) else []

    def extend_startup_science(self, obs, old, new):
        """Replace the saved cell ``old`` with its growth ``new`` and record ``old``'s hash in the lineage.

        Refused (nothing written) when the world or catalog changed, the saved
        block is no longer exactly ``old``, or ``new`` is not a strict
        superset of ``old`` (see ``is_cell_growth``). The block and lineage are
        written together in one atomic file replace.
        """
        self.refresh(force=True)
        if self.state.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "board_world_changed"}
        if self.state.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"ok": False, "reason": "board_catalog_changed_requires_revalidation"}
        if self.state.get("blocks", {}).get("startup_science") != old:
            return {"ok": False, "reason": "board_science_cell_changed"}
        if not is_cell_growth(old, new):
            return {"ok": False, "reason": "board_science_cell_growth_not_a_superset"}
        state = deepcopy(self.state)
        lineage = state.setdefault("lineage", {}).setdefault("startup_science", [])
        lineage.append(plan_hash(old))
        state["blocks"]["startup_science"] = deepcopy(new)
        _atomic_json(self.path, state)
        self.state = state
        self._stamp = self._file_stamp()
        return {"ok": True, "plan": deepcopy(new), "lineage": list(lineage)}

    def reserve_startup_science(self, obs, *, science_machines=1):
        """Reserve the startup science cell once per world; later calls return the saved plan.

        ``science_machines`` (1 or 2) sizes a new cell only. Lab slots are
        surveyed with the cell; a site whose slots are all placeable wins,
        else the first legal site is used and blocked slots stay unbuilt.
        A candidate whose entities (cell or power chain) hit planned-but-unbuilt
        hardware of another registered block is refused before the survey
        (see :func:`planned_unbuilt_hardware`).
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
        planned = planned_unbuilt_hardware(self.builder, obs, self.state["blocks"])
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
                # Other blocks may be planned but not built yet (concurrent jobs).
                if hardware_conflicts(self.builder, plan["entities"], planned):
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
        self._stamp = self._file_stamp()
        return deepcopy(plan)
