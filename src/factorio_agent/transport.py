"""Whole orthogonal corridors with explicit underground crossing pairs."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from . import evidence_capture, remedy
from .board import built_keys, entity
from .routing import LONG_ROUTE_TURNS, VECTORS, route_trunks
from .resource_policy import (
    RESOURCES,
    mining_reservations,
    annotate_mining_exit,
    mining_exit_candidates,
    exit_directions,
    MINING_EXIT_LUA,
    validate_annotations,
)


def xy(e):
    return e["position"]["x"], e["position"]["y"]


PIPE_WITNESS_LUA = r"""
local function surface_pipe_witness(e,q)
 if not e or not e.valid or e.name~="pipe"or e.type~="pipe"or e.force~=f or e.surface~=s
  or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return nil end
 local peers={}
 for _,peer in pairs(e.get_fluid_box_neighbours(1)or{})do local other=peer.entity
  if not other or not other.valid or other.force~=f or other.surface~=s or not other.unit_number then return nil end
  local reciprocal=false
  for _,back in pairs(other.get_fluid_box_neighbours(peer.index)or{})do if back.entity==e and back.index==1 then reciprocal=true end end
  if not reciprocal then return nil end
  peers[#peers+1]={name=other.name,position=pos(other.position),unit_number=other.unit_number,fluidbox_index=peer.index}
 end
 return{name="pipe",position=pos(e.position),unit_number=e.unit_number,world_id=args.world_id,force_index=f.index,peers=peers}
end
"""


def _valid_pipe_witness(pipe):
    def position(p, half=False):
        return (
            isinstance(p, dict)
            and set(p) == {"x", "y"}
            and all(type(v) in (int, float) and math.isfinite(v) and (not half or v % 1 == 0.5) for v in p.values())
        )

    if (
        not isinstance(pipe, dict)
        or pipe.get("name") != "pipe"
        or type(pipe.get("unit_number")) is not int
        or pipe["unit_number"] < 1
        or not isinstance(pipe.get("world_id"), str)
        or not pipe["world_id"]
        or type(pipe.get("force_index")) is not int
        or pipe["force_index"] < 1
        or not position(pipe.get("position"), True)
        or not isinstance(pipe.get("peers"), list)
    ):
        return False
    seen = set()
    for peer in pipe["peers"]:
        if (
            not isinstance(peer, dict)
            or not isinstance(peer.get("name"), str)
            or not peer["name"]
            or not position(peer.get("position"))
            or type(peer.get("unit_number")) is not int
            or peer["unit_number"] < 1
            or peer["unit_number"] == pipe["unit_number"]
            or type(peer.get("fluidbox_index")) is not int
            or peer["fluidbox_index"] < 1
            or (peer["unit_number"], peer["fluidbox_index"]) in seen
        ):
            return False
        seen.add((peer["unit_number"], peer["fluidbox_index"]))
    return True


def compile_crossings(route, foreign, *, maximum_span=5, verified_pipes=()):
    """Cross surface belts or exact verified pipes without modifying them.

    Every crossing is a straight input/output pair. Reject turns in buried
    spans, overlapping pairs and nearby same-axis mouths that could steal pairs.
    """
    if not route.get("ok"):
        return route
    segments = route["segments"]
    positions = [xy(e) for e in segments]
    surface = {xy(e): e for e in foreign if e["name"] == "transport-belt"}
    pipes = {}
    for pipe in verified_pipes:
        if not _valid_pipe_witness(pipe):
            return {"ok": False, "reason": "surface_pipe_crossing_requires_verified_identity"}
        if xy(pipe) in pipes and pipes[xy(pipe)] != pipe:
            return {"ok": False, "reason": "surface_pipe_crossing_identity_conflict"}
        pipes[xy(pipe)] = pipe
    for pipe in foreign:
        if (
            pipe["name"] == "pipe"
            and xy(pipe) in positions
            and (xy(pipe) not in pipes or pipes[xy(pipe)]["unit_number"] != pipe.get("unit_number"))
        ):
            return {"ok": False, "reason": "surface_pipe_crossing_requires_verified_identity"}
    crossable = {**surface, **pipes}
    underground = [e for e in foreign if e["name"] == "underground-belt"]
    hits = [i for i, p in enumerate(positions) if p in crossable]
    spans = []
    for index in hits:
        if spans and index <= spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], index + 1)
        else:
            spans.append((index - 1, index + 1))
    result = []
    pairs = []
    buried = set()
    mouths = {}
    for first, last in spans:
        if first <= 0 or last >= len(segments) - 1 or last - first > maximum_span:
            return {"ok": False, "reason": "crossing_has_no_clear_straight_mouths_or_exceeds_span"}
        direction = segments[first]["direction"]
        v = VECTORS[direction]
        if segments[first - 1]["direction"] != direction or segments[last]["direction"] != direction:
            return {"ok": False, "reason": "underground_mouth_requires_straight_approach_and_exit"}
        for i in range(first, last):
            if segments[i]["direction"] != direction or positions[i + 1] != (
                positions[i][0] + v[0],
                positions[i][1] + v[1],
            ):
                return {"ok": False, "reason": "underground_crossing_would_include_a_turn"}
        if positions[first] in crossable or positions[last] in crossable:
            return {"ok": False, "reason": "underground_mouth_would_replace_foreign_belt"}
        if any(
            surface[positions[i]]["direction"] % 8 == direction % 8
            for i in range(first + 1, last)
            if positions[i] in surface
        ):
            return {"ok": False, "reason": "foreign_parallel_belt_requires_explicit_junction"}
        for index, role in ((first, "input"), (last, "output")):
            p = positions[index]
            for other in underground:
                q = xy(other)
                aligned = (p[0] == q[0]) if direction % 8 == 0 else (p[1] == q[1])
                if other["direction"] == direction and aligned and abs(p[0] - q[0]) + abs(p[1] - q[1]) <= maximum_span:
                    return {"ok": False, "reason": "nearby_underground_endpoint_could_steal_pair"}
            mouths[index] = dict(entity("underground-belt", *p, direction), belt_to_ground_type=role)
        buried.update(range(first + 1, last))
        pair = {
            "input": list(positions[first]),
            "output": list(positions[last]),
            "direction": direction,
            "span": last - first,
        }
        preserved = [pipes[positions[i]] for i in range(first + 1, last) if positions[i] in pipes]
        if preserved:
            pair["preserved_pipes"] = preserved
        pairs.append(pair)
    for i, segment in enumerate(segments):
        if i not in buried:
            result.append(mouths.get(i, dict(segment, name="transport-belt")))
    # Reject accidental surface merges in either direction. Input mouths carry
    # forward underground, not onto the foreign crossing above their tunnel.
    own = {xy(e): e for e in result}
    for p, e in own.items():
        if e.get("belt_to_ground_type") == "input" or p == positions[-1]:
            continue
        vx, vy = VECTORS[e["direction"]]
        if (p[0] + vx, p[1] + vy) in surface:
            return {"ok": False, "reason": "route_would_feed_foreign_surface_belt"}
    for p, e in surface.items():
        vx, vy = VECTORS[e["direction"]]
        recipient = (p[0] + vx, p[1] + vy)
        if recipient == positions[0] and e["direction"] == segments[0]["direction"]:
            continue
        if recipient in own:
            return {"ok": False, "reason": "foreign_surface_belt_would_feed_route"}
    return _plan(
        result, [], underground_pairs=pairs, route_metrics=route["metrics"], layout_policy="pcb-layered-trunks-v1"
    )


def _prospective_mining_source(game, builder, obs, source, reserved):
    """Only an original, native-preflighted tagged core can declare a future port."""
    resource = source.get("mining_resource")
    actual = next(
        (e for e in obs.get("entities", []) if e["name"] == "transport-belt" and e["position"] == source["position"]),
        None,
    )
    if not resource or actual:
        return {"ok": True, "entities": []}
    matching = [e for e in reserved if e["name"] == "transport-belt" and e["position"] == source["position"]]
    if not matching:
        return {"ok": True, "entities": []}
    if (
        not obs.get("ok", True)
        or not isinstance(obs.get("world_id"), str)
        or not obs["world_id"]
        or source.get("kind") != "item"
        or source.get("direction") != "output"
    ):
        return {"ok": False, "reason": "invalid_prospective_mining_source_identity_or_port"}
    try:
        validate_annotations(reserved, builder)
    except (ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "reason": "invalid_prospective_mining_core", "detail": str(exc)}
    core = [e for e in reserved if e.get("mining_resource") == resource and e.get("mining_footprint")]
    sources = [e for e in matching if e in core and e.get("direction") == source.get("facing", 4)]
    nearby = [
        e
        for e in core
        if e["name"] in ("electric-mining-drill", "burner-mining-drill")
        and any(e["mining_footprint"] == p["mining_footprint"] for p in sources)
        and abs(e["position"]["x"] - source["position"]["x"]) <= 3
        and abs(e["position"]["y"] - source["position"]["y"]) <= 3
    ]
    if len(sources) != 1 or not nearby or len(core) > 96:
        return {"ok": False, "reason": "prospective_mining_source_requires_exact_core_belt_and_nearby_drill"}
    from .planning import preflight

    check = preflight(game, builder, obs, core)
    if not check.get("ok"):
        return {"ok": False, "reason": "prospective_mining_core_native_preflight_failed", "preflight": check}
    return {"ok": True, "entities": core}


def unbuilt_external_keepouts(builder, obs, blocks, *, released_block=None, crossable_belts=None):
    """Reserve prospective external hardware and tunnels until native assets exist.

    With a ``crossable_belts`` list, another block's planned surface ``transport-belt`` (outside
    its reserved area) is appended there instead of making its tile and neighbours hard: the
    router then crosses it with an underground pair, exactly as it crosses a built belt. Live map
    eval-1575138435 (seed 1575138435): the hand-fed power's planned coal feed ran north along
    x 54.5 for 20 tiles right in front of the coal bank's east port; as a 3-wide wall it refused
    every coal trunk to all 17 legal district sites until the feed was built.
    """
    actual = built_keys(obs)
    hard = set()
    for name, block in blocks.items():
        area = block.get("reserved_area")
        mining = block.get("resource") in RESOURCES | {"crude-oil"} and any(
            e["name"] in ("burner-mining-drill", "electric-mining-drill", "pumpjack") for e in block.get("entities", [])
        )
        if not area or mining or name == released_block:
            continue
        missing = [e for e in block.get("entities", []) if (e["name"], *xy(e)) not in actual]
        if crossable_belts is not None:
            outside = [
                e
                for e in missing
                if e["name"] == "transport-belt"
                and not (
                    area[0][0] <= e["position"]["x"] <= area[1][0] and area[0][1] <= e["position"]["y"] <= area[1][1]
                )
            ]
            crossable_belts += outside
            keys = {(e["name"], *xy(e)) for e in outside}
            missing = [e for e in missing if (e["name"], *xy(e)) not in keys]
        cells = builder._occupied_by_plan(missing)
        for e in missing:
            if e["name"] in ("transport-belt", "underground-belt", "pipe", "pipe-to-ground"):
                x, y = xy(e)
                cells.update((x + dx, y + dy) for dx, dy in VECTORS.values())
        for pair in block.get("underground_pairs", []) + block.get("fluid_underground_pairs", []):
            first = pair.get("input", pair.get("first"))
            last = pair.get("output", pair.get("last"))
            if first is None or last is None:
                continue
            mouths = [e for e in block.get("entities", []) if xy(e) in (tuple(first), tuple(last))]
            if mouths and all((e["name"], *xy(e)) in actual for e in mouths):
                continue
            dx, dy = last[0] - first[0], last[1] - first[1]
            if bool(dx) == bool(dy):
                raise ValueError("cardinal prospective underground pair required")
            span = abs(dx) + abs(dy)
            if type(span) not in (int, float) or not math.isfinite(span) or span % 1 or not 1 <= span <= 32:
                raise ValueError("bounded prospective underground pair required")
            cells.update((first[0] + dx * i / span, first[1] + dy * i / span) for i in range(int(span) + 1))
        hard.update(
            (x, y)
            for x, y in cells
            if not (x + 0.49 > area[0][0] and x - 0.49 < area[1][0] and y + 0.49 > area[0][1] and y - 0.49 < area[1][1])
        )
    return hard


def _sealed_escape(source, destination, hard) -> dict | None:
    """The port whose forced escape tile (one step along its facing) is in ``hard``, or None."""
    for role, port, sign in (("source", source, 1), ("destination", destination, -1)):
        facing = port.get("facing")
        if facing not in VECTORS:
            continue
        dx, dy = VECTORS[facing]
        cell = (port["position"]["x"] + sign * dx, port["position"]["y"] + sign * dy)
        if cell in hard:
            return {"port": role, "escape": {"x": cell[0], "y": cell[1]}}
    return None


def _production_keepouts(game, builder, obs, source, destination, reserved, bounds, *, released_unbuilt_receiver=None):
    """Whole producer reservations, with only their exact bounded port aprons."""
    empty = {"ok": True, "hard": set(), "source_prefix": [], "destination_suffix": [], "aprons": []}
    root = getattr(getattr(game, "cfg", None), "runtime_dir", None)
    if root is None:
        return (
            {"ok": False, "reason": "transport_unbuilt_receiver_release_identity_changed"}
            if released_unbuilt_receiver is not None
            else empty
        )
    path = Path(root) / "pcb-board.json"
    if not path.exists():
        return (
            {"ok": False, "reason": "transport_unbuilt_receiver_release_identity_changed"}
            if released_unbuilt_receiver is not None
            else empty
        )
    try:
        board = json.loads(path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "transport_production_board_world_changed"}
        blocks = []
        for name, block in board.get("blocks", {}).items():
            mining = block.get("resource") in RESOURCES | {"crude-oil"} and any(
                e.get("name") in ("burner-mining-drill", "electric-mining-drill", "pumpjack")
                for e in block.get("entities", [])
            )
            if block.get("reserved_area") and not mining:
                blocks.append(dict(block, reservation_name=name))
        bus = board.get("shared_main_bus", {})
        for row in bus.get("reservations", []):
            if row.get("area"):
                blocks.append(
                    {
                        "reserved_area": row["area"],
                        "entities": bus.get("entities", []),
                        "ports": [p for p in bus.get("ports", []) if p.get("item") == row.get("item")],
                        "reservation_name": "shared-main-bus:" + str(row.get("item")),
                    }
                )
        for block in blocks:
            area = block["reserved_area"]
            if (
                not isinstance(area, list)
                or len(area) != 2
                or any(not isinstance(p, list) or len(p) != 2 for p in area)
                or any(type(v) not in (int, float) or not math.isfinite(v) for p in area for v in p)
                or any(area[0][i] >= area[1][i] for i in (0, 1))
            ):
                raise ValueError("finite positive production reservation required")

        def inside(p, area):
            return (
                p["x"] + 0.49 > area[0][0]
                and p["x"] - 0.49 < area[1][0]
                and p["y"] + 0.49 > area[0][1]
                and p["y"] - 0.49 < area[1][1]
            )

        actual = {xy(e): e for e in obs.get("entities", []) if e["name"] == "transport-belt"}
        aprons = []
        source_prefix = []
        destination_suffix = []
        destination_owner = None
        for role, port in (("output", source), ("input", destination)):
            owners = [(i, b) for i, b in enumerate(blocks) if inside(port["position"], b["reserved_area"])]
            if not owners:
                continue
            if len(owners) != 1:
                return {"ok": False, "reason": "transport_port_has_overlapping_production_reservations"}
            index, owner = owners[0]
            if role == "input":
                destination_owner = owner["reservation_name"]
            matches = [
                p
                for p in owner.get("ports", [])
                if p.get("direction") == role
                and all(p.get(k) == port.get(k) for k in ("kind", "item", "position", "facing"))
            ]
            if (
                len(matches) != 1
                or port.get("kind") != "item"
                or port.get("direction") != role
                or port.get("facing") not in VECTORS
            ):
                return {
                    "ok": False,
                    "reason": "transport_port_inside_unrelated_production_reservation",
                    "reservation": owner["reservation_name"],
                }
            live = actual.get(xy(port))
            unit = live.get("unit_number") if live else None
            if (
                port.get("world_id", obs["world_id"]) != obs["world_id"]
                or (port.get("unit_number") is not None and port["unit_number"] != unit)
                or (role == "output" and (type(unit) is not int or unit < 1))
            ):
                return {"ok": False, "reason": "production_source_apron_requires_exact_owned_port"}
            if (
                role == "input"
                and not live
                and not any(
                    e["name"] == "transport-belt" and xy(e) == xy(port) and e.get("direction", 0) == port["facing"]
                    for e in reserved
                )
            ):
                return {"ok": False, "reason": "production_destination_apron_requires_owned_or_declared_input"}
            dx, dy = VECTORS[port["facing"]]
            if role == "input":
                dx, dy = -dx, -dy
            points = [dict(port["position"])]
            occupied = builder._occupied_by_plan(owner.get("entities", []))
            for step in range(1, 5):
                p = {"x": port["position"]["x"] + step * dx, "y": port["position"]["y"] + step * dy}
                same_input = role == "input" and any(
                    e["name"] == "transport-belt" and e["position"] == p and e.get("direction", 0) == port["facing"]
                    for e in owner.get("entities", [])
                )
                if (p["x"], p["y"]) in occupied and not same_input:
                    return {
                        "ok": False,
                        "reason": "production_port_apron_hits_producer_asset",
                        "reservation": owner["reservation_name"],
                    }
                if any(i != index and inside(p, b["reserved_area"]) for i, b in enumerate(blocks)):
                    return {"ok": False, "reason": "production_port_apron_hits_other_reservation"}
                if role == "output" and (p["x"], p["y"]) in actual:
                    return {"ok": False, "reason": "production_source_apron_not_empty"}
                points.append(p)
                if not inside(p, owner["reserved_area"]):
                    break
            else:
                return {"ok": False, "reason": "production_port_apron_exceeds_four_outward_cells"}
            units = {
                str(i + 1): actual[(p["x"], p["y"])]["unit_number"]
                for i, p in enumerate(points)
                if (p["x"], p["y"]) in actual
            }
            aprons.append(
                {"owner_index": index, "points": points, "facing": port["facing"], "units": units, "role": role}
            )
            if role == "output":
                source_prefix = points
            else:
                destination_suffix = list(reversed(points))
        released = None
        if released_unbuilt_receiver is not None:
            release = released_unbuilt_receiver
            if (
                not isinstance(release, dict)
                or set(release) != {"block", "plan_revision"}
                or release.get("block") != destination_owner
                or destination_owner not in board.get("blocks", {})
                or hashlib.sha256(
                    json.dumps(board["blocks"][destination_owner], sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                != release.get("plan_revision")
            ):
                return {"ok": False, "reason": "transport_unbuilt_receiver_release_identity_changed"}
            released = destination_owner
        hard = unbuilt_external_keepouts(builder, obs, board.get("blocks", {}), released_block=released)
        # Cells hard only because of other blocks' planned surface belts: a route may tunnel
        # under such a belt (``planned_belts``), its surface belts stay off the neighbours.
        planned_belts = []
        planned_soft = hard - unbuilt_external_keepouts(
            builder, obs, board.get("blocks", {}), released_block=released, crossable_belts=planned_belts
        )
        if xy(source) in hard or xy(destination) in hard:
            return {"ok": False, "reason": "transport_endpoint_conflicts_with_unbuilt_external_plan"}
        for index, block in enumerate(blocks):
            area = block["reserved_area"]
            allowed = {(p["x"], p["y"]) for apron in aprons if apron["owner_index"] == index for p in apron["points"]}
            for p in block.get("ports", []):
                if (
                    p.get("direction") not in ("input", "output")
                    or p.get("facing") not in VECTORS
                    or not isinstance(p.get("position"), dict)
                    or not inside(p["position"], area)
                ):
                    continue
                dx, dy = VECTORS[p["facing"]]
                if p["direction"] == "input":
                    dx, dy = -dx, -dy
                for step in range(1, 5):
                    outside = {"x": p["position"]["x"] + step * dx, "y": p["position"]["y"] + step * dy}
                    if not inside(outside, area):
                        cell = (outside["x"], outside["y"])
                        if cell not in allowed:
                            hard.add(cell)
                        break
            for ix in range(
                max(math.floor(bounds["min_x"]), math.floor(area[0][0]) - 1),
                min(math.ceil(bounds["max_x"]), math.ceil(area[1][0])) + 1,
            ):
                for iy in range(
                    max(math.floor(bounds["min_y"]), math.floor(area[0][1]) - 1),
                    min(math.ceil(bounds["max_y"]), math.ceil(area[1][1])) + 1,
                ):
                    p = {"x": ix + 0.5, "y": iy + 0.5}
                    if inside(p, area) and (ix + 0.5, iy + 0.5) not in allowed:
                        hard.add((ix + 0.5, iy + 0.5))
        return {
            "ok": True,
            "hard": hard,
            "source_prefix": source_prefix,
            "destination_suffix": destination_suffix,
            "aprons": aprons,
            "planned_belts": planned_belts,
            "planned_soft": planned_soft,
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "reason": "invalid_transport_production_reservation", "detail": str(exc)}


SITE_PLAN_FILE = "site-plan.json"


def bus_band(root, world_id):
    """``(x0, y0, x1, y1)`` tile rect of the site plan's bus for this world, or None.

    From the moment the master plan exists, the whole bus band belongs to the
    bus: routes that are not the bus's own (trunk, feed, tap) never enter it.
    The band is wider than any underground span, so no other route can cross
    it either; a consumer on the far side must tap the bus from its own side.
    """
    if root is None:
        return None
    path = Path(root) / SITE_PLAN_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if payload.get("world_id") != world_id:
            return None
        from .site.busframe import Bus

        rect = Bus.from_dict(payload["plan"]["bus"]).rect()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (rect.x0, rect.y0, rect.x1, rect.y1)


def _band_cells(band, bounds):
    """Tile centres of ``band`` inside the route search ``bounds``."""
    x0, y0, x1, y1 = band
    xs = range(max(x0, math.floor(bounds["min_x"])), min(x1, math.ceil(bounds["max_x"]) + 1))
    ys = range(max(y0, math.floor(bounds["min_y"])), min(y1, math.ceil(bounds["max_y"]) + 1))
    return {(x + 0.5, y + 0.5) for x in xs for y in ys}


def _in_band(band, position) -> bool:
    x0, y0, x1, y1 = band
    return x0 <= position["x"] < x1 and y0 <= position["y"] < y1


def _recipient(belt):
    """The tile a surface belt delivers onto."""
    vx, vy = VECTORS[belt["direction"]]
    x, y = xy(belt)
    return (x + vx, y + vy)


def guided_bounds(bounds, guide):
    """Widen the search box to cover ``guide`` = (x0, y0, x1, y1) tile rect.

    The box keeps its tile-centre alignment. Only the searched area grows:
    turn, detour, resource and survey-budget rules are unchanged.
    """
    if guide is None:
        return bounds
    x0, y0, x1, y1 = guide
    widened = dict(bounds)
    widened["min_x"] -= max(0, math.ceil(bounds["min_x"] - (x0 + 0.5)))
    widened["min_y"] -= max(0, math.ceil(bounds["min_y"] - (y0 + 0.5)))
    widened["max_x"] += max(0, math.ceil((x1 - 0.5) - bounds["max_x"]))
    widened["max_y"] += max(0, math.ceil((y1 - 0.5) - bounds["max_y"]))
    return widened


def plan_connection(
    game,
    builder,
    obs,
    source,
    destination,
    reserved=(),
    *,
    ignored_units=(),
    released_unbuilt_receiver=None,
    guide=None,
    bus_route=False,
    max_detour_ratio=None,
):
    """``_plan_connection``; a post-mortem dry run notes every route attempt (``evidence_capture``).

    Under the remedy option ``route_waypoints`` a cross-base route that finds no corridor is
    planned as two legs through a waypoint (:func:`waypoint_connection`)."""
    options = dict(
        ignored_units=ignored_units,
        released_unbuilt_receiver=released_unbuilt_receiver,
        guide=guide,
        bus_route=bus_route,
        max_detour_ratio=max_detour_ratio,
    )
    result = _plan_connection(game, builder, obs, source, destination, reserved, **options)
    if not result.get("ok") and result.get("reason") == "no_clean_trunk_corridor":
        if remedy.option("route_waypoints"):
            legs = waypoint_connection(game, builder, obs, source, destination, reserved, **options)
            result = legs if legs.get("ok") else dict(result, waypoints_tried=legs.get("tried", 0))
    if evidence_capture.active():
        evidence_capture.note(
            "route",
            item=source.get("item"),
            source=source.get("position"),
            destination=destination.get("position"),
            facing=[source.get("facing"), destination.get("facing")],
            guide=guide,
            bus_route=bus_route,
            reserved=len(reserved),
            outcome=evidence_capture.outcome(result),
        )
    return result


WAYPOINT_MIN_DISTANCE = 96  # routing.LONG_ROUTE_DISTANCE: shorter routes never take legs
WAYPOINT_LIMIT = 6  # waypoints tried per route (each costs one or two native surveys)
WAYPOINT_FRACTIONS = (0.5, 0.35, 0.65)  # along the dominant axis, from the source
WAYPOINT_SHIFTS = (6, -6, 0, 12, -12)  # lateral offsets from the source's, then the destination's line


def waypoints(source, destination) -> list[tuple[float, float, int]]:
    """Candidate ``(x, y, facing)`` waypoints of a long route, best first (pure).

    The route runs mostly along one axis; a waypoint lies on that axis at a fraction of the
    way, on a line parallel to it beside the source's or the destination's lateral line, and
    faces along it toward the destination. A leg through it is a short ordinary route each
    (eval-1575138435: 52.5,34.5 -> 67.5,-126.5 had no corridor; via 58.5,-60.5 both legs fit).
    """
    a, b = source["position"], destination["position"]
    dx, dy = b["x"] - a["x"], b["y"] - a["y"]
    if abs(dx) + abs(dy) < WAYPOINT_MIN_DISTANCE:
        return []
    vertical = abs(dy) >= abs(dx)
    facing = (8 if dy > 0 else 0) if vertical else (4 if dx > 0 else 12)
    found = []
    for fraction in WAYPOINT_FRACTIONS:
        along = (a["y"] + fraction * dy) if vertical else (a["x"] + fraction * dx)
        along = math.floor(along) + 0.5
        for base in (a["x"], b["x"]) if vertical else (a["y"], b["y"]):
            for shift in WAYPOINT_SHIFTS:
                lateral = base + shift
                point = (lateral, along, facing) if vertical else (along, lateral, facing)
                if point not in found:
                    found.append(point)
    return found


def waypoint_connection(game, builder, obs, source, destination, reserved=(), **options) -> dict:
    """``source`` -> waypoint -> ``destination`` as two routes joined straight, or a failure with ``tried``."""
    tried = 0
    for x, y, facing in waypoints(source, destination)[:WAYPOINT_LIMIT]:
        tried += 1
        vx, vy = VECTORS[facing]
        end = {**destination, "direction": "input", "position": {"x": x, "y": y}, "facing": facing}
        first = _plan_connection(game, builder, obs, source, end, reserved, **options)
        if not first.get("ok"):
            continue
        start = {**source, "direction": "output", "position": {"x": x + vx, "y": y + vy}, "facing": facing}
        start.pop("mining_resource", None)
        second = _plan_connection(
            game, builder, obs, start, destination, list(reserved) + list(first["entities"]), **options
        )
        if not second.get("ok"):
            continue
        a, b = first["route_metrics"], second["route_metrics"]
        span = abs(destination["position"]["x"] - source["position"]["x"]) + abs(
            destination["position"]["y"] - source["position"]["y"]
        )
        length = a["length"] + b["length"]
        clear = {(e["name"], *xy(e)): e for e in first.get("clear_obstacles", []) + second.get("clear_obstacles", [])}
        return dict(
            second,
            entities=list(first["entities"]) + list(second["entities"]),
            ports=[source, destination],
            required_items=_merge_counts(first.get("required_items"), second.get("required_items")),
            underground_pairs=list(first.get("underground_pairs", [])) + list(second.get("underground_pairs", [])),
            clear_obstacles=list(clear.values()),
            route_metrics={
                "length": length,
                "turns": a["turns"] + b["turns"],
                "manhattan_distance": span,
                "detour_ratio": length / span if span else 1.0,
                "waypoint": {"x": x, "y": y, "facing": facing},
            },
            waypoints_tried=tried,
        )
    return {"ok": False, "reason": "no_clean_trunk_corridor", "tried": tried}


def _merge_counts(*rows) -> dict:
    out = {}
    for row in rows:
        for name, count in (row or {}).items():
            out[name] = out.get(name, 0) + count
    return out


def _plan_connection(
    game,
    builder,
    obs,
    source,
    destination,
    reserved=(),
    *,
    ignored_units=(),
    released_unbuilt_receiver=None,
    guide=None,
    bus_route=False,
    max_detour_ratio=None,
):
    """Route one belt from ``source`` to ``destination`` port.

    ``guide`` is an optional reserved corridor bounding box (x0, y0, x1, y1)
    in tiles from the master plan; it only widens the search box.
    ``max_detour_ratio`` (default: the router's 1.35) lets a route follow a
    reserved corridor that itself detours (``smelting_site.corridor_detour``).
    ``bus_route`` marks the bus's own feed; every other route keeps out of
    the site plan's bus band (``bus_band``) and may not end inside it.
    """
    if source.get("item") != destination.get("item"):
        return {"ok": False, "reason": "port_item_mismatch"}
    band = None if bus_route else bus_band(getattr(getattr(game, "cfg", None), "runtime_dir", None), obs["world_id"])
    if band is not None and (_in_band(band, source["position"]) or _in_band(band, destination["position"])):
        return {"ok": False, "reason": "route_endpoint_inside_bus_band", "band": list(band)}
    mining_resource = source.get("mining_resource")
    if mining_resource is not None and (
        mining_resource not in RESOURCES or mining_resource != source.get("item") or source.get("facing") not in VECTORS
    ):
        return {"ok": False, "reason": "invalid_registered_mining_exit_source"}
    a, b = source["position"], destination["position"]
    ends = {(a["x"], a["y"]), (b["x"], b["y"])}
    bounds = {
        "min_x": min(a["x"], b["x"]) - 16,
        "max_x": max(a["x"], b["x"]) + 16,
        "min_y": min(a["y"], b["y"]) - 16,
        "max_y": max(a["y"], b["y"]) + 16,
    }
    bounds = guided_bounds(bounds, guide)
    if (bounds["max_x"] - bounds["min_x"] + 1) * (bounds["max_y"] - bounds["min_y"] + 1) > 40000:
        return {"ok": False, "reason": "belt_corridor_survey_budget_exceeded"}
    prospective = _prospective_mining_source(game, builder, obs, source, reserved)
    if not prospective.get("ok"):
        return prospective
    production = _production_keepouts(
        game, builder, obs, source, destination, reserved, bounds, released_unbuilt_receiver=released_unbuilt_receiver
    )
    if not production.get("ok"):
        return production
    sealed = _sealed_escape(source, destination, production["hard"])
    if sealed is not None:
        # The forced first/last tile lies in another block's unbuilt plan (or its
        # side-load margin): no corridor exists until that plan is built, then
        # its belts are ordinary crossable hardware. Say so; do not search.
        return {"ok": False, "reason": "route_port_escape_reserved_by_unbuilt_plan", **sealed}
    zones = mining_reservations(game, obs["world_id"])
    pipe_candidates = [
        {"name": "pipe", "position": e["position"], "unit_number": e["unit_number"]}
        for e in obs["entities"]
        if e["name"] == "pipe"
        and type(e.get("unit_number")) is int
        and e["unit_number"] > 0
        and e["unit_number"] not in ignored_units
        and bounds["min_x"] <= e["position"]["x"] <= bounds["max_x"]
        and bounds["min_y"] <= e["position"]["y"] <= bounds["max_y"]
    ]
    payload = json.dumps(
        json.dumps(
            {
                "bounds": bounds,
                "ignored": list(ignored_units),
                "source": source,
                "destination": destination,
                "zones": zones,
                "world_id": obs["world_id"],
                "pipe_candidates": pipe_candidates,
                "production_aprons": production["aprons"],
                "prospective_core": prospective["entities"],
                "exit_candidates": mining_exit_candidates(
                    a, facing=source["facing"] if source.get("facing") in VECTORS else 4
                ),
            },
            separators=(",", ":"),
        )
    )
    survey = game.query(
        """
local args=helpers.json_to_table("""
        + payload
        + """);local b=args.bounds;local blocked={};local clearable={};local crossable_pipes={}
if not d or d.world_id~=args.world_id then return {ok=false,reason="surface_pipe_crossing_world_changed"}end
local ignored={};for _,unit in ipairs(args.ignored) do ignored[unit]=true end
local function key(p)return p.x..":"..p.y end
local pipe_candidates={};for _,q in ipairs(args.pipe_candidates)do pipe_candidates[key(q.position)]=q end
for _,apron in ipairs(args.production_aprons)do for index,p in ipairs(apron.points)do
 local expected=apron.units[tostring(index)];local belt=target(p,"transport-belt")
 if expected then
  if not belt or belt.force~=f or belt.surface~=s or belt.unit_number~=expected or belt.direction~=apron.facing
   or belt.position.x~=p.x or belt.position.y~=p.y then return {ok=false,reason="production_port_apron_belt_identity_changed"}end
  for lane=1,2 do for _,item in pairs(belt.get_transport_line(lane).get_contents())do
   if item.name~=args.source.item then return {ok=false,reason="production_port_apron_belt_contaminated"}end
  end end
 elseif belt then return {ok=false,reason="production_port_apron_unexpected_belt"}end
end end
"""
        + PIPE_WITNESS_LUA
        + MINING_EXIT_LUA
        + """
local function patch_at(p)
 return mining_patch_at(p,args.source.mining_resource,args.zones)
end
local exit_allowed={};local exit_shared={};local exit_prefix={};local exit_length=-1;local exit_directions={}
local source_patch=patch_at(args.source.position)
if source_patch then
 if not args.source.mining_resource or not cardinal_facing(args.source.facing) then return {ok=false,reason="corridor_resource_source_requires_registered_exit"} end
 for _,spec in ipairs(args.prospective_core)do
  if spec.name=="electric-mining-drill" or spec.name=="burner-mining-drill" then
   local radius=prototypes.entity[spec.name].mining_drill_radius;local count=0
   for _,ore in pairs(s.find_entities_filtered{area={{spec.position.x-radius,spec.position.y-radius},{spec.position.x+radius,spec.position.y+radius}},type="resource"})do
    if ore.name~=args.source.mining_resource then return {ok=false,reason="prospective_mining_core_mixed_resource"}end
    count=count+ore.amount
   end
   if count<=0 then return {ok=false,reason="prospective_mining_core_has_no_resource"}end
  end
 end
 local chosen=select_mining_exit(args.source,args.zones,ignored,args.prospective_core,args.exit_candidates,nil,true)
 if not chosen.ok then return chosen end
 exit_prefix=chosen.exit_prefix;exit_length=chosen.exit_length
 for index,p in ipairs(exit_prefix) do
  exit_shared[key(p)]=true
  if index<#exit_prefix then
   exit_allowed[key(p)]=index-1;local q=exit_prefix[index+1]
   exit_directions[key(p)]=step_direction(p,q)
  end
 end
end
local destination_patch=patch_at(args.destination.position)
if destination_patch then return {ok=false,reason="corridor_destination_on_resource_or_mining_reservation"} end
for x=b.min_x,b.max_x do for y=b.min_y,b.max_y do
 local p={x=x,y=y};local old=target(p,"transport-belt")
 if old and ignored[old.unit_number] then old=nil end
 local entities=s.find_entities_filtered{area={{x-.4,y-.4},{x+.4,y+.4}}}
 local on_patch=false
 for _,e in pairs(entities) do if e.type=="resource" then on_patch=true;break end end
 if not on_patch then for _,zone in ipairs(args.zones) do local q=zone.area
  if x+.4>q[1][1] and x-.4<q[2][1] and y+.4>q[1][2] and y-.4<q[2][2] then on_patch=true;break end
 end end
 local candidate=pipe_candidates[key(p)];local pipe=candidate and target(p,"pipe")or nil
 local witness=not on_patch and pipe and surface_pipe_witness(pipe,candidate)or nil
 local legal=(old and old.force==f) or witness or s.can_place_entity{name="transport-belt",position=p,force=f,
  build_check_type=defines.build_check_type.manual_ghost,forced=true}
 if legal then for layer in pairs(prototypes.entity["transport-belt"].collision_mask.layers)do
  if s.get_tile(p).collides_with(layer)then legal=false;break end
 end end
 if not legal then
  local replacing=false;for _,e in pairs(entities) do if ignored[e.unit_number] then replacing=true end end
  if replacing then
   legal=true;for layer in pairs(prototypes.entity["transport-belt"].collision_mask.layers) do
    if s.get_tile(p).collides_with(layer) then legal=false;break end
   end
  end
 end
 local natural={}
 if on_patch and exit_allowed[key(p)]==nil then legal=false end
 if old and exit_shared[key(p)] then
  if exit_directions[key(p)] and old.direction~=exit_directions[key(p)] then return {ok=false,reason="mining_exit_existing_belt_direction_changed",position=p} end
  for lane=1,2 do for _,item in pairs(old.get_transport_line(lane).get_contents()) do
   if item.name~=args.source.item then return {ok=false,reason="mining_exit_existing_belt_contaminated",position=p} end
  end end
 end
 if legal then for _,e in pairs(entities) do
  if e~=a and e.type~="resource" and e~=old and not(witness and e==pipe)and not ignored[e.unit_number] and not e.prototype.collision_mask.colliding_with_tiles_only then
   local rock=e.type=="simple-entity" and (e.name=="big-rock" or e.name=="huge-rock" or e.name=="big-sand-rock")
   if e.force.name=="neutral" and e.minable and (e.type=="tree" or rock) then
    natural[#natural+1]={name=e.name,position=pos(e.position),unit_number=e.unit_number}
   else legal=false;break end
  end
 end end
 if not legal then blocked[#blocked+1]=p else
  if witness then crossable_pipes[#crossable_pipes+1]=witness end
  if #natural>0 then clearable[#clearable+1]={position=p,entities=natural}end
 end
end end
return {ok=true,blocked=blocked,clearable=clearable,max_span=prototypes.entity["underground-belt"].max_underground_distance,
 exit_prefix=exit_prefix,exit_length=exit_length,crossable_pipes=crossable_pipes}
"""
    )
    if not survey.get("ok"):
        return survey
    # Only native-bound existing pipes join the ordinary surface-belt layer.
    pipes = survey.get("crossable_pipes", [])
    if pipes == {}:
        pipes = []
    if not isinstance(pipes, list):
        return {"ok": False, "reason": "invalid_native_surface_pipe_crossings"}
    for pipe in pipes:
        if pipe.get("peers") == {}:
            pipe["peers"] = []
    all_entities = {
        (e["name"], *xy(e)): e for e in obs["entities"] + list(reserved) if e.get("unit_number") not in ignored_units
    }
    prefix = survey.get("exit_prefix", [])
    if prefix == {}:
        prefix = []
    if production["source_prefix"]:
        if prefix:
            return {"ok": False, "reason": "production_source_apron_conflicts_with_mining_exit"}
        prefix = production["source_prefix"]
    try:
        prefix_directions = (
            (
                [source["facing"]] * len(prefix)
                if production["source_prefix"]
                else exit_directions(prefix, source["facing"])
            )
            if prefix
            else []
        )
    except ValueError as exc:
        return {"ok": False, "reason": "invalid_native_mining_exit", "detail": str(exc)}
    prefix_headings = {(p["x"], p["y"]): d for p, d in zip(prefix, prefix_directions)}
    shared_headings = dict(prefix_headings)
    shared_headings.update({(p["x"], p["y"]): destination["facing"] for p in production["destination_suffix"]})
    foreign = [
        e
        for e in all_entities.values()
        if e["name"] in {"transport-belt", "underground-belt"}
        and xy(e) not in ends
        and not (e["name"] == "transport-belt" and e["direction"] == shared_headings.get(xy(e)))
    ]
    # A belt that already runs into the source port is that block's own
    # upstream (a bank's collector may turn into its port). It is fixed
    # hardware to route around, not a foreign belt merging into the route.
    feeders = {xy(e) for e in foreign if e["name"] == "transport-belt" and _recipient(e) == (a["x"], a["y"])}
    foreign = [e for e in foreign if xy(e) not in feeders]
    # Other blocks' planned surface belts are crossed like built ones (unbuilt_external_keepouts).
    known = {xy(e) for e in foreign}
    foreign += [e for e in production.get("planned_belts", []) if xy(e) not in known and xy(e) not in ends]
    foreign += pipes
    hard = {(p["x"], p["y"]) for p in survey["blocked"]}
    hard.update(feeders)
    soft = production.get("planned_soft", set())
    hard.update(production["hard"] - soft)
    soft_neighbours = soft - {xy(e) for e in production.get("planned_belts", [])} - ends
    hard.update(builder._occupied_by_plan([e for e in reserved if e["name"] != "transport-belt"]))
    if band is not None:
        hard.update(_band_cells(band, bounds))
    hard -= ends
    surface = {xy(e) for e in foreign if e["name"] in ("transport-belt", "pipe")}
    rejected = 0

    def accepts_layers(candidate):
        nonlocal rejected
        if prefix:
            segments = candidate.get("segments", [])
            if len(segments) < len(prefix) or any(
                xy(segments[i]) != (p["x"], p["y"])
                or (i < len(prefix) - 1 and segments[i]["direction"] != prefix_directions[i])
                for i, p in enumerate(prefix)
            ):
                return False
        suffix = production["destination_suffix"]
        if suffix:
            segments = candidate.get("segments", [])
            if len(segments) < len(suffix) or any(
                xy(e) != (p["x"], p["y"]) or e["direction"] != destination["facing"]
                for e, p in zip(segments[-len(suffix) :], suffix)
            ):
                return False
        layered = compile_crossings(candidate, foreign, maximum_span=survey["max_span"], verified_pipes=pipes)
        accepted = layered.get("ok", False)
        if accepted and prefix:
            by_position = {xy(e): e for e in layered["entities"]}
            accepted = all(by_position.get((p["x"], p["y"]), {}).get("name") == "transport-belt" for p in prefix[:-1])
        if accepted and production["aprons"]:
            by_position = {xy(e): e for e in layered["entities"]}
            accepted = all((p["x"], p["y"]) in by_position for apron in production["aprons"] for p in apron["points"])
        if accepted and soft_neighbours:
            # Beside another block's planned belt only underground mouths may stand (no incidental feed).
            accepted = not any(e["name"] == "transport-belt" and xy(e) in soft_neighbours for e in layered["entities"])
        if not accepted:
            rejected += 1
        return accepted

    distance = abs(a["x"] - b["x"]) + abs(a["y"] - b["y"])
    node_budget = max(150000, min(400000, int(distance + 1) * 2000))
    # Remedy ladder route relaxation (remedy.ROUTE_RELAX): longer fallback reach, more turns, more detour.
    relax = remedy.route_limits()
    long_turns = relax["long_route_turns"] or LONG_ROUTE_TURNS
    if relax["max_detour_ratio"] is not None:
        max_detour_ratio = max(max_detour_ratio or 1.35, relax["max_detour_ratio"])
    reach = {"long_route_distance": relax["long_route_distance"]} if relax["long_route_distance"] else {}
    for attempt in range(8):
        route = route_trunks(
            a,
            b,
            occupied=hard,
            bounds=bounds,
            start_direction=source["facing"],
            end_direction=destination["facing"],
            max_nodes=node_budget,
            candidate_validator=accepts_layers,
            long_route_turns=long_turns,
            **reach,
            **({"max_detour_ratio": max_detour_ratio} if max_detour_ratio is not None else {}),
        )
        compiled = compile_crossings(route, foreign, maximum_span=survey["max_span"], verified_pipes=pipes)
        if compiled.get("ok"):
            break
        if not route.get("ok"):
            return compiled
        conflicts = {xy(e) for e in route["segments"]} & surface - ends
        fresh = conflicts - hard
        if not fresh:
            return compiled
        # Reject these unsafe crossing cells, then choose another COMPLETE
        # corridor under the same original turn/detour gates. Never maze-route.
        hard.update(fresh)
    else:
        return {"ok": False, "reason": "bounded_layered_corridor_replanning_exhausted"}
    compiled["routing_attempts"] = attempt + 1
    if survey.get("exit_length", -1) >= 0:
        annotate_mining_exit(
            compiled["entities"], mining_resource, a, survey["exit_length"], path=prefix, facing=source["facing"]
        )
        compiled["mining_exit"] = {
            "resource": mining_resource,
            "source_position": dict(a),
            "last_step": survey["exit_length"],
        }
    compiled["layer_candidates_rejected"] = rejected
    compiled["ports"] = [source, destination]
    positions = {xy(e) for e in compiled["entities"]}
    allowed_tiles = set()
    obstacles = {}
    for row in survey.get("clearable", []):
        p = row["position"]["x"], row["position"]["y"]
        if p not in positions:
            continue
        allowed_tiles.add(p)
        for e in row["entities"]:
            obstacles[(e["name"], *xy(e))] = e
    # No cap: a planned corridor is cleared, never rerouted around nature (site.nature).
    compiled["clear_obstacles"] = list(obstacles.values())
    compiled["clearance_world_id"] = obs["world_id"]
    preflight = builder.can_place(compiled["entities"])
    # Prospective destination/source mouths are deliberately shared with blocks.
    failures = [
        e
        for e in preflight.get("blocked", [])
        if xy(e) not in allowed_tiles or e.get("reason") != "terrain_or_entity_collision"
    ]
    if not preflight.get("ok") and (not preflight.get("blocked") or failures):
        return {
            "ok": False,
            "reason": "native_corridor_preflight_failed",
            "blocked": failures,
            "native_reason": preflight.get("reason"),
        }
    return compiled


def surface_pipe_proof(game, obs, plan):
    """Fresh exact native pipe/reciprocal-peer guard before and after building.

    Returns a report with an explicit boolean ``ok``; an empty witness set
    succeeds without I/O. Callers retain witnesses in underground_pairs.
    """
    pipes = {}
    for pair in plan.get("underground_pairs", []):
        for pipe in pair.get("preserved_pipes", []):
            if not _valid_pipe_witness(pipe) or pipe["world_id"] != obs.get("world_id"):
                return {"ok": False, "reason": "preserved_surface_pipe_witness_invalid"}
            key = (pipe["unit_number"], *xy(pipe))
            if key in pipes and pipes[key] != pipe:
                return {"ok": False, "reason": "preserved_surface_pipe_witness_changed"}
            pipes[key] = pipe
    if not pipes:
        return {"ok": True, "preserved_pipes": 0}
    if not obs.get("ok", True):
        return {"ok": False, "reason": "preserved_surface_pipe_observation_invalid"}
    actual = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
    for q in pipes.values():
        if actual.get(("pipe", *xy(q)), {}).get("unit_number") != q["unit_number"]:
            return {"ok": False, "reason": "preserved_surface_pipe_missing_or_replaced", "pipe": q}
    payload = json.dumps(
        json.dumps({"world_id": obs["world_id"], "pipes": list(pipes.values())}, separators=(",", ":"))
    )
    return game.query(
        "local args=helpers.json_to_table("
        + payload
        + ");"
        + PIPE_WITNESS_LUA
        + """
if not d or d.world_id~=args.world_id then return {ok=false,reason="surface_pipe_crossing_world_changed"}end
for _,q in ipairs(args.pipes)do
 if q.force_index~=f.index then return {ok=false,reason="preserved_surface_pipe_force_changed"}end
 local fresh=surface_pipe_witness(target(q.position,"pipe"),q)
 if not fresh then return {ok=false,reason="preserved_surface_pipe_identity_or_reciprocity_changed",unit_number=q.unit_number}end
 local expected={};for _,p in ipairs(q.peers)do expected[p.unit_number..":"..p.fluidbox_index]=p end
 if #fresh.peers~=#q.peers then return {ok=false,reason="preserved_surface_pipe_peer_count_changed",unit_number=q.unit_number}end
 for _,peer in ipairs(fresh.peers)do local old=expected[peer.unit_number..":"..peer.fluidbox_index]
  if not old or old.name~=peer.name or old.position.x~=peer.position.x or old.position.y~=peer.position.y then
   return {ok=false,reason="preserved_surface_pipe_peer_identity_changed",unit_number=q.unit_number}end
 end
end;return {ok=true,preserved_pipes=#args.pipes}
"""
    )


def underground_proof(obs, plan):
    by_position = {xy(e): e for e in obs["entities"] if e["name"] == "underground-belt"}
    for pair in plan.get("underground_pairs", []):
        first = by_position.get(tuple(pair["input"]))
        last = by_position.get(tuple(pair["output"]))
        if not first or not last:
            return False
        if not first.get("underground_pair_verified") or not last.get("underground_pair_verified"):
            return False
        if first.get("underground_neighbour", {}).get("unit_number") != last["unit_number"]:
            return False
        if first.get("direction") != pair["direction"] or last.get("direction") != pair["direction"]:
            return False
    return True
