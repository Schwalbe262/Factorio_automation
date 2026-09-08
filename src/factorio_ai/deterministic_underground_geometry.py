"""Explicit underground pairs for owned, directed item-route geometry.

These records describe a plan, not proof of an engine connection. A caller
must obtain the distance limit from its world catalog and separately verify
the live pair before treating transport capacity or material flow as proven.
"""
from __future__ import annotations

import math

from .factory_templates import DIRECTIONS


def _mouth(entity):
    if not isinstance(entity, dict) or entity.get("name") != "underground-belt":
        raise ValueError("underground pair requires ordinary underground belt endpoints")
    direction = entity.get("direction")
    role = entity.get("belt_to_ground_type")
    position = entity.get("position")
    if (type(direction) is not int or direction not in DIRECTIONS or role not in {"input", "output"}
            or not isinstance(position, dict)
            or any(type(position.get(axis)) not in (int, float) or not math.isfinite(position[axis])
                   for axis in ("x", "y"))):
        raise ValueError("invalid underground endpoint geometry")
    return position["x"], position["y"], direction, role


def underground_edges(plan: dict) -> dict:
    """Resolve only explicit, unambiguous, reciprocal pairs inside this plan."""
    mouths = {}
    for entity in plan.get("entities", []):
        if entity.get("name") != "underground-belt":
            continue
        mouth = _mouth(entity)
        point = mouth[:2]
        if point in mouths and mouths[point] != mouth:
            raise ValueError("contradictory underground endpoints")
        mouths[point] = mouth
    pairs = plan.get("underground_pairs", [])
    if not isinstance(pairs, list):
        raise ValueError("underground pairs must be explicit records")
    claimed, edges = set(), {}
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("invalid underground pair record")
        inlet, outlet = _mouth(pair.get("input")), _mouth(pair.get("output"))
        limit = pair.get("max_distance")
        if (inlet[3] != "input" or outlet[3] != "output" or inlet[2] != outlet[2]
                or mouths.get(inlet[:2]) != inlet or mouths.get(outlet[:2]) != outlet
                or inlet[:2] in claimed or outlet[:2] in claimed
                or type(limit) is not int or limit < 1):
            raise ValueError("underground pair does not match its reserved endpoints")
        dx, dy = DIRECTIONS[inlet[2]]
        vx, vy = outlet[0] - inlet[0], outlet[1] - inlet[1]
        distance = vx * dx + vy * dy
        if vx * dy != vy * dx or distance != math.floor(distance):
            raise ValueError("underground pair must follow a cardinal tile line")
        if not 0 < distance <= limit:
            raise ValueError("underground pair exceeds its catalog distance")
        for mouth in mouths.values():
            if mouth[:2] in {inlet[:2], outlet[:2]} or mouth[2] != inlet[2]:
                continue
            mx, my = mouth[0] - inlet[0], mouth[1] - inlet[1]
            if mx * dy == my * dx and 0 < mx * dx + my * dy < distance:
                raise ValueError("another underground endpoint interrupts the reserved pair")
        claimed.update((inlet[:2], outlet[:2]))
        edges[inlet[:2]] = outlet[:2]
    if claimed != set(mouths):
        raise ValueError("underground route has an unpaired endpoint")
    return edges
