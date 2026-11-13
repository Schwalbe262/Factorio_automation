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

With ``design["candidates"]`` (``layout_variants.candidates``) the line is chosen
first (``_choose``): each candidate (at most ``layout_selection.PROBE_LIMIT``) is
probed on its own first allocation and best orientation, scored by
``layout_selection.score`` (intrinsic terms + the orientation's port cost),
optionally explored, and then placed in that order with the code above; the
result carries the ``layout`` record (chosen genome, scores, runner-ups).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from factorio_agent.core.deterministic_builder import _plan

from .manufacturing import connect_power_groups
from .planning import overlap, preflight
from . import planned_tap, remedy
from .production_layout import adapter_intrusions, audit_line, design_line, input_demand, rows
from .production_sources import ROUTE_TAP, resolve, route_tap_sites
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


def foreign_keepouts(builder, obs, board, owner) -> list[dict]:
    """1x1 pole keepouts on every other block's planned-but-unbuilt tile (``board.planned_unbuilt_hardware``).

    The planned bus (``board["shared_main_bus"]``) counts as a block here.
    Belt routing already avoids other blocks' unbuilt routes; the pole bridge
    did not, and a row's bridge pole was planned on the trunk of a bus still
    being built: the bus belt went down first and the row blocked for good on
    "reserved block placement is obstructed" (fleets auto-1113-1708 seed
    3707974081, auto-1113-1758 seed 4101796979).
    """
    from .board import entity, planned_unbuilt_hardware
    from .smelting import RESERVED_TILE

    blocks = dict(board.get("blocks") or {})
    if isinstance(board.get("shared_main_bus"), dict):
        # The planned bus is published beside the blocks, not among them.
        blocks.setdefault("shared_main_bus", board["shared_main_bus"])
    tiles = planned_unbuilt_hardware(builder, obs, blocks, exclude=(owner,))
    return [entity(RESERVED_TILE, x, y) for x, y in sorted(tiles)]


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
    belt_ports = [port for port in line["inputs"] if port.get("kind") != "fluid"]
    found_by_port = [(port, resolve(board, product, port["item"], owner=owner)) for port in belt_ports]
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
    ignored = list(taps["ignored_units"])
    route_taps = []
    for port, found in found_by_port:
        if found["kind"] == "planned_bus_tap":
            continue
        if found["kind"] == ROUTE_TAP:
            tapped = _route_tap(game, builder, obs, owner, found, port, entities, catalog, line)
            if not tapped["ok"]:
                return tapped
            routes.append(tapped["route"])
            entities = merge_entities(entities, tapped["tap"]["entities"] + tapped["route"]["entities"])
            route_taps.append(tapped["tap"])
            ignored += tapped["tap"]["remove_units"]
            sources.append({"item": port["item"], "kind": ROUTE_TAP, "block": found["block"]})
            connections.append(tapped["connection"])
            continue
        route = plan_connection(game, builder, obs, found["source"], port, entities, **extra)
        if not route.get("ok"):
            return {"ok": False, "reason": route.get("reason", "route_failed"), "item": port["item"]}
        routes.append(route)
        entities = merge_entities(entities, route["entities"])
        sources.append({"item": port["item"], "kind": found["kind"], "block": found["block"]})
        connection = {
            "item": port["item"],
            "source": found["source"]["position"],
            "destination": port["position"],
            "owner": owner,
        }
        if catalog is not None:
            # Declared claim on the source port (planned_furnaces.furnace_block_claims reads it).
            connection["demand_per_minute"] = round(input_demand(catalog, line, port["item"]), 3)
        connections.append(connection)
    return {
        "ok": True,
        "entities": entities,
        "routes": routes,
        "connections": connections,
        "sources": sources,
        "taps": taps["taps"],
        "route_taps": route_taps,
        "ignored_units": ignored,
    }


def _route_tap(game, builder, obs, owner, found, port, entities, catalog, line) -> dict:
    """A splitter tap on the taken port's route and the belt from its branch to ``port``.

    ``{"ok", "tap", "route", "connection"}`` or a failure naming the sites tried. Sites come from
    the observed route (``production_sources.route_tap_sites``); a site whose tiles meet this
    plan's entities is skipped; the first site with a clean route wins.
    """
    item = port["item"]
    sites = route_tap_sites(obs, found["source"], item=item, name=f"{owner}:{item}")
    occupied = {xy(e) for e in entities}
    failures = []
    for tap in sites:
        if any(xy(p) in occupied for p in [tap["port"]] + [{"position": tap["splitter"]["position"]}]):
            failures.append("route_tap_site_meets_the_row")
            continue
        route = plan_connection(
            game, builder, obs, tap["port"], port, list(entities) + tap["entities"], ignored_units=tap["remove_units"]
        )
        if not route.get("ok"):
            failures.append(route.get("reason", "route_failed"))
            continue
        connection = {
            "item": item,
            "source": dict(tap["port"]["position"]),
            "destination": port["position"],
            "owner": owner,
            "tapped_connection": {k: found["tapped_connection"].get(k) for k in ("owner", "source", "destination")},
        }
        if catalog is not None:
            connection["demand_per_minute"] = round(input_demand(catalog, line, item), 3)
        tap["tapped_connection"] = connection["tapped_connection"]
        return {"ok": True, "tap": tap, "route": route, "connection": connection}
    return {
        "ok": False,
        "reason": "production_row_route_tap_unavailable",
        "item": item,
        "tapped_port": found["source"].get("position"),
        "sites": len(sites),
        "failures": failures[-6:],
    }


def _candidate_design(design: dict, candidate: dict) -> dict:
    line = candidate["line"]
    out = dict(line, arguments=dict(design["arguments"], genome=candidate["genome"].to_dict()))
    if design.get("fluid_source"):
        out["fluid_source"] = design["fluid_source"]
    return out


def _no_mirror(t) -> bool:
    return t.mirror is None


def _choose(obs, catalog, site, design: dict, owner: str, returns_to_bus: bool) -> tuple[list[dict], dict]:
    """Feasible options best first (probe: one allocation, best orientation) and the exploration choice."""
    from . import layout_selection as selection

    demand = design.get("demand") or selection.demand_for(design["spec"]["rate_per_minute"])
    config = design.get("layout_settings") or selection.Settings()
    options, infeasible = [], []
    for candidate in design["candidates"][: selection.PROBE_LIMIT]:
        size = footprint_size(candidate["line"])
        try:
            blocks = site.allocate_candidates(
                "production",
                size[0],
                size[1],
                name=owner,
                count=1,
                returns_to_bus=returns_to_bus,
                world_id=obs["world_id"],
            )
        except Exception:  # noqa: BLE001 - a candidate without a block is just infeasible
            blocks = []
        if not blocks:
            infeasible.append({"variant": candidate["descriptor"]["id"], "reason": "no_allocation"})
            continue
        block = blocks[0]
        wants = block_wants(block, lambda plan: plan["inputs"], lambda plan: [_output_facing(plan)])
        build = builder_for(catalog, _candidate_design(design, candidate))
        placed = orientations(build, size, block.rect, wants, allowed=_no_mirror)
        if not placed:
            infeasible.append({"variant": candidate["descriptor"]["id"], "reason": "no_orientation"})
            continue
        options.append(selection.option(candidate, demand, placed[0].score))
    settle(site, obs["world_id"], owner, None)
    options = selection.ranked(options)
    seed = design.get("layout_seed", 0)
    index, pool = selection.choose([o["score"] for o in options], config, seed) if options else (0, 0)
    return options, {"index": index, "pool": pool, "config": config, "infeasible": infeasible}


def place_line(game, builder, obs, catalog, site, board, design: dict, *, owner: str, returns_to_bus: bool = False):
    """Allocate, orient, route, power and preflight one line; a state dict or a failure."""
    if site is None:
        return {"ok": False, "reason": "production_row_requires_site_service"}
    if not design.get("candidates"):
        return _place_one(game, builder, obs, catalog, site, board, design, owner=owner, returns_to_bus=returns_to_bus)
    from . import layout_selection as selection

    options, choice = _choose(obs, catalog, site, design, owner, returns_to_bus)
    if not options:
        return {"ok": False, "reason": "no_layout_candidate_fits_a_site", "infeasible": choice["infeasible"][-6:]}
    # Remedy ladder rung "layout:next_variant" (remedy.RELAXATIONS): start with the next ranked candidate.
    chosen = options[(choice["index"] + int(remedy.option("layout_rotate", 0) or 0)) % len(options)]
    order = [chosen] + [o for o in options if o is not chosen]
    tried = []
    result = None
    for option in order:
        result = _place_one(
            game,
            builder,
            obs,
            catalog,
            site,
            board,
            _candidate_design(design, option["candidate"]),
            owner=owner,
            returns_to_bus=returns_to_bus,
        )
        if result["ok"]:
            result["layout"] = selection.record(
                options,
                option,
                config=choice["config"],
                pool_size=choice["pool"],
                tick=obs.get("tick"),
                fallback_from=tried or None,
                library=design.get("layout_library"),
                library_skipped=design.get("layout_skipped") or None,
                infeasible=choice["infeasible"] or None,
            )
            return result
        tried.append({"variant": option["id"], "reason": result.get("reason")})
    return dict(result, layout_failures=tried[-6:])


def _place_one(game, builder, obs, catalog, site, board, design: dict, *, owner: str, returns_to_bus: bool = False):
    """``place_line`` for one canonical line (module doc)."""
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
    keepouts = foreign_keepouts(builder, obs, board, owner)
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
            fluid = _fluid_route(game, builder, obs, catalog, line, routed["entities"], design, product)
            if not fluid["ok"]:
                failures.append({"allocation": block.name, **fluid})
                continue
            routed["entities"] = merge_entities(routed["entities"], fluid.get("entities", []))
            intrusions = adapter_intrusions(catalog, line, routed["entities"])
            if intrusions:
                failures.append({"allocation": block.name, "reason": "lane_merge_fed_by_route", "audit": intrusions})
                continue
            groups = rows(line)
            power = connect_power_groups(game, builder, obs, groups, routed["entities"] + keepouts)
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
                fluid_underground_pairs=deepcopy(fluid.get("underground_pairs", [])),
                preserved_fluid_crossings=deepcopy(fluid.get("preserved_fluid_crossings", [])),
                owned_fluid_crossings=deepcopy(fluid.get("owned_fluid_crossings", [])),
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
                "route_taps": routed.get("route_taps", []),
                "route_metrics": [r.get("route_metrics") for r in routed["routes"]],
                "fluid_handoff": fluid.get("handoff"),
            }
    settle(site, obs["world_id"], owner, None)
    return {
        "ok": False,
        "reason": "no_production_row_site_with_complete_connections",
        "candidates": len(blocks),
        "failures": failures[-6:],
    }


def _fluid_route(game, builder, obs, catalog, line, entities, design, product) -> dict:
    """The typed pipe route of a fluid row's header port (``production_fluids``); ``{"ok": True}`` without one."""
    from pathlib import Path

    from . import production_fluids

    ports = [p for p in line["inputs"] if p.get("kind") == "fluid"]
    if not ports:
        return {"ok": True}
    root = Path(game.cfg.runtime_dir)
    found = production_fluids.source(root, obs, catalog, design.get("fluid_source"), ports[0]["item"], product)
    if not found["ok"]:
        return found
    return production_fluids.route(game, builder, obs, line, entities, ports[0], found["source"])


def turned(state: dict, canonical: dict) -> dict:
    """``canonical`` (an extended canonical line) turned like the placed line."""
    from .site.transform import Transform

    return apply_transform(Transform.from_dict(state["transform"]), canonical)
