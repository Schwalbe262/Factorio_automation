"""Pure proposals for an adjacent bypass around a straight long-arm chain.

No proposal is a placement, live connection, capacity, or retirement proof.
The caller supplies its catalog distance and complete other-owner plans, then
must prove the current world and perform a paid, guarded cutover separately.
"""
from __future__ import annotations

from copy import deepcopy
import math

from .deterministic_input_links import _geometry, _identity, _path, _point
from .factory_templates import DIRECTIONS


def _references(value, points):
    if isinstance(value, dict):
        position = value.get("position")
        if isinstance(position, dict) and (position.get("x"), position.get("y")) in points:
            return True
        return any(_references(child, points) for child in value.values())
    return isinstance(value, (list, tuple)) and any(_references(child, points) for child in value)


def _arm_touches(entity, points):
    if entity.get("name") not in {"inserter", "fast-inserter", "long-handed-inserter"}:
        return False
    dx, dy = DIRECTIONS[entity.get("direction", 0)]
    reach = 2 if entity["name"] == "long-handed-inserter" else 1
    x, y = _point(entity)
    return any((x + sign * dx * reach, y + sign * dy * reach) in points for sign in (-1, 1))


def _incoming(edges, point):
    return [(source, arm) for source, outgoing in edges.items() for target, arm in outgoing if target == point]


def _belt_touches(entity, route):
    if entity.get("name") not in {"transport-belt", "underground-belt"}:
        return False
    x, y = _point(entity)
    dx, dy = DIRECTIONS[entity.get("direction", 0)]
    for belt in route:
        bx, by = _point(belt)
        bdx, bdy = DIRECTIONS[belt.get("direction", 0)]
        if (x + dx, y + dy) == (bx, by) or (bx + bdx, by + bdy) == (x, y):
            return True  # Mouth side feeds and opposing lanes are also kept outside this narrow proposal.
    return False


def _pieces(entry, direction, length, offset, limit):
    dx, dy = DIRECTIONS[direction]
    lx, ly = -dy, dx
    side = 1 if offset > 0 else -1
    outward = next(d for d, delta in DIRECTIONS.items() if delta == (lx * side, ly * side))

    def entity(lateral, forward, facing, role=None):
        result = {"name": "underground-belt" if role else "transport-belt", "direction": facing,
                  "position": {"x": entry[0] + lx * lateral + dx * forward,
                               "y": entry[1] + ly * lateral + dy * forward}}
        if role:
            result["belt_to_ground_type"] = role
        return result

    rows = [entity(side * n, 0, direction if n == abs(offset) else outward)
            for n in range(1, abs(offset) + 1)]
    pairs, cursor, finish = [], 1, length - 1
    while cursor < finish:
        end = min(cursor + limit, finish)
        inlet, outlet = entity(offset, cursor, direction, "input"), entity(offset, end, direction, "output")
        rows.extend((inlet, outlet))
        pairs.append({"input": deepcopy(inlet), "output": deepcopy(outlet), "max_distance": limit})
        cursor = end + 1
    if cursor == finish:
        rows.append(entity(offset, cursor, direction))
    rows.extend(entity(side * n, length, (outward + 8) % 16) for n in range(abs(offset), 0, -1))
    return rows, pairs, outward


def propose_collinear_bypasses(plan: dict, ordered_path: list[dict], *,
                               max_distance: int, other_plans: list[dict]) -> list[dict]:
    """Return at most four proposals, ordered +1, -1, +2, -2 lateral tiles.

    ``ordered_path`` is an exact entry-to-exit segment of the canonical plan,
    with a surface belt before the first long-arm pickup and after the last
    drop. Canonical endpoints, taps, and other ownership cannot be moved.
    Retained segment records are not permission to mine or release them.
    """
    try:
        return _proposals(plan, ordered_path, max_distance, other_plans)
    except (KeyError, TypeError, ValueError, OverflowError):
        return []


def _proposals(plan, path, limit, other_plans):
    if (type(limit) is not int or limit < 1 or not isinstance(plan, dict) or not isinstance(path, list)
            or not isinstance(other_plans, list) or not 5 <= len(path) <= 128
            or not all(isinstance(owner, dict) for owner in other_plans)):
        return []
    entities = plan["entities"]
    rows = [entities, path, *(owner.get("entities", []) for owner in other_plans)]
    if any(not isinstance(group, list) or not all(isinstance(row, dict) for row in group) for group in rows):
        return []
    identities = {}
    for row in entities:
        identity = _identity(row)
        if identity in identities and identities[identity] != row:
            return []
        identities[identity] = row
    if any(row not in entities for row in path) or len({_identity(row) for row in path}) != len(path):
        return []
    if any(row["name"] != "transport-belt" for row in (path[0], path[1], path[-2], path[-1])):
        return []
    if path[0].get("_keep_output_clear"):
        return []
    direction = path[0]["direction"]
    dx, dy = DIRECTIONS[direction]
    start, finish = _point(path[0]), _point(path[-1])
    length = (finish[0] - start[0]) * dx + (finish[1] - start[1]) * dy
    if not 1 <= length <= 128 or length != int(length):
        return []
    previous, arms = -1, 0
    for row in path:
        x, y = _point(row)
        if any(type(n) not in (int, float) or not math.isfinite(n) or (n - .5) % 1 for n in (x, y)):
            return []
        forward = (x - start[0]) * dx + (y - start[1]) * dy
        if (x - start[0]) * dy != (y - start[1]) * dx or forward <= previous:
            return []
        if row["name"] == "transport-belt" and row.get("direction") == direction:
            pass
        elif row["name"] == "long-handed-inserter" and row.get("direction") == (direction + 8) % 16:
            arms += 1
        else:
            return []
        previous = forward
    if not arms:
        return []
    belts, edges, _ = _geometry(plan)
    if _path(belts, edges, start, finish) != path:
        return []
    retired_ids = {_identity(row) for row in path[1:-1]}
    protected = {_point(row) for row in path[:-1]}
    path_ids = {_identity(row) for row in path}
    belt_path = [row for row in path if row["name"] == "transport-belt"]
    for index, row in enumerate(belt_path[:-1]):
        point = _point(row)
        if len(edges[point]) != 1 or (index and len(_incoming(edges, point)) != 1):
            return []
    if len(_incoming(edges, start)) > 1:
        return []
    for row in entities:
        if _identity(row) not in path_ids and (_point(row) in protected or _arm_touches(row, protected)):
            return []
    for field in ("ports", "source_port", "consumer_port", "upstream_tap", "upstream_tail", "consumer_entry"):
        if _references(plan.get(field), protected):
            return []
    for owner in other_plans:
        if (_references(owner, protected)
                or any(_arm_touches(row, protected) for row in owner.get("entities", []))
                or any(_belt_touches(row, belt_path[:-1]) for row in owner.get("entities", []) if row not in entities)):
            return []

    proposals = []
    occupied = {_point(row) for row in entities}
    other_occupied = {_point(row) for owner in other_plans for row in owner.get("entities", [])}
    for offset in (1, -1, 2, -2):
        pieces, pairs, outward = _pieces(start, direction, int(length), offset, limit)
        if len(pieces) > 32:
            continue
        new_points = {_point(row) for row in pieces}
        if (new_points & (occupied | other_occupied)
                or any(_arm_touches(row, new_points) for row in entities
                       if _identity(row) not in retired_ids)
                or any(_arm_touches(row, new_points) for owner in other_plans for row in owner.get("entities", []))
                or any(_belt_touches(row, pieces) for owner in other_plans
                       for row in owner.get("entities", []) if row not in entities)):
            continue
        entry = {**deepcopy(path[0]), "direction": outward}
        proposed = deepcopy(plan)
        kept = [deepcopy(entry if _identity(row) == _identity(path[0]) else row)
                for row in entities if _identity(row) not in retired_ids]
        proposed["entities"] = [*kept[:-1], *deepcopy(pieces), kept[-1]]
        proposed["underground_pairs"] = [*deepcopy(plan.get("underground_pairs", [])), *deepcopy(pairs)]
        proposed["flow_verified"] = False
        proposed["transport_route_capacity_verified"] = False
        for field in ("path", "crossings"):
            proposed.pop(field, None)  # Original derived geometry remains in the caller's original plan.
        try:
            new_belts, new_edges, _ = _geometry(proposed)
        except (KeyError, TypeError, ValueError):
            continue
        expected = [entry, *pieces, path[-1]]
        if _path(new_belts, new_edges, start, finish) != expected:
            continue
        if _incoming(new_edges, start) != _incoming(edges, start):
            continue
        if any(len(new_edges[_point(row)]) != 1 or len(_incoming(new_edges, _point(row))) != 1 for row in pieces):
            continue
        proposals.append({"offset": offset, "plan": proposed, "new_entities": deepcopy(pieces),
                          "entry": {"old": deepcopy(path[0]), "new": deepcopy(entry)},
                          "exit": deepcopy(path[-1]), "retained_segment": deepcopy(path[1:-1]),
                          "flow_verified": False, "placement_verified": False})
    return proposals
