"""Feed heads of the paired furnace bank (``smelting.paired_furnace_bank``), chosen by the flow model.

The bank's own ("shared") head side-loads raw onto one lane and coal onto the other lane of
one short belt and THEN splits it to the two furnace rows, so each row gets half a lane of raw
(a quarter belt: 12 stone furnaces of ore, 225 ore/min on a yellow belt). The "split" head
(:func:`split_feed_head`) is the community layout: the raw belt is split head-on first and each
half side-loads onto its row's input belt, filling that lane; coal does the same on the other
lane. One yellow raw belt then feeds two rows of 24 stone furnaces.

Both heads occupy the canonical columns ``x0 .. x0 + HEAD_COLUMNS - 1`` of the bank's 12 rows;
furnace slots, output belt, ports other than the coal port and the reserved growth are the
same, so a head swap never moves a furnace slot. Which head a new bank gets is decided by
:func:`rank_heads` (belt_flow at full reserved growth, docs/v2/LAYOUT_PRINCIPLES.md "Belt
lanes and bandwidth"): steel/brick blocks (``planned_furnaces.head_choice``) and the smelting
district (``smelting_site.district_head_choice``) use it.
"""

from __future__ import annotations

from factorio_agent.core.deterministic_builder import _plan

from .bandwidth_audit import SHORTFALL as BANDWIDTH_SHORTFALL
from .board import entity

HEADS = ("shared", "split")
HEAD_COLUMNS = 8  # canonical columns x0 .. x0 + 7 hold the feed head; slot belts start at x0 + 8


def _belt(x0, y0, x, y, direction):
    return entity("transport-belt", x0 + x + 0.5, y0 + y + 0.5, direction)


def _port(item, x, y, facing):
    return {"kind": "item", "item": item, "position": {"x": x, "y": y}, "facing": facing, "direction": "input"}


def split_feed_head(x0: int, y0: int) -> tuple[list, dict, list]:
    """Feed head that halves each input once: ``(entities, coal port, underground pairs)``.

    It splits the raw belt first (both lanes, head-on), then side-loads each half onto its
    row's input belt (the near lane takes up to a full lane), and does the same with coal
    from the other side; one underground carries the upper row's coal under the lower
    input row. Canonical frame (R raw port, C coal port facing north, S splitter, U/u
    underground in/out, row input belts continue at x0 + 8)::

        R S > > > > > v
        . S > > > v > >    <- upper input row: x0 + 6 stub, both side-loads at x0 + 7
        . . . . . v > ^
        . . . . . v ^ .
        . . . . . v u .
        . . . . > > > v    <- lower input row head: x0 + 4 stub, both side-loads at x0 + 5
        . . . . . ^ . v
        . . . . . ^ U v
        . . . . . S S v
        . . . . . ^ . v
        . . . . . ^ . v
        . . . . . C . >    -> lower input row (x0 + 8 ..)

    Lanes match the shared head: raw on the left lane, coal on the right lane of both rows.
    """
    E, S, N = 4, 8, 0
    tiles = [((0, 0), E), *(((x, 0), E) for x in range(2, 7)), ((7, 0), S)]  # raw port, branch A
    tiles += [((2, 1), E), ((3, 1), E), ((4, 1), E), ((5, 1), S), ((5, 2), S), ((5, 3), S), ((5, 4), S)]  # branch B
    tiles += [((6, 1), E), ((7, 1), E)]  # upper input row: stub, double side-load
    tiles += [((4, 5), E), ((5, 5), E), ((6, 5), E), ((7, 5), S)]  # lower input row: stub, double side-load, curve
    tiles += [((7, y), S) for y in range(6, 11)] + [((7, 11), E)]
    tiles += [((5, 11), N), ((5, 10), N), ((5, 9), N), ((5, 7), N), ((5, 6), N)]  # coal port, coal to the lower row
    tiles += [((6, 3), N), ((6, 2), E), ((7, 2), N)]  # coal to the upper row, after the underground
    es = [_belt(x0, y0, x, y, d) for (x, y), d in tiles]
    es += [
        dict(entity("splitter", x0 + 1.5, y0 + 1.0, E), _width=2, _height=1),  # raw: tiles (1,0), (1,1)
        dict(entity("splitter", x0 + 6.0, y0 + 8.5, N), _width=2, _height=1),  # coal: tiles (5,8), (6,8)
        dict(entity("underground-belt", x0 + 6.5, y0 + 7.5, N), belt_to_ground_type="input"),
        dict(entity("underground-belt", x0 + 6.5, y0 + 4.5, N), belt_to_ground_type="output"),
    ]
    pairs = [{"input": [x0 + 6.5, y0 + 7.5], "output": [x0 + 6.5, y0 + 4.5], "direction": N, "span": 3}]
    return es, _port("coal", x0 + 5.5, y0 + 11.5, N), pairs


def with_head(bank: dict, x0: int, y0: int, head: str) -> dict:
    """``bank`` (canonical, corner ``(x0, y0)``) with feed head ``head``; ``bank`` is changed in place.

    "split" replaces every belt and splitter of the head columns by :func:`split_feed_head`
    and the coal port by its port; "shared" keeps the bank's own head. Both record
    ``feed_head`` so audits and reworks know which head a saved bank has.
    """
    if head == "split":
        es, coal, pairs = split_feed_head(x0, y0)
        kept = [
            e
            for e in bank["entities"]
            if not (e["name"] in ("transport-belt", "splitter") and e["position"]["x"] < x0 + HEAD_COLUMNS)
        ]
        bank["entities"] = kept + es
        bank["ports"] = [bank["ports"][0], coal, *bank["ports"][2:]]
        bank["underground_pairs"] = pairs
        bank["required_items"] = _plan(bank["entities"], [])["required_items"]
    elif head != "shared":
        raise ValueError(f"unknown furnace feed head {head!r}")
    bank["feed_head"] = head
    return bank


def head_entities(bank: dict, x0: int) -> list:
    """Belts, splitters and undergrounds of a canonical bank's head columns (rework scope)."""
    return [
        e
        for e in bank["entities"]
        if e["name"] in ("transport-belt", "splitter", "underground-belt") and e["position"]["x"] < x0 + HEAD_COLUMNS
    ]


def rank_heads(audit_for, size_for) -> dict:
    """The head whose flow model meets the full bank's demand x margin without waste.

    ``audit_for(head)`` is the belt_flow summary of the bank at full reserved growth,
    ``size_for(head)`` its built entity count. Ranked by (shortfalls, wasted capacity
    findings, entities): the split head wins whenever the shared head halves a lane twice.
    """
    rows = []
    for head in HEADS:
        counts = audit_for(head)["counts"]
        rows.append(
            {
                "head": head,
                "shortfalls": sum(counts.get(k, 0) for k in BANDWIDTH_SHORTFALL),
                "waste": counts.get("side_load_before_splitter", 0),
                "entities": size_for(head),
                "counts": counts,
            }
        )
    best = min(rows, key=lambda r: (r["shortfalls"], r["waste"], r["entities"]))
    return {"head": best["head"], "candidates": rows}
