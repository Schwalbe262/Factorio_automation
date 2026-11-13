"""Obsolete blocks: one rule for temporary equipment that a verified permanent successor replaced.

The early base builds hand-served stand-ins (burner starter cells, the
hand-fed supply cell, the startup science cell and its labs). Once a
permanent block does the same work they are only busywork and hold items
the permanent rows would otherwise craft again. Every retirement follows the
same rule (PLANNERS.md "Obsolete blocks"):

1. trigger: a permanent successor is verified (succeeded jobs plus live or
   board evidence, the owning module decides what counts);
2. guards: the block feeds nothing another job still waits for, no active
   job depends on it, and the character has inventory room for what mining
   returns;
3. dismantle: one paid, guarded ``mine`` per entity (unit number + world id),
   contents return to the inventory;
4. release the board block and record ``phase: retired`` (never rebuilt);
5. proposed once, at low priority (``RETIRE_PRIORITY``), by a planner rule.

This module holds the shared parts; ``supply_cell``, ``starter_retirement``
and ``science_retirement`` keep their own triggers and evidence. ``REGISTRY``
lists every obsolete block, including roadmap equipment that will plug in
later (``implemented=False``: documented, not acted on).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .core.deterministic_state import _atomic_json

RETIRE_PRIORITY = -1000  # below every roadmap step: background demolition
ACTIVE = ("running", "waiting", "pending")


@dataclass(frozen=True)
class Obsolete:
    """One temporary block and the permanent successor that makes it obsolete."""

    block: str
    capability: str
    successor: str
    module: str
    implemented: bool = True


REGISTRY: tuple[Obsolete, ...] = (
    Obsolete(
        "supply_cell",
        "supply_cell",
        "every product has a succeeded produce row or no demand above HANDCRAFT_LIMIT",
        "supply_cell",
    ),
    Obsolete(
        "starter cells of a resource",
        "starter_retirement",
        "PERMANENT_SOURCES[resource] succeeded and the board shows the source",
        "starter_retirement",
    ),
    Obsolete(
        "startup_science (cell, slot labs, starter lab)",
        "startup_science_retirement",
        "automation_science succeeded, its labs research from machine-made packs, every bootstrap research done",
        "science_retirement",
    ),
    # Phase switch, not a dismantle: the hand-fed boiler stays and is fed by belt.
    Obsolete("hand-fed boiler fuelling", "power_coal_feed", "dedicated coal belt (power_mode belt)", "power_coal_feed"),
    Obsolete(
        "starter boiler coal feed (drill, fuel arm, belts upstream of the join)",
        "power_coal_tap",
        "output-priority splitter tap on the coal patch trunk, proven 30 game seconds",
        "power_coal_tap",
    ),
    # Later (documented only):
    Obsolete("private plate lines of early rows", "-", "planned bus taps (planned_tap)", "-", implemented=False),
    Obsolete(
        "stone furnaces of the first smelting bank", "-", "steel furnaces (furnace_upgrade)", "-", implemented=False
    ),
    Obsolete("burner inserters / burner drills on outposts", "-", "electric mining banks", "-", implemented=False),
)


# ----- trigger and guards -----------------------------------------------------
def succeeded(jobs) -> set[str]:
    """Capabilities with at least one succeeded job."""
    return {job.get("capability") for job in jobs if job.get("status") == "succeeded"}


def active_jobs(jobs, *, capabilities=None, exclude=(), reason_markers=(), statuses=ACTIVE) -> list:
    """Keys of active jobs that still depend on a block.

    ``capabilities``: only jobs of these capabilities count (None: any);
    ``exclude``: never these capabilities (the retiring job itself);
    ``reason_markers``: only jobs whose reason names one of them (e.g. "machine-made belt").
    """
    rows = []
    for job in jobs:
        capability = job.get("capability")
        if capability in exclude or (capabilities is not None and capability not in capabilities):
            continue
        if job.get("status") not in statuses:
            continue
        if reason_markers and not any(m in str(job.get("reason", "")) for m in reason_markers):
            continue
        rows.append(job.get("key"))
    return rows


def proposed(jobs, queue_nodes, capability, match=None) -> bool:
    """A job or queue node of ``capability`` (whose parameters satisfy ``match``) exists: propose once."""
    for row in list(jobs) + list(queue_nodes):
        if row.get("capability") != capability:
            continue
        if match is None or match(row.get("parameters") or {}):
            return True
    return False


def slots_needed(catalog, entities) -> int:
    """Main inventory slots that mining ``entities`` (and their contents) may fill, counting fresh stacks."""
    items = getattr(catalog, "items", None) or {}
    counts: dict[str, int] = {}
    for e in entities:
        counts[e["name"]] = counts.get(e["name"], 0) + 1
        inside = e.get("inventory") or e.get("output_inventory") or {}
        for name, n in inside.items():
            if isinstance(n, (int, float)) and n > 0:
                counts[name] = counts.get(name, 0) + int(n)
    total = 0
    for name, n in counts.items():
        stack = (items.get(name) or {}).get("stack_size")
        stack = stack if type(stack) is int and stack > 0 else 50
        total += math.ceil(n / stack)
    return total


def room_action(obs, needed, storage=None):
    """None when the inventory has ``needed`` empty slots, else storage's paid deposit/craft or a wait."""
    empty = obs.get("inventory_empty_slots")
    if not isinstance(empty, int) or empty >= needed:
        return None
    if storage is not None:
        result = storage.step(obs, minimum_slots=max(1, needed))
        if result is not None:
            return result
        return None  # storage's fresh snapshot shows the room
    return {
        "status": "waiting",
        "reason": "obsolete_block_needs_inventory_room",
        "evidence": {"empty_slots": empty, "needed": needed},
    }


# ----- dismantle, release, record -------------------------------------------
def guarded_mine(entity, world_id, reason):
    """A paid ``mine`` of exactly this entity (unit number + world), or None when it has no unit number."""
    if not entity or type(entity.get("unit_number")) is not int:
        return None
    return {
        "type": "mine",
        "name": entity["name"],
        "position": dict(entity["position"]),
        "count": 1,
        "expected_entity_unit": entity["unit_number"],
        "expected_entity_world_id": world_id,
        "reason": reason,
    }


def release_block(board_path, world_id, block) -> bool:
    """Remove ``block`` from the board of this world (one atomic write); True when it was removed."""
    path = Path(board_path)
    if not path.exists():
        return False
    board = json.loads(path.read_text(encoding="utf-8"))
    if board.get("world_id") != world_id or block not in board.get("blocks", {}):
        return False
    del board["blocks"][block]
    _atomic_json(path, board)
    return True


def block_released(board_path, block) -> bool:
    path = Path(board_path)
    if not path.exists():
        return True
    return block not in (json.loads(path.read_text(encoding="utf-8")).get("blocks") or {})


def mark_retired(record, tick) -> bool:
    """Set ``phase: retired`` and ``retired_tick`` once; True when the record changed."""
    if record.get("phase") == "retired":
        return False
    record["phase"] = "retired"
    record["retired_tick"] = tick
    return True


def retire_node(node_id, capability, parameters, reason, priority=RETIRE_PRIORITY):
    """The one-shot planner node of a retirement."""
    return {
        "id": node_id,
        "capability": capability,
        "parameters": dict(parameters),
        "priority": priority,
        "depends_on": [],
        "reason": reason,
    }
