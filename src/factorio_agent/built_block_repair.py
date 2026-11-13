"""A built block that does not work: diagnosis and the needs record of its repair (the ``clear`` rung).

Owner rule (2026-11-13, AGENTS.md "A built block is not frozen"): part of a built block may be
dismantled by paid guarded removal (``mine``) and rebuilt from a republished plan; a critical
consumer is never cut off without a replacement.

Trigger: every planned entity of the block stands, but its proof keeps failing for
``REPAIR_AFTER_TICKS``. The block's owner then diagnoses the built hardware (pure functions
below, fed by a world-bound read) and, when a fault is found, blocks its job with a ``built``
needs record (``plan_needs.KINDS``). The remedy ladder's ``clear`` rung
(``remedy.CLEAR_REBUILD``, option ``repair_built_block``) re-runs the job with the repair on:
the owner plans the replacement part first (republished in its state, which the board reads),
mines the faulty entities (guarded by unit number and world) and builds the new part through
its normal ``ensure_plan``. At most ``REPAIRS_MAX`` repairs per block and world.

Faults:

- fluids (``water_network_faults``): a water pipe of the plan on a steam box's port tile
  (``steam_port_tiles``: the pipe joins water into the steam network, aud-1114-0703 seed
  477133967: power at water 0 / steam 0 for 37 min); a plan pipe holding steam (mixed
  network); water in the pipes but none in the boiler (the network does not reach it).
- belts (``belt_tile_faults``): a plan belt tile occupied by a different entity of ours
  (aud-1114-0526-2446866888: a power pole at (23.5,10.5) on the iron lane head). Diagnosis
  only so far: the pole carries the bank's power, so its repair must first build a
  replacement link (never cut a consumer off) before the guarded mine; not wired yet.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from . import plan_needs

KIND = "built"
OPTION = "repair_built_block"
REPAIR_AFTER_TICKS = 3600  # one game minute of a failing proof with every planned entity standing
REPAIRS_MAX = 2
BELT_NAMES = frozenset({"transport-belt", "underground-belt", "splitter"})


def _key(position: Mapping) -> tuple[float, float]:
    return (round(float(position["x"]), 3), round(float(position["y"]), 3))


def _fluids(row: Any) -> dict:
    """``{fluid: amount}`` of one pipe row of the live read (an empty Lua table may come back as a list)."""
    if isinstance(row, Mapping):
        fluids = row.get("fluids")
        if isinstance(fluids, Mapping):
            return {str(k): v for k, v in fluids.items() if isinstance(v, (int, float)) and v > 0}
    return {}


def water_network_faults(
    entities: Iterable[Mapping], pipe_rows: list | None = None, boiler_water: float | None = None
) -> list[dict]:
    """Faults of a planned water network (pump -> pipes -> boiler) as ``[{fault, position}]``.

    ``entities``: the block's plan; ``pipe_rows``: the live read of its pipes in plan order
    (``{"standing", "fluids"}``), None when not read; ``boiler_water``: the boiler's water.
    """
    from .core.deterministic_builder import steam_port_tiles

    entities = list(entities)
    pipes = [e for e in entities if e.get("name") == "pipe"]
    ports = {_key({"x": x, "y": y}) for x, y in steam_port_tiles([e for e in entities if e.get("name") != "pipe"])}
    faults = [
        {"fault": "water_pipe_on_steam_port", "position": dict(p["position"])}
        for p in pipes
        if _key(p["position"]) in ports
    ]
    rows = list(pipe_rows or ())
    if rows:
        for pipe, row in zip(pipes, rows):
            if "steam" in _fluids(row):
                faults.append({"fault": "steam_in_water_pipe", "position": dict(pipe["position"])})
        wet = any("water" in _fluids(row) for row in rows)
        if not faults and wet and isinstance(boiler_water, (int, float)) and boiler_water <= 0:
            faults.append({"fault": "water_does_not_reach_boiler", "position": dict(pipes[-1]["position"])})
    return faults


def route_pipes(entities: Iterable[Mapping], source: Mapping, port: Mapping) -> list[dict]:
    """The routed pipes of a plan: those 4-connected to ``source`` without passing the block's ``port``.

    The steam bank's own pipes (between its water port and the boiler) are part of the block's
    template, not of the route, and are kept by a repair.
    """
    pipes = {_key(e["position"]): e for e in entities if e.get("name") == "pipe"}
    start, stop = _key(source), _key(port)
    if start not in pipes:
        return []
    seen, todo = {start}, [start]
    while todo:
        x, y = todo.pop()
        if (x, y) == stop:
            continue
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = (round(x + dx, 3), round(y + dy, 3))
            if nxt in pipes and nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return [e for k, e in pipes.items() if k in seen]


def belt_tile_faults(entities: Iterable[Mapping], live: Iterable[Mapping]) -> list[dict]:
    """Plan belt tiles where a different entity of ours stands: ``[{fault, position, planned, found, unit_number}]``.

    A 1x1 entity (pole, pipe, chest, inserter) whose position is a planned belt tile. Larger
    entities and natural ones are not counted (they cannot stand on a built plan belt).
    """
    planned = {}
    for e in entities:
        if e.get("name") in BELT_NAMES:
            planned[_key(e["position"])] = e
    faults = []
    for e in live:
        name = e.get("name")
        position = e.get("position")
        if not isinstance(position, Mapping) or name in BELT_NAMES or not name:
            continue
        hit = planned.get(_key(position))
        if hit is None:
            continue
        faults.append(
            {
                "fault": "plan_belt_tile_taken",
                "position": dict(position),
                "planned": hit.get("name"),
                "found": name,
                "unit_number": e.get("unit_number"),
            }
        )
    return faults


def needs(block: str, faults: list[dict], **fields) -> dict:
    """The ``built`` needs record of a block whose built hardware is faulty."""
    first = faults[0] if faults else {}
    where = first.get("position") or {}
    point = [where.get("x"), where.get("y")] if where else None
    return plan_needs.make(
        KIND,
        f"built_block_fault:{block}:{first.get('fault', 'unknown')}",
        block=block,
        at=point,
        faults=[f"{f.get('fault')}@{_key(f['position'])}" for f in faults if f.get("position")],
        blockers=[
            {
                "entity": f.get("found") or "pipe",
                "position": [f["position"]["x"], f["position"]["y"]],
                "reason": f["fault"],
            }
            for f in faults
            if f.get("position")
        ],
        change=f"the faulty part of the built {block} mined and rebuilt from a republished plan",
        **fields,
    )
