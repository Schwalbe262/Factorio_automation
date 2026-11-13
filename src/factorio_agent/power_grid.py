"""Resource-backed pole-hop plans, independent from belt corridor routing."""

from __future__ import annotations

import heapq
import json
from pathlib import Path
import math

from factorio_agent.core.deterministic_builder import _plan
from .board import entity
from .resource_policy import mining_reservations, validate_annotations


def _foreign_block_points(game, builder, goal, points):
    """Candidate pole points that belong to another registered block.

    Jobs run concurrently, so another block may be planned but not built yet.
    A power bridge that only checks the live world can then claim a tile that
    block needs: on a live map a mining bank's bridge pole was planned exactly
    where the science cell later put an inserter, and the bank blocked. Poles
    stay out of every other block's reserved area and off its planned
    hardware. The block whose reserved area holds the goal is the caller's own.
    Any problem reading the board yields no restriction.
    """
    try:
        path = Path(game.cfg.runtime_dir) / "pcb-board.json"
        blocks = json.loads(path.read_text(encoding="utf-8")).get("blocks", {})
    except (AttributeError, OSError, TypeError, ValueError):
        return set()
    foreign = set()
    for block in blocks.values():
        if not isinstance(block, dict):
            continue
        area = block.get("reserved_area")
        inside = None
        if isinstance(area, list) and len(area) == 2:
            (x0, y0), (x1, y1) = area

            def inside(point, x0=x0, y0=y0, x1=x1, y1=y1):
                return x0 <= point[0] <= x1 and y0 <= point[1] <= y1

            if inside(goal):
                continue
        try:
            occupied = builder._occupied_by_plan(block.get("entities", []))
        except (KeyError, TypeError, ValueError):
            occupied = set()
        for point in points:
            if point in occupied or (inside is not None and inside(point)):
                foreign.add(point)
    return foreign


def _inside_other_blocks(game, goal, positions):
    """Positions inside the reserved area of a registered block that does not hold ``goal``."""
    try:
        path = Path(game.cfg.runtime_dir) / "pcb-board.json"
        blocks = json.loads(path.read_text(encoding="utf-8")).get("blocks", {})
    except (AttributeError, OSError, TypeError, ValueError):
        return set()
    areas = []
    for block in blocks.values():
        area = block.get("reserved_area") if isinstance(block, dict) else None
        if isinstance(area, list) and len(area) == 2:
            (x0, y0), (x1, y1) = area
            if not (x0 <= goal[0] <= x1 and y0 <= goal[1] <= y1):
                areas.append((x0, y0, x1, y1))
    return {p for p in positions if any(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for x0, y0, x1, y1 in areas)}


# Extra hop cost of a pole inside a site-plan zone that is not the goal's (or the source's).
ZONE_POLE_COST = 2.0
# ... inside the smelting zone, the bus or a bus-side block strip (they are built densely later).
DENSE_ZONE_POLE_COST = 6.0
DENSE_ZONE_KINDS = ("smelting", "bus")


def _plan_zone_costs(game, world_id, goal):
    """``[(x0, y0, x1, y1, cost)]`` of site-plan zones and bus strips a bridge pole should keep out of.

    Fleet all-1113-2155 seed 852394612: the iron bank's bridge ran 23 poles straight across the
    planned smelting zone (a column at x 16.5, then a diagonal); 7 of the 8 district slots stood on
    a pole and every remaining site failed at the corridor stage. Poles may still cross a zone when
    no way around exists inside the search box, at an extra cost per pole. Zones holding ``goal``
    are the caller's own. Any problem reading the plan yields no costs.
    """
    try:
        path = Path(game.cfg.runtime_dir) / "site-plan.json"
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if payload.get("world_id") != world_id:
            return []
        plan = payload["plan"]
        rows = [(z.get("kind"), z["rect"]) for z in plan.get("zones", [])]
        rows += [("strip", s["rect"]) for s in plan.get("strips", [])]
    except (AttributeError, OSError, TypeError, ValueError, KeyError):
        return []
    costs = []
    for kind, rect in rows:
        x0, y0, x1, y1 = rect
        if x0 <= goal[0] <= x1 and y0 <= goal[1] <= y1:
            continue
        cost = DENSE_ZONE_POLE_COST if kind in DENSE_ZONE_KINDS or kind == "strip" else ZONE_POLE_COST
        costs.append((x0, y0, x1, y1, cost))
    return costs


def _zone_cost(costs, point) -> float:
    return sum(c for x0, y0, x1, y1, c in costs if x0 <= point[0] <= x1 and y0 <= point[1] <= y1)


def generating(e) -> bool:
    """A steam engine feeding its network now: buffered energy, or native status ``working``.

    A network loaded to the plant's limit drains the engines' buffers every tick, so ``energy``
    reads 0 while they generate at full output (eval-1575138435, 257 min: both engines
    ``working`` with steam, energy 0; every power bridge failed ``no_supplied_pole_network`` and
    steam expansion waited ``running_source_plant_required``, the very shortage it should fix).
    """
    return e.get("name") == "steam-engine" and (e.get("energy", 0) > 0 or e.get("status_name") == "working")


def supplied_networks(obs) -> set:
    """Electric network ids with a generating steam engine (:func:`generating`)."""
    return {e.get("electric_network_id") for e in obs.get("entities", []) if generating(e)}


def plan_connection(game, builder, obs, destination, reserved=()):
    networks = supplied_networks(obs)
    sources = [
        e for e in obs["entities"] if e["name"] == "small-electric-pole" and e.get("electric_network_id") in networks
    ]
    if not sources:
        return {"ok": False, "reason": "no_supplied_pole_network"}
    result = _connect(game, builder, obs, destination, reserved, sources, sources)
    if result.get("reason") != "no_bounded_pole_hop_connection":
        return result
    # The nearest supplied pole can sit deep inside another block (a smelting
    # district's furnace poles are more than one hop from its edge), and pole
    # points inside other blocks are not legal, so no hop leaves it. Search
    # once more from the nearest supplied pole outside every other block.
    goal = (destination["x"], destination["y"])
    inside = _inside_other_blocks(game, goal, {(e["position"]["x"], e["position"]["y"]) for e in sources})
    outside = [e for e in sources if (e["position"]["x"], e["position"]["y"]) not in inside]
    if not inside or not outside:
        return result
    return _connect(game, builder, obs, destination, reserved, sources, outside)


HOP_GRID_PITCHES = (4, 2, 1)  # candidate pole tiles every N tiles around the goal, coarse first


def _connect(game, builder, obs, destination, reserved, sources, anchors):
    """Pole hops from supplied ``sources`` to ``destination``; the search box spans the nearest of ``anchors``.

    Candidate tiles lie on a grid around the goal. A coarse grid can miss the
    few free tiles beside an ore patch (seed 4176033748: a coal bank 8 tiles
    from a powered pole had every 4-tile grid point on ore), so a finer grid
    is tried before giving up.
    """
    result = None
    for pitch in HOP_GRID_PITCHES:
        result = _connect_on_grid(game, builder, obs, destination, reserved, sources, anchors, pitch)
        if result.get("reason") not in ("no_bounded_pole_hop_connection", "power_connection_survey_budget_exceeded"):
            return result
    return result


def _connect_on_grid(game, builder, obs, destination, reserved, sources, anchors, pitch):
    goal = (destination["x"], destination["y"])
    mining_goal = None
    for spec in reserved:
        area = spec.get("mining_footprint")
        resource = spec.get("mining_resource")
        if (
            resource
            and area
            and area[0][0] <= goal[0] - 0.5
            and goal[0] + 0.5 <= area[1][0]
            and area[0][1] <= goal[1] - 0.5
            and goal[1] + 0.5 <= area[1][1]
        ):
            mining_goal = dict(entity("small-electric-pole", *goal), mining_resource=resource, mining_footprint=area)
            try:
                validate_annotations([mining_goal], builder)
            except ValueError:
                return {"ok": False, "reason": "invalid_mining_power_goal"}
            break
    zones = mining_reservations(game, obs.get("world_id", ""))
    closest = min(anchors, key=lambda e: math.dist(goal, (e["position"]["x"], e["position"]["y"])))
    start = closest["position"]["x"], closest["position"]["y"]
    bounds = (
        min(start[0], goal[0]) - 12,
        max(start[0], goal[0]) + 12,
        min(start[1], goal[1]) - 12,
        max(start[1], goal[1]) + 12,
    )
    points = {
        (goal[0] + x * pitch, goal[1] + y * pitch)
        for x in range(math.ceil((bounds[0] - goal[0]) / pitch), math.floor((bounds[1] - goal[0]) / pitch) + 1)
        for y in range(math.ceil((bounds[2] - goal[1]) / pitch), math.floor((bounds[3] - goal[1]) / pitch) + 1)
    }
    starts = {
        (e["position"]["x"], e["position"]["y"]): e
        for e in sources
        if bounds[0] <= e["position"]["x"] <= bounds[1] and bounds[2] <= e["position"]["y"] <= bounds[3]
    }
    points.update(starts)
    points.add(goal)
    if len(points) > 2500:
        return {"ok": False, "reason": "power_connection_survey_budget_exceeded"}
    ordered = sorted(points)
    payload = json.dumps(
        json.dumps(
            {
                "points": [{"x": x, "y": y} for x, y in ordered],
                "zones": zones,
                "goal": destination,
                "mining_goal": mining_goal,
            },
            separators=(",", ":"),
        )
    )
    result = game.query(
        """
local args=helpers.json_to_table("""
        + payload
        + """);local clear={}
for i,p in ipairs(args.points) do
 local old=target(p,"small-electric-pole")
 local legal=(old and old.force==f and old.position.x==p.x and old.position.y==p.y) or false
 if not legal and s.can_place_entity{name="small-electric-pole",position=p,force=f} then
  legal=true;local mining=args.mining_goal and p.x==args.goal.x and p.y==args.goal.y and args.mining_goal.mining_resource or nil
  for _,ore in pairs(s.find_entities_filtered{area={{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}},type="resource"}) do
   if not mining or ore.name~=mining then legal=false;break end
  end
  if legal then for _,zone in ipairs(args.zones) do local b=zone.area
   if p.x+.49>b[1][1] and p.x-.49<b[2][1] and p.y+.49>b[1][2] and p.y-.49<b[2][2]
    and (not mining or zone.resource~=mining) then legal=false;break end
  end end
 end
 if legal then clear[#clear+1]=i end
end
return {ok=true,clear=clear}
"""
    )
    if not result.get("ok"):
        return {"ok": False, "reason": result.get("reason", "power_survey_failed")}
    legal = {ordered[i - 1] for i in result.get("clear", [])}
    legal -= builder._occupied_by_plan(list(reserved))
    legal -= _foreign_block_points(game, builder, goal, legal) - set(starts)
    if goal not in legal:
        return {"ok": False, "reason": "power_target_footprint_obstructed"}
    zone_costs = _plan_zone_costs(game, obs.get("world_id"), goal)
    frontier = []
    costs = {}
    parents = {}
    for position in starts:
        if position in legal:
            costs[position] = 0
            heapq.heappush(frontier, (math.dist(position, goal) / 7, position))
    visited = 0
    while frontier and visited < 2500:
        _, node = heapq.heappop(frontier)
        visited += 1
        if node == goal:
            break
        for nxt in legal:
            distance = math.dist(node, nxt)
            if distance == 0 or distance > 7:
                continue
            cost = costs[node] + 1 + distance * 0.001 + (_zone_cost(zone_costs, nxt) if zone_costs else 0)
            if cost < costs.get(nxt, math.inf):
                costs[nxt] = cost
                parents[nxt] = node
                heapq.heappush(frontier, (cost + math.dist(nxt, goal) / 7, nxt))
    else:
        return {"ok": False, "reason": "no_bounded_pole_hop_connection"}
    path = [goal]
    while path[-1] not in starts:
        path.append(parents[path[-1]])
    path.reverse()
    entities = [
        dict(mining_goal) if mining_goal and (x, y) == goal else entity("small-electric-pole", x, y)
        for x, y in path[1:]
    ]
    return _plan(
        entities,
        [],
        template="power_grid_extension",
        source_unit_number=starts[path[0]].get("unit_number"),
        source_position={"x": path[0][0], "y": path[0][1]},
        destination=dict(destination),
        wire_hops=len(path) - 1,
        maximum_hop_tiles=7,
    )


def _key(e):
    return e["name"], e["position"]["x"], e["position"]["y"]


def replan_power_bridge(game, builder, obs, plan, *, power_port, bridge, reserved=(), avoid=()):
    """Re-plan the unbuilt part of a saved plan's power bridge (planned by :func:`plan_connection`).

    Rules:

    * ``bridge`` lists the plan's bridge poles; the caller tells them apart
      from its own hardware. Every bridge pole that is already built (an
      observed entity of the same name on the same position) stays in the
      plan, and so does every non-bridge entity.
    * Only unbuilt bridge poles are dropped; a fresh :func:`plan_connection`
      from the supplied network (built bridge poles that are energized are
      part of it) to ``power_port`` replaces them. ``reserved`` are the
      caller's planned non-pole entities the new chain must keep off.
    * No new pole may stand on a position in ``avoid`` (the obstruction).
    * Nothing is built or removed here and nothing is written; the caller
      re-runs its own validation, persists and republishes.

    Returns ``{"ok", "entities", "kept", "removed", "added", "bridge",
    "wire_hops", "source_position"}`` or ``{"ok": False, "reason"}``.
    """
    bridge_keys = {_key(e) for e in bridge}
    observed = {_key(e) for e in obs.get("entities", []) if isinstance(e.get("position"), dict)}
    kept = [e for e in bridge if _key(e) in observed]
    removed = [e for e in bridge if _key(e) not in observed]
    if not removed:
        return {"ok": False, "reason": "power_bridge_already_built"}
    connection = plan_connection(game, builder, obs, power_port, reserved)
    if not connection.get("ok"):
        return {"ok": False, "reason": connection.get("reason", "power_bridge_replan_failed")}
    blocked = {(p["x"], p["y"]) for p in avoid}
    if any((e["position"]["x"], e["position"]["y"]) in blocked for e in connection["entities"]):
        return {"ok": False, "reason": "power_bridge_replan_reuses_obstruction"}
    removed_keys = {_key(e) for e in removed}
    entities = [e for e in plan["entities"] if _key(e) not in removed_keys]
    present = {_key(e) for e in entities}
    added = [e for e in connection["entities"] if _key(e) not in present]
    return {
        "ok": True,
        "entities": entities + added,
        "kept": kept,
        "removed": removed,
        "added": added,
        "bridge": [e for e in entities if _key(e) in bridge_keys] + added,
        "wire_hops": connection["wire_hops"],
        "source_position": connection.get("source_position"),
    }
