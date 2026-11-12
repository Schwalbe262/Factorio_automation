"""Inserter-limited machine rate for one row machine (pure arithmetic).

A row machine has one input edge and one output edge, each ``size`` tiles
long (``row.RowShape``). The input edge holds plain inserters for the near
belt and long-handed inserters for the far belt; the output edge holds plain
inserters. Each inserter carries ``params.inserter_rate`` items/minute.

Derating rule: a machine whose nameplate demand exceeds what its edges can
carry is run at the rate its inserters sustain instead of being rejected.

* one input belt: all ``size`` input tiles serve it;
* two input belts: the split ``near + far = size`` (each >= 1) that allows
  the highest craft rate is used;
* the output edge carries at most ``size`` inserters;
* effective crafts/minute = min(nameplate, input cap, output cap).

The row generator then places ``ceil(demand / rate)`` inserters per belt for
the effective rate, which always fits the edge by construction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeCapacity:
    """Highest crafts/minute the edges carry and the input split that reaches it."""

    crafts: float
    near: int
    far: int


def edge_capacity(
    size: int,
    belt_demand: tuple[float, ...],
    product_per_craft: float,
    rates: tuple[float, float, float],
) -> EdgeCapacity:
    """``belt_demand``: items per craft on each input belt (near first).

    ``rates`` = (near inserter, far long-handed inserter, output inserter)
    items/minute. Returns the inserter-limited crafts/minute (not capped by
    the machine's nameplate speed).
    """
    near_rate, far_rate, out_rate = rates
    if size < 1 or not belt_demand or len(belt_demand) > 2:
        raise ValueError("edge capacity needs a positive edge and one or two input belts")
    output_cap = size * out_rate / product_per_craft
    if len(belt_demand) == 1:
        return EdgeCapacity(min(size * near_rate / belt_demand[0], output_cap), size, 0)
    if size < 2:
        raise ValueError("two input belts need an input edge of at least two tiles")
    best = None
    for near in range(1, size):
        far = size - near
        crafts = min(near * near_rate / belt_demand[0], far * far_rate / belt_demand[1])
        if best is None or crafts > best.crafts + 1e-12:
            best = EdgeCapacity(crafts, near, far)
    return EdgeCapacity(min(best.crafts, output_cap), best.near, best.far)
