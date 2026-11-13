"""Plate source ports for belt consumers (red science first).

The smelting district registers one belt output port per plate product
(``board["blocks"]["smelting_district"]["ports"]``, written by
``smelting.SmeltingDistrict.step``; the same ports are in
``smelting-district.json`` ``plan.ports``). Port positions and ``facing``
are already in world coordinates, so a rotated or mirrored district needs
no special handling here.

``smelter-lanes.json`` is the old development map's supply file (written
only by ``paired_smelting``). It is read only when the district gives no
port for an item and the file belongs to the same world and catalog.

A port is *taken* when a ``board["connections"]`` row of another owner
starts at its position; a taken port is never handed out twice.

Resolver for consumers beside the bus (``plate_bus``): when a planned main
bus carrying every requested plate is published for this world, plates come
from bus taps (``planned_tap.connect_inputs``) and the district ports are
not handed out; the district ports (and green science's district splitter
branch) are only the fallback for a world without a planned bus.
"""

from __future__ import annotations

import json
from pathlib import Path

PLATES = ("iron-plate", "copper-plate")
DISTRICT_BLOCK = "smelting_district"
LEGACY_FILE = "smelter-lanes.json"


def _key(position) -> tuple | None:
    if isinstance(position, dict) and "x" in position and "y" in position:
        return (position["x"], position["y"])
    return None


def _belt_outputs(ports):
    """Belt output ports (not chest ports), in order."""
    return [
        p
        for p in ports or []
        if isinstance(p, dict)
        and p.get("direction") == "output"
        and "name" not in p
        and isinstance(p.get("facing"), int)
        and _key(p.get("position")) is not None
    ]


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def district_ports(root: Path, board: dict, world_id: str) -> list[dict]:
    """The district's plate output ports: the registered board block, else the saved district plan."""
    block = (board.get("blocks") or {}).get(DISTRICT_BLOCK)
    if isinstance(block, dict):
        return _belt_outputs(block.get("ports"))
    state = _read(Path(root) / "smelting-district.json")
    if state.get("world_id") != world_id:
        return []
    return _belt_outputs(state.get("plan", {}).get("ports"))


def legacy_ports(root: Path, world_id: str, catalog_fingerprint: str) -> list[dict]:
    """Output ports of the old ``smelter-lanes.json`` when it belongs to this world and catalog."""
    lanes = _read(Path(root) / LEGACY_FILE)
    if lanes.get("world_id") != world_id or lanes.get("catalog_fingerprint") != catalog_fingerprint:
        return []
    return _belt_outputs([p for module in lanes.get("modules", []) for p in module.get("ports", [])])


def plate_sources(root, board: dict, world_id: str, catalog_fingerprint: str, *, owner: str, items=PLATES) -> dict:
    """One free source port per plate item for ``owner``.

    Returns ``{"ok": True, "ports": {item: port}, "origin": {item: "smelting_district"|"smelter_lanes"}}``
    or ``{"ok": False, "reason", "item"}``. Ports already connected by
    ``owner`` itself count as free, so a re-plan finds the same port.
    """
    taken = {
        _key(row.get("source"))
        for row in board.get("connections", [])
        if isinstance(row, dict) and row.get("owner") != owner
    } - {None}
    groups = (
        ("smelting_district", district_ports(root, board, world_id)),
        ("smelter_lanes", legacy_ports(root, world_id, catalog_fingerprint)),
    )
    ports, origin = {}, {}
    for item in items:
        offered = False
        for name, group in groups:
            for p in group:
                if p.get("item") != item:
                    continue
                offered = True
                if _key(p["position"]) not in taken:
                    ports[item], origin[item] = dict(p), name
                    break
            if item in ports:
                break
        if item not in ports:
            reason = "plate_source_port_taken" if offered else "plate_source_port_missing"
            return {"ok": False, "reason": reason, "item": item}
    return {"ok": True, "ports": ports, "origin": origin}


def plate_bus(board: dict, world_id: str, items=PLATES) -> dict | None:
    """The published planned bus when it carries every item of ``items``; None selects the fallback."""
    from .planned_tap import carries, published

    bus = published(board, world_id)
    if bus is None or not all(carries(bus, item) for item in items):
        return None
    return bus


def bus_budget(bus: dict, nominal: dict, *, owner: str) -> dict:
    """Plates/minute per item a new tap consumer may plan with (``planned_tap.tap_budget``)."""
    from .planned_tap import claimed, tap_budget

    return {item: tap_budget(rate, claimed(bus, item, owner=owner)) for item, rate in nominal.items()}
