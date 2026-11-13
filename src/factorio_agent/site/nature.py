"""The one rule for trees and rocks on planned sites.

Owner's rule (2026-11-13): the layout is never routed around trees. Blocks
belong next to the main bus in the slot the plan gives them; a site search
that dodged forests pushed them away from the bus and tangled the layout.

* Minable neutral nature (trees, rocks) never rejects or displaces a site
  whose position the plan decides: the bus, bus-side block slots, their
  feed and tap routes and the reserved growth strips. Position is compared
  first; the tree count only breaks ties between otherwise equal positions
  (:func:`ranked`, :func:`tiebreak`).
* There is no cap on how much nature one plan clears. The plan lists every
  obstacle in ``clear_obstacles`` and the builder clears them first, as a
  clearing phase of the same job: each step checks one batch of
  :data:`CLEARANCE_BATCH` obstacles (:func:`batches`) and returns one paid
  ``mine`` action, so a large forest is cleared over many batches.
* Water, cliffs, non-minable or player entities, ore (resource policy) and
  other registered blocks still block.

Mining outposts and off-bus temporary cells (the startup science cell, the
hand-fed supply cell) have a free position and may still prefer clear ground.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, TypeVar

# Obstacles one builder clearance query checks. Bounds the query payload, not
# the plan: a plan may list any number of obstacles.
CLEARANCE_BATCH = 96

T = TypeVar("T")


def tiebreak(clearable: int) -> tuple[int]:
    """Secondary sort key after the position score: fewer trees win a tie only."""
    return (int(clearable),)


def ranked(rows: Iterable[tuple[float, int, T]]) -> list[T]:
    """Order ``(position_cost, clearable_count, item)`` rows, position first.

    The clearable count is compared only between rows whose position cost is
    equal, so nature can never move an item ahead of a better position.
    """
    return [item for _, _, item in sorted(rows, key=lambda row: (row[0], *tiebreak(row[1])))]


def batches(obstacles: Sequence[Any], size: int = CLEARANCE_BATCH) -> list[list[Any]]:
    """Split a plan's ``clear_obstacles`` into consecutive builder batches."""
    if size < 1:
        raise ValueError("clearance batch size must be positive")
    return [list(obstacles[i : i + size]) for i in range(0, len(obstacles), size)]
