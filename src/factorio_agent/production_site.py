"""Place a canonical produced line on the master plan and connect it (planning only).

``place_line`` asks the shared ``SiteService`` for district blocks of the
line's footprint (``returns_to_bus=False``: a stock line ends in a chest;
``returns_to_bus=True`` is accepted for later bus products), turns the line
so its external input ports face the block's bus-adjacent tap band
(``site.fit.orientations``, rotations only: a mirrored row would swap belt
lanes the row audit was written for), re-audits the turned line, routes
every input port from its source (``transport.plan_connection``), bridges
power (``manufacturing.connect_power_groups``) and finishes with
``planning.preflight``. The first block that passes everything is kept
(``site.placement.settle``); every other candidate is released.
"""

from __future__ import annotations

from typing import Any

from factorio_agent.core.deterministic_builder import _plan

from .manufacturing import connect_power_groups
from .planning import overlap, preflight
from . import planned_tap
from .production_layout import audit_line, design_line, input_demand
from .production_sources import resolve
from .site.fit import orientations
from .site.placement import block_wants, settle
from .site.transform import apply as apply_transform
from .smelting import merge_entities
from .transport import plan_connection, xy

CANDIDATES = 4
LAYOUT_POLICY = "production-row-v1"


def footprint_size(line: dict) -> tuple[int, int]:
    (x0, y0), (x1, y1) = line["reserved_area"]
    return int(x1 - x0), int(y1 - y0)


def builder_for(catalog: Any, design: dict):
    """``build(x0, y0)``: the canonical line with its reserved area's corner at (x0, y0)."""
    (cx0, cy0), _ = design["reserved_area"]

    def build(x0: int, y0: int) -> dict:
        origin = (0.5 + x0 - cx0, 0.5 + y0 - cy0)
        return design_line(catalog, origin=origin, **design["arguments"])

    return build


def _output_facing(line: dict) -> dict:
    """The chest port with the main row's flow as its facing (for orientation scoring)."""
    belt_out = next(p for p in line["main"]["ports"] if p["direction"] == "output")
    return dict(line["output"], facing=belt_out["facing"])


def _taps(game, builder, obs, board, line, owner, block, ports, catalog):
    """Planned-bus taps and routes for ``ports`` (``planned_tap.connect_inputs``)."""
    if not ports:
        return {
            "ok": True,
            "entities": list(line["entities"]),
            "routes": [],
            "taps": [],
            "ignored_units": [],
            "sources": {},
        }
    span = planned_tap.max_span(game)
    if span is None:
        return {"ok": False, "reason": "planned_bus_underground_span_unknown"}
    demand = {p["item"]: input_demand(catalog, line, p["item"]) for p in ports}
    bus = planned_tap.published(board, obs["world_id"])
    if bus is None:
        return {"ok": False, "reason": "planned_bus_world_changed"}
    return planned_tap.connect_inputs(
        game,
        builder,
        obs,
        bus,
        owner=owner,
        allocation=block,
        inputs=ports,
        entities=list(line["entities"]),
        demand=demand,
        span=span,
        route=plan_connection,
    )


def _routes(game, builder, obs, board, line, owner, product, block=None, catalog=None):
    found_by_port = [(port, resolve(board, product, port["item"], owner=owner)) for port in line["inputs"]]
    for _, found in found_by_port:
        if not found["ok"]:
            return {"ok": False, **found}
    tapped = [port for port, found in found_by_port if found["kind"] == "planned_bus_tap"]
    taps = _taps(game, builder, obs, board, line, owner, block, tapped, catalog)
    if not taps["ok"]:
        return taps
    entities = taps["entities"]
    routes = list(taps["routes"])
    connections = [
        {
            "item": p["item"],
            "source": taps["sources"][p["item"]]["position"],
            "destination": p["position"],
            "owner": owner,
        }
        for p in tapped
    ]
    sources = [{"item": p["item"], "kind": "planned_bus_tap", "block": "shared_main_bus"} for p in tapped]
    extra = {"ignored_units": tuple(taps["ignored_units"])} if taps["ignored_units"] else {}
    for port, found in found_by_port:
        if found["kind"] == "planned_bus_tap":
            continue
        route = plan_connection(game, builder, obs, found["source"], port, entities, **extra)
        if not route.get("ok"):
            return {"ok": False, "reason": route.get("reason", "route_failed"), "item": port["item"]}
        routes.append(route)
        entities = merge_entities(entities, route["entities"])
        sources.append({"item": port["item"], "kind": found["kind"], "block": found["block"]})
        connections.append(
            {
                "item": port["item"],
                "source": found["source"]["position"],
                "destination": port["position"],
                "owner": owner,
            }
        )
    return {
        "ok": True,
        "entities": entities,
        "routes": routes,
        "connections": connections,
        "sources": sources,
        "taps": taps["taps"],
        "ignored_units": taps["ignored_units"],
    }


def place_line(game, builder, obs, catalog, site, board, design: dict, *, owner: str, returns_to_bus: bool = False):
    """Allocate, orient, route, power and preflight one line; a state dict or a failure."""
    if site is None:
        return {"ok": False, "reason": "production_row_requires_site_service"}
    product = design["spec"]["product"]
    size = footprint_size(design)
    try:
        blocks = site.allocate_candidates(
            "production",
            size[0],
            size[1],
            name=owner,
            count=CANDIDATES,
            returns_to_bus=returns_to_bus,
            world_id=obs["world_id"],
        )
    except Exception as exc:  # noqa: BLE001 - the allocator reports, never crashes the job
        return {"ok": False, "reason": "production_row_site_allocation_failed", "detail": str(exc)[:200]}
    build = builder_for(catalog, design)
    failures = []
    for block in blocks:
        wants = block_wants(block, lambda plan: plan["inputs"], lambda plan: [_output_facing(plan)])
        for placement in orientations(build, size, block.rect, wants, allowed=lambda t: t.mirror is None)[:2]:
            line = placement.plan
            findings = audit_line(catalog, line)
            if findings:
                failures.append({"allocation": block.name, "reason": "turned_line_audit_failed", "audit": findings})
                continue
            if any(
                overlap(line["reserved_area"], b["reserved_area"])
                for b in board.get("blocks", {}).values()
                if isinstance(b, dict) and "reserved_area" in b
            ):
                failures.append({"allocation": block.name, "reason": "board_reservation_overlap"})
                continue
            routed = _routes(game, builder, obs, board, line, owner, product, block, catalog)
            if not routed["ok"]:
                failures.append({"allocation": block.name, **routed})
                continue
            groups = [line["main"]] + ([line["sub"]] if line["sub"] else [])
            power = connect_power_groups(game, builder, obs, groups, routed["entities"])
            if not power.get("ok"):
                failures.append({"allocation": block.name, "reason": power.get("reason", "power_failed")})
                continue
            entities = merge_entities(routed["entities"], power["entities"])
            check = preflight(game, builder, obs, entities, ignored_units=routed["ignored_units"])
            if not check.get("ok"):
                failures.append({"allocation": block.name, "reason": check.get("reason", "preflight_failed")})
                continue
            obstacles = {(e["name"], *xy(e)): e for r in routed["routes"] + [check] for e in r["clear_obstacles"]}
            plan = _plan(
                entities,
                line["inputs"] + [line["output"]],
                reserved_area=line["reserved_area"],
                underground_pairs=[pair for r in routed["routes"] for pair in r.get("underground_pairs", [])]
                + planned_tap.tap_pairs(routed["taps"]),
                clear_obstacles=list(obstacles.values()),
                clearance_world_id=obs["world_id"],
                layout_policy=LAYOUT_POLICY,
                recipe=design["spec"]["recipe"],
                product=product,
            )
            settle(site, obs["world_id"], owner, block.name)
            return {
                "ok": True,
                "allocation": block.name,
                "transform": placement.transform.to_dict(),
                "line": line,
                "canonical": apply_transform(placement.transform.inverse(), line),
                "plan": plan,
                "connections": routed["connections"],
                "sources": routed["sources"],
                "taps": routed["taps"],
                "route_metrics": [r.get("route_metrics") for r in routed["routes"]],
            }
    settle(site, obs["world_id"], owner, None)
    return {
        "ok": False,
        "reason": "no_production_row_site_with_complete_connections",
        "candidates": len(blocks),
        "failures": failures[-6:],
    }


def turned(state: dict, canonical: dict) -> dict:
    """``canonical`` (an extended canonical line) turned like the placed line."""
    from .site.transform import Transform

    return apply_transform(Transform.from_dict(state["transform"]), canonical)
