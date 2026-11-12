"""Place a canonical block template into a target rectangle in its best orientation.

A template is built by ``build(x0, y0)`` with its footprint at the tile rect
``[x0, x0 + w) x [y0, y0 + h)``. For each of the 8 rotation/mirror variants
(:func:`site.transform.variants`) whose rotated size equals the target
size, the template is built so its footprint centre is the target centre
and transformed about that centre, which lands it exactly on the target.

Scoring (lower is better), from ``wants(plan) -> [(port, point, role[, side])]``
(see :func:`port_score` for the edge-aware form used with ``side``):

* every port adds the Manhattan distance from its position to ``point``
  (where the material comes from for ``role`` "in", where it must go for
  "out");
* a port facing the wrong way adds ``AWAY_PENALTY``: an input whose belt
  runs mainly towards its feeder point (``facing . (point - port) > 0`` and
  at least as large as the perpendicular part) or an output whose belt runs
  mainly away from its target.

Ties keep the variant order of :func:`site.transform.variants` (plain
rotations before mirrored ones), so an unmirrored fit is preferred.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import Rect
from .transform import Transform, apply, variants

AWAY_PENALTY = 1000.0
TURN_PENALTY = 64.0
FACING_VECTOR = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
SIDE_NORMAL = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}


@dataclass(frozen=True)
class Placement:
    """One oriented template: ``plan`` is already transformed onto ``target``."""

    score: float
    transform: Transform
    plan: dict
    target: Rect


def port_score(port: dict, point: tuple[float, float], role: str, side: str = "") -> float:
    """Cost of one port; see the module docstring.

    With an edge ``side`` the port belongs to (the feeder's entry edge or the
    zone's output edge), the facing is judged against that edge's outward
    normal instead: an input belt running out through its entry edge or an
    output belt running in from its output edge costs ``AWAY_PENALTY``, an
    output running parallel to its edge costs ``TURN_PENALTY``, and an
    output's distance is measured to the edge line only.
    """
    p = port["position"]
    dx = point[0] - p["x"]
    dy = point[1] - p["y"]
    vx, vy = FACING_VECTOR.get(port.get("facing"), (0, 0))
    if side:
        nx, ny = SIDE_NORMAL[side]
        outward = vx * nx + vy * ny
        if role == "out":
            score = abs(dx * nx + dy * ny)
            if outward < 0:
                return score + AWAY_PENALTY
            return score + (TURN_PENALTY if outward == 0 else 0.0)
        score = abs(dx) + abs(dy)
        return score + (AWAY_PENALTY if outward > 0 else 0.0)
    score = abs(dx) + abs(dy)
    toward = vx * dx + vy * dy
    across = abs(vy * dx - vx * dy)
    # Only a belt running mainly along the line to the point counts as
    # facing the wrong way; a perpendicular (side-entry) port is fine.
    dominant = abs(toward) >= across
    if dominant and ((role == "in" and toward > 0) or (role == "out" and toward < 0)):
        score += AWAY_PENALTY
    return score


def orientations(build, size: tuple[int, int], target: Rect, wants, *, allowed=None) -> list[Placement]:
    """All fitting variants of ``build`` on ``target``, best first.

    ``allowed(transform) -> bool`` can exclude variants a consumer cannot
    handle yet.
    """
    w, h = size
    cx = target.x0 + target.width / 2
    cy = target.y0 + target.height / 2
    found = []
    for index, base in enumerate(variants()):
        turned = (h, w) if base.rotation in (4, 12) else (w, h)
        if turned != (target.width, target.height):
            continue
        corner = (cx - w / 2, cy - h / 2)
        if not (float(corner[0]).is_integer() and float(corner[1]).is_integer()):
            continue
        try:
            t = Transform(base.rotation, base.mirror, (cx, cy))
        except ValueError:
            continue
        if allowed is not None and not allowed(t):
            continue
        try:
            plan = apply(t, build(int(corner[0]), int(corner[1])))
        except ValueError:
            continue  # e.g. a fluid machine that cannot be mirrored
        score = sum(port_score(*row) for row in wants(plan))
        found.append((score, index, Placement(score, t, plan, target)))
    found.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in found]
