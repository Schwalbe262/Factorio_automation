"""Allocator sites for production blocks built from a canonical template.

``allocated_placements`` asks the shared :class:`site.service.SiteService`
for district blocks of the template's footprint, orients the template into
each block with :func:`site.fit.orientations` (input ports scored towards
the block's tap band on the bus-adjacent edge, outputs towards the block's
``output_point``), and returns ``(allocation_name, placement)`` pairs, best
orientation per block, in allocator order. The caller keeps the block it
builds with :func:`settle` and every other candidate is released.

Every failure (no service, survey failure, bus exhausted, footprint deeper
than the district strips) returns an empty list; callers with a site
service report that as their blocked reason.
"""

from __future__ import annotations

from .fit import orientations

DEFAULT_COUNT = 4


def block_wants(allocation, inputs, outputs=()):
    """``wants`` for :func:`site.fit.orientations` on one allocation.

    ``inputs(plan)`` / ``outputs(plan)`` return the template's input/output
    ports in the transformed plan.
    """
    tap = allocation.tap_rect.centre()

    def wants(plan):
        rows = [(port, tap, "in", allocation.input_side) for port in inputs(plan)]
        rows += [(port, allocation.output_point, "out", allocation.output_side) for port in outputs(plan)]
        return rows

    return wants


def allocated_placements(
    site,
    world_id,
    *,
    kind,
    name,
    size,
    build,
    inputs,
    outputs=lambda plan: (),
    returns_to_bus=False,
    count=DEFAULT_COUNT,
    block=None,
):
    """Oriented template placements on fresh allocations ``<name>#<i>``.

    ``size`` is the template footprint ``(w, h)``; blocks are requested as
    ``w`` along the bus by ``h`` deep (a quarter turn of the template fits
    whichever way the bus runs). ``block=(along, depth)`` requests another
    block shape of the same area, e.g. ``(h, w)`` for a template whose
    input edge is its ``h`` side.
    """
    if site is None:
        return []
    w, h = block or size
    try:
        blocks = site.allocate_candidates(
            kind, w, h, name=name, count=count, returns_to_bus=returns_to_bus, world_id=world_id
        )
    except Exception:  # noqa: BLE001 - the allocator is preferred, never a new failure mode
        return []
    found = []
    for block in blocks:
        best = orientations(build, size, block.rect, block_wants(block, inputs, outputs))
        if best:
            found.append((block.name, best[0]))
    used = {block_name for block_name, _placement in found}
    for block in blocks:
        if block.name not in used:
            site.release(block.name, world_id)
    return found


def settle(site, world_id, name: str, keep: str | None) -> None:
    """Keep allocation ``keep`` (None: keep nothing) among the ``<name>#*`` candidates."""
    if site is None:
        return
    try:
        site.keep_only(name + "#", keep, world_id)
    except Exception:  # noqa: BLE001 - releasing spare candidates must not fail the build
        return
