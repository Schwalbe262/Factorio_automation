"""Where a produced line's input belts come from (reads the PCB board only).

Every consumer registers ``board["connections"]`` rows
``{"item", "source": <position>, "destination", "owner"}`` when it is laid
out; a port whose position is the ``source`` of a row is *taken*.

Source order for one ingredient ``item`` of product ``product``:

0. on the planned bus (template ``planned_main_bus``) a plate with a built
   trunk: a tap planned together with the block (``planned_tap``);
1. the installed shared-main-bus tap named ``tap_name(product, item)``
   (``board["shared_main_bus"]["ports"]``), the mechanism the construction
   mall and steel supply use once the main bus exists;
2. a free belt output port of a registered board block carrying ``item``
   (smelting district plate ports, another block's belt output), not taken
   by any connection. Roadmap guard: the smelting district's ports go to red
   science first, so they count as free only once a block named
   ``RED_SCIENCE_BLOCK`` is registered (whatever ports it left are spare).

A chest port (an output with a ``name``, such as a stock tail) is never a
belt source. When no source exists and the main bus exists,
``tap_parameters`` proposes the bus tap to install; otherwise the caller
reports a blocker naming the item.
"""

from __future__ import annotations

from typing import Any

SMELTING_BLOCK = "smelting_district"
RED_SCIENCE_BLOCK = "automation_science"
TAP_PREFIX = "produce-"


def tap_name(product: str, item: str) -> str:
    """Bus tap name for one line input (BusTap names: lower-case, <= 80 chars)."""
    return (TAP_PREFIX + product + "-" + item)[:80]


def _key(position: Any) -> tuple | None:
    if isinstance(position, dict) and "x" in position and "y" in position:
        return (position["x"], position["y"])
    if isinstance(position, (list, tuple)) and len(position) == 2:
        return (position[0], position[1])
    return None


def taken_positions(board: dict) -> set[tuple]:
    return {_key(row.get("source")) for row in board.get("connections", []) if isinstance(row, dict)} - {None}


def bus_tap_port(board: dict, product: str, item: str) -> dict | None:
    bus = board.get("shared_main_bus") or {}
    name = tap_name(product, item)
    port = next((p for p in bus.get("ports", []) if p.get("name") == name), None)
    if port is None:
        return None
    return dict(port, item=item, direction="output")


def free_board_ports(board: dict, item: str, *, exclude: tuple[str, ...] = ()) -> list[dict]:
    """Untaken belt output ports carrying ``item``, in board order, as ``(block, port)`` dicts."""
    taken = taken_positions(board)
    blocks = board.get("blocks") or {}
    found = []
    for name, block in blocks.items():
        if name in exclude or not isinstance(block, dict):
            continue
        if name == SMELTING_BLOCK and RED_SCIENCE_BLOCK not in blocks:
            continue
        for port in block.get("ports", []):
            if (
                port.get("direction") == "output"
                and port.get("item") == item
                and "name" not in port
                and isinstance(port.get("facing"), int)
                and _key(port.get("position")) not in taken
            ):
                found.append({"block": name, "port": dict(port)})
    return found


def resolve(board: dict, product: str, item: str, *, owner: str) -> dict:
    """``{"ok": True, "kind", "source", "block"}`` or ``{"ok": False, "reason", "item"}``.

    On the planned bus (``planned_tap``) an item with a built trunk resolves
    to kind ``planned_bus_tap`` without a source: the tap is planned with the
    block's site (``production_site.place_line``), so no free port is taken.
    """
    from .planned_tap import carries, published

    if carries(published(board, board.get("world_id")), item):
        return {"ok": True, "kind": "planned_bus_tap", "source": None, "block": "shared_main_bus"}
    tap = bus_tap_port(board, product, item)
    if tap is not None:
        return {"ok": True, "kind": "bus_tap", "source": tap, "block": "shared_main_bus"}
    ports = free_board_ports(board, item, exclude=(owner,))
    if ports:
        return {"ok": True, "kind": "board_port", "source": ports[0]["port"], "block": ports[0]["block"]}
    return {
        "ok": False,
        "reason": "production_row_input_source_missing",
        "item": item,
        "needs": f"a free belt output port of {item} on the board or the bus tap {tap_name(product, item)}",
        "bus_available": bool(board.get("shared_main_bus")),
    }


def sourced_items(board: dict, product: str, *, owner: str):
    """Predicate for ``production_layout``: can ``item`` be delivered by a source now?"""

    def sourced(item: str) -> bool:
        return resolve(board, product, item, owner=owner)["ok"]

    return sourced


def plate_sources(board: dict, items, *, world_id=None) -> dict:
    """``{item: bool}``: can a new produce row get a belt of ``item`` now (``resolve`` or a bus tap)?

    For the produce autopilot: a row proposed before any source exists only blocks
    ``production_row_input_source_missing`` (all-1113-2107: 6 rows in 3 runs whose bus
    never came). A board of another world has no source.
    """
    same = isinstance(board, dict) and (world_id is None or board.get("world_id") == world_id)
    out = {}
    for item in items:
        if not same:
            out[item] = False
            continue
        found = resolve(board, "", item, owner="")["ok"]
        out[item] = bool(found or tap_parameters(board, "", item, 1.0))
    return out


def tap_parameters(board: dict, product: str, item: str, demand_per_minute: float) -> dict | None:
    """First free surplus-tap row on the shared main bus for ``item``; None without a bus line."""
    bus = board.get("shared_main_bus")
    if not bus:
        return None
    from .industrial import free_tap_parameters

    try:
        rows = free_tap_parameters(
            bus, name=tap_name(product, item), item=item, demand_per_minute=round(demand_per_minute, 3)
        )
    except (ValueError, KeyError, TypeError):
        return None
    return rows[0] if rows else None
