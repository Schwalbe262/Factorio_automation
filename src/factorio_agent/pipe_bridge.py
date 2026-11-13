"""Pipe bridge: a pipe-to-ground pair under one tile of a straight pipe run, so a belt crosses on the surface.

eval-1575138435: the crude pipeline ran along the east edge of the planned bus on the
column of every east tap port, so no new east-side tap or feed had a free port tile. A
player puts the pipe underground there. Rule (owner, 2026-11-13: a built block may be
partly rebuilt when a neighbour needs it, paid guarded removal, plan republished first):

* :func:`plan` (pure): the crossing tile and its two neighbours along the run are plain
  pipes, the run continues beyond both (pipe or pipe-to-ground), and none of the three has
  a fluid neighbour across the run. The bridge removes the three pipes and builds a
  pipe-to-ground on each neighbour tile, its surface side facing away from the crossing.
* :func:`release`: the owner's saved plan and board copy are rewritten first (the three
  pipes out, the pair in, the fluid graph edges re-joined, the old bindings dropped). Only
  owners whose state shape is known are rewritten (``OWNER_STATES``); any other owner is a
  blocker, never a silent change.
* :func:`step`: one paid action at a time: stock the pair, mine each pipe by unit, build the
  pair; :func:`bind` then records the new units in the owner's bindings.
"""

from __future__ import annotations

import json
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json

from .transport import VECTORS, xy

PIPE = "pipe"
UNDERGROUND = "pipe-to-ground"
FLUID = frozenset({"pipe", "pipe-to-ground", "pump", "storage-tank", "offshore-pump", "oil-refinery"})
# Owners whose saved state the bridge may rewrite (shape: plan/route entities, fluid graph, bindings).
OWNER_STATES = {"basic_petroleum": "basic-petroleum.json"}
LAYOUT_POLICY = "pipe-bridge-v1"


def _facing(vector) -> int:
    return next(d for d, v in VECTORS.items() if tuple(v) == tuple(vector))


def _key(e) -> str:
    return e["name"] + ":" + str(e["position"]["x"]) + ":" + str(e["position"]["y"])


def plan(obs, tile, axis) -> dict:
    """The bridge of the pipe at ``tile`` (``(x, y)`` centre) on a run along ``axis`` (unit vector)."""
    live = {}
    for e in obs.get("entities", []):
        live[xy(e)] = e
    ax, ay = axis
    x, y = tile

    def at(k, lateral=0):
        return live.get((x + k * ax + lateral * -ay, y + k * ay + lateral * ax))

    removed = []
    for k in (-1, 0, 1):
        e = at(k)
        if e is None or e.get("name") != PIPE or type(e.get("unit_number")) is not int:
            return {"ok": False, "reason": "pipe_bridge_needs_three_plain_pipes", "at": [x + k * ax, y + k * ay]}
        for lateral in (-1, 1):
            side = at(k, lateral)
            if side is not None and side.get("name") in FLUID:
                return {"ok": False, "reason": "pipe_bridge_meets_a_side_connection", "at": xy(side)}
        removed.append({"name": PIPE, "position": dict(e["position"]), "unit_number": e["unit_number"]})
    for k in (-2, 2):
        e = at(k)
        if e is None or e.get("name") not in (PIPE, UNDERGROUND):
            return {"ok": False, "reason": "pipe_bridge_run_does_not_continue", "at": [x + k * ax, y + k * ay]}
    first = {
        "name": UNDERGROUND,
        "position": dict(removed[0]["position"]),
        "direction": _facing((-ax, -ay)),
    }
    last = {"name": UNDERGROUND, "position": dict(removed[2]["position"]), "direction": _facing((ax, ay))}
    ends = [at(-2), at(2)]
    return {
        "ok": True,
        "tile": {"x": x, "y": y},
        "axis": [ax, ay],
        "remove": removed,
        "add": [first, last],
        "ends": [
            {"name": e["name"], "position": dict(e["position"]), "direction": e.get("direction", 0)} for e in ends
        ],
    }


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _swap(entities, bridge) -> tuple[list, bool]:
    gone = {xy(e) for e in bridge["remove"]}
    kept = [e for e in entities if not (e.get("name") == PIPE and xy(e) in gone)]
    if len(kept) == len(entities):
        return entities, False
    return kept + [dict(e) for e in bridge["add"]], True


def owners(board: dict, bridge) -> list[str]:
    """Board blocks holding a pipe the bridge removes."""
    gone = {xy(e) for e in bridge["remove"]}
    return sorted(
        name
        for name, block in (board.get("blocks") or {}).items()
        if isinstance(block, dict) and any(e.get("name") == PIPE and xy(e) in gone for e in block.get("entities") or [])
    )


def _edge(a, b) -> dict:
    keep = ("direction", "name", "position")
    return {
        "a": dict({k: a[k] for k in keep if k in a}, fluidbox_index=1),
        "b": dict({k: b[k] for k in keep if k in b}, fluidbox_index=1),
    }


def _rewrite_state(state: dict, bridge) -> bool:
    changed = False
    for key in ("plan", "route"):
        part = state.get(key)
        if isinstance(part, dict) and isinstance(part.get("entities"), list):
            part["entities"], dirty = _swap(part["entities"], bridge)
            changed |= dirty
    if isinstance(state.get("fluid_nodes"), list):
        state["fluid_nodes"], dirty = _swap(state["fluid_nodes"], bridge)
        changed |= dirty
    if isinstance(state.get("fluid_edges"), list) and changed:
        gone = {xy(e) for e in bridge["remove"]}
        edges = [
            row
            for row in state["fluid_edges"]
            if not (xy(row["a"]) in gone and row["a"].get("name") == PIPE)
            and not (xy(row["b"]) in gone and row["b"].get("name") == PIPE)
        ]
        first, last = bridge["add"]
        before, after = bridge["ends"]
        edges += [_edge(before, first), _edge(first, last), _edge(last, after)]
        state["fluid_edges"] = edges
    if isinstance(state.get("bindings"), dict):
        for e in bridge["remove"]:
            if state["bindings"].pop(_key(e), None) is not None:
                changed = True
    return changed


def release(root, world_id: str, bridge) -> dict | None:
    """Republish every owner without the three pipes and with the pair (idempotent); a blocked status or None."""
    root = Path(root)
    board_path = root / "pcb-board.json"
    board = _read(board_path)
    if board.get("world_id") != world_id:
        return {"status": "blocked", "reason": "pipe_bridge_board_world_changed"}
    names = owners(board, bridge)
    unknown = [n for n in names if n not in OWNER_STATES]
    if unknown:
        return {"status": "blocked", "reason": "pipe_bridge_owner_state_unknown", "owners": unknown}
    for name in names:
        path = root / OWNER_STATES[name]
        state = _read(path)
        if state.get("world_id") != world_id:
            return {"status": "blocked", "reason": "pipe_bridge_owner_world_changed", "owner": name}
        if _rewrite_state(state, bridge):
            _atomic_json(path, state)
        block = board["blocks"][name]
        block["entities"], _ = _swap(block["entities"], bridge)
        if isinstance(state.get("plan"), dict) and block.get("layout_policy") == state["plan"].get("layout_policy"):
            block["entities"] = list(state["plan"]["entities"])
    if names:
        _atomic_json(board_path, board)
    return None


def standing(obs, bridge) -> bool:
    live = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
    return all(
        (found := live.get((e["name"], *xy(e)))) is not None and found.get("direction", 0) == e["direction"]
        for e in bridge["add"]
    )


def step(builder, obs, bridge) -> dict | None:
    """The next paid action of the bridge (stock, guarded mine, build), or None once the pair stands."""
    if standing(obs, bridge):
        return None
    live = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
    if obs.get("inventory", {}).get(UNDERGROUND, 0) < 2:
        result = builder.bootstrap.ensure_item(obs, UNDERGROUND, 2)
        if result.get("type") or result.get("status") != "succeeded":
            return result
    for e in bridge["remove"]:
        old = live.get((PIPE, *xy(e)))
        if old is None:
            continue
        if old.get("unit_number") != e["unit_number"]:
            return {"status": "blocked", "reason": "pipe_bridge_pipe_identity_changed", "position": e["position"]}
        return {
            "type": "mine",
            "name": PIPE,
            "position": old["position"],
            "count": 1,
            "expected_entity_unit": e["unit_number"],
            "expected_entity_world_id": obs["world_id"],
            "reason": "normally recover one owned pipe for a fully stocked pipe-to-ground bridge",
        }
    result = builder.ensure_plan(
        obs,
        _plan(
            [dict(e) for e in bridge["add"]],
            [],
            clear_obstacles=[],
            clearance_world_id=obs["world_id"],
            layout_policy=LAYOUT_POLICY,
        ),
    )
    if result.get("status") != "succeeded":
        return result
    return None


def bind(root, world_id: str, bridge, obs) -> None:
    """Record the standing pair's units in every known owner's bindings (idempotent)."""
    root = Path(root)
    board = _read(root / "pcb-board.json")
    live = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
    names = [n for n, block in (board.get("blocks") or {}).items() if n in OWNER_STATES and isinstance(block, dict)]
    for name in names:
        path = root / OWNER_STATES[name]
        state = _read(path)
        bindings = state.get("bindings")
        if state.get("world_id") != world_id or not isinstance(bindings, dict):
            continue
        plan_keys = {_key(e) for e in (state.get("plan") or {}).get("entities", [])}
        changed = False
        for e in bridge["add"]:
            found = live.get((e["name"], *xy(e)))
            key = _key(e)
            if found is not None and key in plan_keys and bindings.get(key) != found.get("unit_number"):
                bindings[key] = found["unit_number"]
                changed = True
        if changed:
            _atomic_json(path, state)
