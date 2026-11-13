"""Typed fluid input of a ``produce`` fluid row: published source -> pipe route -> header port.

The same protocol as ``chemical_production`` (``docs/v2/FLUID_HANDOFFS.md``):

1. ``source(root, obs, catalog, key, fluid, item)``: the exact published outlet
   ``fluid-sources.json["sources"][key]`` (the row's ``fluid_source`` parameter, published by the
   tail's supply row: an independent intake or a ``fluid_distribution`` alias). Missing:
   ``production_row_fluid_source_missing`` (a planning failure naming the key).
2. ``route(...)``: ``chemical_production.plan_typed_fluid_connection`` from the source to the
   row's header port (bounded turns/detour, remedy ``route_relax`` rungs, other fluids hard,
   buried crossings compiled), then ``chemical_plan_isolation`` (no foreign fluid peer, no stolen
   buried pair). The result keeps the route's first neighbour (the handoff edge).
3. ``handoff_step``: before any build the edge is declared (``fluid_handoffs.declare_handoff``);
   once the first neighbour pipe stands, its unit is bound (``bind_handoff``).
4. ``handoff_bound``: the proof's check that the registry holds the bound edge for this row.

Consumer id: ``produce:<item>``. Pure planning in 1-2 (read-only queries); 3 writes registry
metadata only, never fluids.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from .transport import xy

CONSUMER_PREFIX = "produce:"


def consumer_id(item: str) -> str:
    return CONSUMER_PREFIX + item


def _registry(root: Path) -> dict:
    path = Path(root) / "fluid-sources.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def source(root, obs, catalog, key: str | None, fluid: str, item: str) -> dict:
    """``{"ok": True, "source"}`` or a planning failure naming what is missing."""
    if not key:
        return {"ok": False, "reason": "production_row_fluid_source_required", "item": fluid}
    registry = _registry(root)
    if registry.get("world_id") != obs.get("world_id") or registry.get("catalog_fingerprint") != catalog.fingerprint:
        return {"ok": False, "reason": "production_row_fluid_source_missing", "item": fluid, "fluid_source": key}
    found = (registry.get("sources") or {}).get(key)
    if not isinstance(found, dict):
        return {
            "ok": False,
            "reason": "production_row_fluid_source_missing",
            "item": fluid,
            "fluid_source": key,
            "needs": f"a published {fluid} outlet {key} (water_gathering / fluid_distribution supply row)",
        }
    if (
        found.get("kind") != "fluid"
        or found.get("item") != fluid
        or found.get("name") != "pipe"
        or found.get("direction") != "output"
        or type(found.get("unit_number")) is not int
    ):
        return {"ok": False, "reason": "production_row_fluid_source_not_exact", "item": fluid, "fluid_source": key}
    for owner, rows in (registry.get("handoffs") or {}).items():
        for row in (rows or {}).values():
            if row.get("source") == found and owner != consumer_id(item):
                return {
                    "ok": False,
                    "reason": "production_row_fluid_source_claimed",
                    "item": fluid,
                    "fluid_source": key,
                    "consumer": owner,
                }
    return {"ok": True, "source": deepcopy(found)}


def route(game, builder, obs, line: dict, entities: list[dict], port: dict, found: dict) -> dict:
    """The typed pipe route and its isolation check, or a failure with ``source``/``destination``."""
    from .chemical_production import chemical_plan_isolation, plan_typed_fluid_connection

    fluid = port["item"]
    header = [e for e in line["main"]["entities"] if e["name"] in ("pipe", "pipe-to-ground")]
    core = {
        "entities": [e for e in entities if e["name"] not in ("pipe", "pipe-to-ground")],
        "output_access": [],
        "reserved_area": [[0, 0], [0, 0]],  # the row's own tiles are hard through its entities
        "fluid_groups": {fluid: header},
    }
    destination = {k: port[k] for k in ("kind", "item", "position", "facing")}
    result = plan_typed_fluid_connection(game, builder, obs, found, destination, core)
    if not result.get("ok"):
        return {
            "ok": False,
            "reason": result.get("reason", "production_row_fluid_route_failed"),
            "item": fluid,
            "source": found.get("position"),
            "destination": port.get("position"),
        }
    nodes = header + [e for e in result["entities"] if (e["name"], *xy(e)) not in {(h["name"], *xy(h)) for h in header}]
    machine = next(e for e in line["main"]["entities"] if e.get("recipe"))
    plans = {fluid: {"source": found, "nodes": nodes, "route": result}}
    isolation = chemical_plan_isolation(game, plans, {"machine": machine})
    if not isolation.get("ok"):
        return {
            "ok": False,
            "reason": isolation.get("reason", "production_row_fluid_isolation_failed"),
            "item": fluid,
            "source": found.get("position"),
            "destination": port.get("position"),
        }
    neighbor = {
        k: v for k, v in result["first_neighbor"].items() if k in ("name", "position", "direction", "fluidbox_index")
    }
    return {
        "ok": True,
        "entities": result["entities"],
        "underground_pairs": result.get("underground_pairs", []),
        "preserved_fluid_crossings": result.get("preserved_fluid_crossings", []),
        "owned_fluid_crossings": result.get("owned_fluid_crossings", []),
        "handoff": {"fluid": fluid, "source": deepcopy(found), "first_neighbor": neighbor},
        "route_metrics": result.get("route_metrics"),
    }


def handoff_step(game, catalog, obs, item: str, handoff: dict | None) -> dict | None:
    """Declare the edge (every pass, idempotent) and bind it once the first neighbour stands."""
    if not handoff:
        return None
    from .chemical_production import _handoff

    who = consumer_id(item)
    planned = {k: v for k, v in handoff["first_neighbor"].items() if k != "unit_number"}
    declared = _handoff(game, catalog, obs, who, handoff["fluid"], handoff["source"], planned)
    if not declared.get("ok"):
        return {"status": "blocked", "reason": "production_row_fluid_handoff_not_authorized", "evidence": declared}
    live = next(
        (e for e in obs.get("entities", []) if e.get("name") == planned["name"] and xy(e) == xy(planned)),
        None,
    )
    if live is not None and type(live.get("unit_number")) is int:
        bound = _handoff(
            game,
            catalog,
            obs,
            who,
            handoff["fluid"],
            handoff["source"],
            dict(planned, unit_number=live["unit_number"]),
            bind=True,
        )
        if not bound.get("ok"):
            return {"status": "blocked", "reason": "production_row_fluid_handoff_not_bound", "evidence": bound}
    return None


def handoff_bound(root, obs, item: str, handoff: dict | None) -> bool:
    """The registry holds this row's bound edge on the exact source (proof side, fresh read)."""
    if not handoff:
        return True
    row = ((_registry(root).get("handoffs") or {}).get(consumer_id(item)) or {}).get(handoff["fluid"]) or {}
    return (
        row.get("phase") == "bound"
        and row.get("source") == handoff["source"]
        and row.get("world_id") == obs.get("world_id")
    )
