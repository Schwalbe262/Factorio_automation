"""Declared mining flow/growth, bounded patch diagnostics and paid-bank planning.

No method builds, mines, saves runtime state or changes an accepted source port.
The prepare_new_bank signature is compatible with paired_mining's current caller.

Orientation conventions (map tiles, x east, y south; directions 0 north,
4 east, 8 south, 12 west):

* The canonical paired bank flows EAST (:func:`paired_mining.mining_pair`):
  the collector runs east, the "left" drill row sits north of it and the
  "right" row south, the output port is the east end and new columns are
  added at the west end (growth opposite the flow).
* Any other flow is that canonical layout turned clockwise about the output
  port with :class:`site.transform.Transform` (rotation only, never a
  mirror), so "left" stays the left lane of the turned collector and both
  lanes are still fed by their own row.
* A bank's orientation is carried by its ``layout_contract.policy.flow`` /
  ``flow_direction`` and the ``facing`` of its collector and output ports.
  Banks saved without a contract or orientation field are EAST.
* Only policies made with ``site_oriented=True`` (orientation decided by the
  site master plan, see :mod:`mining_site`) may execute a non-east flow;
  without a plan every new bank is EAST exactly as before.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import time

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.world_catalog import _yield
from .board import entity
from .planning import overlap, preflight
from .paired_mining import mining_pair, resource_survey
from .power_grid import plan_connection as power_connection
from .resource_policy import MINING_EXIT_LUA, annotate_mining_exit, mining_exit_candidates
from .site.transform import Transform, apply
from .smelting import merge_entities
from .actor_client import CHUNK_CHARTED_LUA


FLOWS = {"west_to_east": 4, "east_to_west": 12, "north_to_south": 8, "south_to_north": 0}
VECTORS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
DIRECTION_NAMES = {0: "north", 4: "east", 8: "south", 12: "west"}
RESOURCES = frozenset(("iron-ore", "copper-ore", "coal", "stone"))
PAIR_PITCH = 4
BANK_PITCH = 8
MAX_PAIRS = 8
MAX_TILES = 4096
MAX_CANDIDATES = 12
PLANNING_SECONDS = 20


def _payload(v):
    return json.dumps(json.dumps(v, separators=(",", ":")))


def _hash(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _xy(v):
    return v["position"]["x"], v["position"]["y"]


def _signature(e):
    return e["name"], *_xy(e), e.get("direction", 0), e.get("belt_to_ground_type")


def _position(p):
    return (
        isinstance(p, dict)
        and set(p) == {"x", "y"}
        and all(type(v) in (int, float) and math.isfinite(v) for v in p.values())
    )


def make_policy(
    world_id,
    catalog_fingerprint,
    resource,
    *,
    flow="west_to_east",
    grid_origin=None,
    minimum_growth_pairs=1,
    site_oriented=False,
):
    """Choose flow, opposite column growth and perpendicular new-bank growth first.

    ``site_oriented`` marks a flow decided by the site master plan; it adds
    ``orientation_source="site-plan"`` and allows any cardinal flow to
    execute. Without it the policy is byte-identical to earlier versions
    (only EAST executes), so saved contracts keep validating.
    """
    origin = deepcopy(grid_origin or {"x": 0.5, "y": 0.5})
    if (
        not isinstance(world_id, str)
        or not world_id
        or not isinstance(catalog_fingerprint, str)
        or not catalog_fingerprint
        or resource not in RESOURCES
        or flow not in FLOWS
        or not _position(origin)
        or any((v - 0.5) % 1 for v in origin.values())
        or type(minimum_growth_pairs) is not int
        or not 0 <= minimum_growth_pairs <= 2
    ):
        raise ValueError("bound world/catalog, solid resource, cardinal flow and half-tile fixed grid required")
    direction = FLOWS[flow]
    growth = (direction + 8) % 16
    field = (direction + 4) % 16
    value = dict(
        schema_version=1,
        world_id=world_id,
        catalog_fingerprint=catalog_fingerprint,
        resource=resource,
        flow=flow,
        flow_direction=direction,
        growth_direction=growth,
        growth=DIRECTION_NAMES[growth],
        field_growth_direction=field,
        field_growth=DIRECTION_NAMES[field],
        grid_origin=origin,
        pair_pitch=PAIR_PITCH,
        bank_pitch=BANK_PITCH,
        maximum_pair_slots=MAX_PAIRS,
        minimum_growth_pairs=minimum_growth_pairs,
        collector_lanes={"left": resource, "right": resource},
        main_bus_flow="south_to_north",
        orientation_execution_supported=direction == 4 or bool(site_oriented),
    )
    if site_oriented:
        value["orientation_source"] = "site-plan"
    value["policy_id"] = _hash(value)
    return value


def validate_policy(policy, world_id, catalog_fingerprint, resource):
    if not isinstance(policy, dict):
        raise ValueError("explicit mining layout policy required")
    expected = make_policy(
        world_id,
        catalog_fingerprint,
        resource,
        flow=policy.get("flow"),
        grid_origin=policy.get("grid_origin"),
        minimum_growth_pairs=policy.get("minimum_growth_pairs"),
        site_oriented=policy.get("orientation_source") == "site-plan",
    )
    if policy != expected:
        raise ValueError("mining policy frame, pitch, growth or immutable identity changed")


def flow_transform(flow, origin):
    """The clockwise turn mapping the canonical EAST layout to ``flow`` about ``origin`` (x, y).

    Rotation only: a mirror would swap the lane sides of the producer rows.
    """
    return Transform((FLOWS[flow] - 4) % 16, None, (origin[0], origin[1]))


def turned_bank(plan, flow, origin):
    """``plan`` (a canonical EAST bank or candidate plan) turned to ``flow`` about ``origin``."""
    t = flow_transform(flow, origin)
    return plan if t.identity else apply(t, plan)


def oriented_pair_core(output, pairs, resource, flow="west_to_east"):
    """The paired core whose collector ends at ``output`` and flows ``flow``.

    The canonical EAST core :func:`paired_mining.mining_pair` with its output
    port on ``output`` is turned about ``output`` (see :func:`flow_transform`);
    EAST is the canonical core unchanged.
    """
    if (
        not _position(output)
        or any((v - 0.5) % 1 for v in output.values())
        or resource not in RESOURCES
        or type(pairs) is not int
        or not 1 <= pairs <= MAX_PAIRS
        or flow not in FLOWS
    ):
        raise ValueError("half-tile output, 1..8 paired columns and cardinal flow required")
    base = turned_bank(mining_pair(output["x"] - 2, output["y"] - 2, pairs, resource), flow, (output["x"], output["y"]))
    entities = deepcopy(base["entities"])
    for e in entities:
        # Pole facing has no production effect; retain the canonical zero value.
        if e["name"] == "small-electric-pole":
            e["direction"] = 0
    mapping = {_signature(e): e for e in entities}
    rows = {side: [mapping[_signature(e)] for e in base["producer_rows"][side]] for side in ("left", "right")}
    port = deepcopy(base["ports"][0])
    port["position"] = dict(output)
    return _plan(
        entities,
        [port],
        resource=resource,
        resource_cell=True,
        paired=True,
        pair_count=pairs,
        drill_count=2 * pairs,
        producer_rows=rows,
        mining_footprint=deepcopy(base["mining_footprint"]),
        layout_policy="pcb-paired-mining-policy-v1",
        flow=flow,
        growth_direction=(FLOWS[flow] + 8) % 16,
    )


def capacity_budget(rate_per_drill, left_drills, right_drills, *, belt_speed, force_productivity_bonus=0):
    """Single-item, unstacked ore budgets; each actual collector lane is independent."""
    if (
        any(
            type(v) not in (int, float) or not math.isfinite(v)
            for v in (rate_per_drill, belt_speed, force_productivity_bonus)
        )
        or rate_per_drill <= 0
        or belt_speed <= 0
        or force_productivity_bonus < 0
        or any(type(n) is not int or n < 0 for n in (left_drills, right_drills))
    ):
        raise ValueError("finite native mining/belt rates and row counts required")
    per_lane = belt_speed * 60 * 4 * 60
    rate = rate_per_drill * (1 + force_productivity_bonus)
    offered = {"left": left_drills * rate, "right": right_drills * rate}
    delivered = {k: min(v, per_lane) for k, v in offered.items()}
    return dict(
        base_per_drill_per_minute=rate_per_drill,
        force_productivity_bonus=force_productivity_bonus,
        planned_per_drill_per_minute=rate,
        lane_capacity_per_minute=per_lane,
        belt_capacity_per_minute=2 * per_lane,
        offered_per_minute=offered,
        lane_limited_per_minute=delivered,
        source_nominal_per_minute=sum(offered.values()),
        collector_ceiling_per_minute=sum(delivered.values()),
        drills_per_lane_without_clipping=math.floor(per_lane / rate),
        drills_per_lane_to_saturate=math.ceil(per_lane / rate),
        required_independent_collectors=max(1, *(math.ceil(v / per_lane) for v in offered.values())),
        measured_rate_per_minute=None,
        scope="nameplate and lane ceiling; no production or consumer-throughput measurement",
    )


def layout_contract(bank, policy):
    """A serializable fixed-port contract; no changes to existing entity lists."""
    plan = bank["plan"]
    resource = plan["resource"]
    port = plan.get("collector_port", plan["ports"][0])
    pairs = plan["pair_count"]
    validate_policy(policy, policy["world_id"], policy["catalog_fingerprint"], resource)
    if resource != policy["resource"] or port["facing"] != policy["flow_direction"]:
        raise ValueError("paired bank flow contradicts chosen policy")
    slots = bank.get("reserved_pair_slots", pairs)
    if type(slots) is not int or not pairs <= slots <= MAX_PAIRS:
        raise ValueError("invalid finite reserved pair slots")
    future = oriented_pair_core(port["position"], slots, resource, policy["flow"])
    current = oriented_pair_core(port["position"], pairs, resource, policy["flow"])
    expected = {_signature(e) for e in current["entities"]}
    if not expected <= {_signature(e) for e in plan["entities"]}:
        raise ValueError("installed core is not the declared fixed four-tile grid")
    outlet = plan["ports"][0]
    ux, uy = VECTORS[outlet["facing"]]
    x, y = _xy(outlet)
    output_access = [{"x": x + i * ux, "y": y + i * uy} for i in range(1, 4)]
    fixed_poles = [e for e in current["entities"] if e["name"] == "small-electric-pole"]
    power = min(fixed_poles, key=lambda e: (math.dist(_xy(e), (x, y)), _xy(e)))
    contract = dict(
        schema_version=1,
        policy=deepcopy(policy),
        fixed_output_port=deepcopy(outlet),
        collector_port=deepcopy(port),
        fixed_power_port=deepcopy(power["position"]),
        pair_count=pairs,
        reserved_pair_slots=slots,
        growth_area=deepcopy(future["mining_footprint"]),
        growth_entities=[deepcopy(e) for e in future["entities"] if _signature(e) not in expected],
        output_access=output_access,
        output_access_scope="three reserved off-resource "
        + DIRECTION_NAMES[outlet["facing"]].upper()
        + " continuation cells",
        output_resource_free_verified=bool(bank.get("output_resource_free_verified")),
        service_side=DIRECTION_NAMES[(port["facing"] + 12) % 16],
        defense_rule="outside growth footprint, output approach and power/service access",
        extension_rule="add paired columns only opposite collector flow; retain every installed unit and both fixed ports",
    )
    contract["contract_id"] = _hash(contract)
    return contract


def validate_extension_contract(bank, new_plan):
    """Read-only guard for prepare_extension before publishing or spending stock."""
    old = bank["plan"]
    contract = bank.get("layout_contract") or old.get("layout_contract")
    if not contract:
        return {"ok": False, "reason": "mining_layout_contract_required_before_extension"}
    try:
        if contract.get("contract_id") != _hash({k: v for k, v in contract.items() if k != "contract_id"}):
            raise ValueError("contract revision changed")
        policy = contract["policy"]
        validate_policy(policy, policy["world_id"], policy["catalog_fingerprint"], old["resource"])
        if contract["fixed_output_port"] != old["ports"][0] or contract["collector_port"] != old.get(
            "collector_port", old["ports"][0]
        ):
            raise ValueError("old fixed interface changed")
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "reason": "mining_layout_contract_identity_changed", "detail": str(exc)}
    if (
        new_plan.get("resource") != old.get("resource")
        or new_plan.get("ports") != old.get("ports")
        or new_plan.get("collector_port", new_plan["ports"][0]) != old.get("collector_port", old["ports"][0])
    ):
        return {"ok": False, "reason": "mining_extension_changed_fixed_source_interface"}
    if not {_signature(e) for e in old["entities"]} <= {_signature(e) for e in new_plan["entities"]}:
        return {"ok": False, "reason": "mining_extension_removed_or_rotated_owned_asset"}
    new_by_signature = {_signature(e): e for e in new_plan["entities"]}
    if any(new_by_signature[_signature(e)] != e for e in old["entities"]):
        return {"ok": False, "reason": "mining_extension_changed_owned_scope_metadata"}
    pairs = new_plan.get("pair_count")
    if type(pairs) is not int or not old["pair_count"] <= pairs <= contract["reserved_pair_slots"]:
        return {"ok": False, "reason": "mining_extension_exceeds_declared_monotonic_growth"}
    expected = oriented_pair_core(
        contract["collector_port"]["position"], pairs, old["resource"], contract["policy"]["flow"]
    )
    if any(
        {_signature(e) for e in new_plan.get("producer_rows", {}).get(k, [])}
        != {_signature(e) for e in expected["producer_rows"][k]}
        for k in ("left", "right")
    ) or not {_signature(e) for e in expected["entities"]} <= {_signature(e) for e in new_plan["entities"]}:
        return {"ok": False, "reason": "mining_extension_left_the_declared_grid"}
    wanted_drills = {_signature(e) for e in expected["entities"] if e["name"] == "electric-mining-drill"}
    if {_signature(e) for e in new_plan["entities"] if e["name"] == "electric-mining-drill"} != wanted_drills:
        return {"ok": False, "reason": "mining_extension_added_undeclared_producer"}
    added = {_signature(e) for e in new_plan["entities"]} - {_signature(e) for e in old["entities"]}
    if not added <= {_signature(e) for e in expected["entities"]}:
        return {"ok": False, "reason": "mining_extension_added_undeclared_geometry"}
    return {"ok": True, "policy_id": contract["policy"]["policy_id"], "native_identity_and_lane_recheck_required": True}


PATCH_SURVEY_LUA = (
    CHUNK_CHARTED_LUA
    + r"""
if not d or d.world_id~=x.world_id or s.name~="nauvis"then return {ok=false,reason="mining_patch_world_or_surface_changed"}end
local drill=prototypes.entity["electric-mining-drill"];local belt=prototypes.entity["transport-belt"]
local resource=prototypes.entity[x.resource];local tiles={};local truncated=false;local charted=0
for cx=math.floor(x.area[1][1]/32),math.floor((x.area[2][1]-.0001)/32)do
 for cy=math.floor(x.area[1][2]/32),math.floor((x.area[2][2]-.0001)/32)do
  if chunk_charted(cx,cy)then charted=charted+1
   local area={{math.max(x.area[1][1],cx*32),math.max(x.area[1][2],cy*32)},
               {math.min(x.area[2][1]-.0001,(cx+1)*32-.0001),math.min(x.area[2][2]-.0001,(cy+1)*32-.0001)}}
   local remaining=x.maximum_tiles-#tiles
   local found=s.find_entities_filtered{area=area,type="resource",limit=remaining+1}
   if #found>remaining then truncated=true;break end
   for _,e in ipairs(found)do tiles[#tiles+1]={name=e.name,position=e.position,amount=e.amount,unit_number=e.unit_number,
    world_id=d.world_id,surface_index=s.index}end
  end
 end;if truncated then break end
end
return {ok=true,tick=game.tick,world_id=d.world_id,surface_index=s.index,area=x.area,tiles=tiles,charted_chunks=charted,truncated=truncated,
 native={mining_speed=drill.mining_speed,mining_radius=drill.mining_drill_radius,mining_time=resource.mineable_properties.mining_time,
 belt_speed=belt.belt_speed,force_productivity_bonus=f.mining_drill_productivity_bonus}}
"""
)


def survey_patch(game, obs, catalog, resource, area, *, maximum_tiles=MAX_TILES):
    if (
        resource not in RESOURCES
        or not isinstance(area, list)
        or len(area) != 2
        or any(not isinstance(q, list) or len(q) != 2 for q in area)
        or any(type(v) not in (int, float) or not math.isfinite(v) for q in area for v in q)
        or any(not 0 < area[1][i] - area[0][i] <= 128 for i in (0, 1))
        or type(maximum_tiles) is not int
        or not 1 <= maximum_tiles <= MAX_TILES
        or not obs.get("world_id")
    ):
        raise ValueError("explicit bounded patch area up to128x128 and 1..4096 tiles required")
    args = dict(world_id=obs["world_id"], resource=resource, area=area, maximum_tiles=maximum_tiles)
    result = game.query("local x=helpers.json_to_table(" + _payload(args) + ");" + PATCH_SURVEY_LUA)
    if not result.get("ok"):
        return result
    if (
        result.get("world_id") != obs["world_id"]
        or not isinstance(result.get("tiles"), list)
        or len(result["tiles"]) > maximum_tiles
        or any(
            t.get("world_id") != obs["world_id"]
            or t.get("surface_index") != result.get("surface_index")
            or not _position(t.get("position"))
            or type(t.get("amount")) not in (int, float)
            or t["amount"] < 0
            for t in result["tiles"]
        )
    ):
        return {"ok": False, "reason": "mining_patch_native_receipt_changed"}
    native = result.get("native", {})
    if (
        any(
            type(native.get(k)) not in (int, float) or not math.isfinite(native[k]) or native[k] <= 0
            for k in ("mining_speed", "mining_radius", "mining_time", "belt_speed")
        )
        or type(native.get("force_productivity_bonus")) not in (int, float)
        or not math.isfinite(native["force_productivity_bonus"])
        or native["force_productivity_bonus"] < 0
    ):
        return {"ok": False, "reason": "mining_patch_native_rates_invalid"}
    if result.get("truncated"):
        return {"ok": False, "reason": "mining_patch_tile_budget_exceeded", "maximum_tiles": maximum_tiles}
    result["catalog_fingerprint"] = catalog.fingerprint
    result["resource"] = resource
    return result


MAX_EXIT_CELLS = 12


def _canonical_inputs(snapshot, policy, reservations):
    """Snapshot tiles, survey area and reservations in the canonical EAST frame.

    The frame is the policy flow's turn about the grid origin, inverted; the
    half-tile grid maps onto itself. Returns ``(transform, tiles, area,
    reservations)``; for EAST the inputs are returned unchanged.
    """
    origin = policy["grid_origin"]
    t = flow_transform(policy["flow"], (origin["x"], origin["y"]))
    if t.identity:
        return t, snapshot["tiles"], snapshot["area"], list(reservations)
    back = t.inverse()
    tiles = []
    for tile in snapshot["tiles"]:
        x, y = back.point(*_xy(tile))
        tiles.append(dict(tile, position={"x": x, "y": y}))

    def box(a):
        x0, y0, x1, y1 = back.rect(a[0][0], a[0][1], a[1][0], a[1][1])
        return [[x0, y0], [x1, y1]]

    return t, tiles, box(snapshot["area"]), [box(a) for a in reservations]


def _exit_cells(tiles, port):
    """Cells from the canonical (EAST) output port to the first resource-free cell.

    0 means the port itself is off the ore. Only 1..:data:`MAX_EXIT_CELLS`
    can be served by the bounded native exit (it starts on the bank's ore).
    """
    x, y = _xy(port)
    for step in range(MAX_EXIT_CELLS + 2):
        if (x + step, y) not in tiles:
            return step
    return MAX_EXIT_CELLS + 2


def _inside(area, rect):
    """True when ``area`` [[x0, y0], [x1, y1]] lies inside ``rect`` [x0, y0, x1, y1]."""
    return rect[0] <= area[0][0] and rect[1] <= area[0][1] and area[1][0] <= rect[2] and area[1][1] <= rect[3]


def candidate_sites(snapshot, policy, pairs, reservations, *, maximum=MAX_CANDIDATES, site_exit=None):
    """Pure coherent grid; score unique ore and contiguous future columns once.

    Sites are searched in the canonical EAST frame of the policy flow and
    returned on the map. The score, compared as a tuple (bigger is better):

    * without ``site_exit``: (extra growth columns, ore tiles of the poorer
      drill row, unique ore tiles, ore amount (capped), closeness to the map
      origin) - unchanged from earlier versions;
    * with ``site_exit`` (see :mod:`mining_site`): (extra growth columns, ore
      tiles of the poorer drill row, -exit cost, unique ore tiles, ore
      amount (capped)), where exit cost = on-ore exit cells + Manhattan
      distance from the first off-ore cell to ``site_exit["corridor_start"]``.
      Sites whose exit cannot leave the ore within the bounded native exit,
      or whose growth footprint leaves the plan's mining zone, are dropped.
    """
    if type(pairs) is not int or not 1 <= pairs <= MAX_PAIRS or type(maximum) is not int or not 1 <= maximum <= 12:
        raise ValueError("1..8 pairs and1..12 candidates required")
    validate_policy(policy, snapshot["world_id"], snapshot["catalog_fingerprint"], snapshot["resource"])
    if not policy["orientation_execution_supported"]:
        raise ValueError("non-EAST new-bank execution requires native exit and lane-proof integration")
    frame, canonical_tiles, bounds, reservations = _canonical_inputs(snapshot, policy, reservations)
    origin = policy["grid_origin"]
    resource = policy["resource"]
    native = snapshot["native"]
    radius = native["mining_radius"]
    tiles = {_xy(tile): tile for tile in canonical_tiles}
    zone = None
    if site_exit and site_exit.get("zone_rect"):
        zone = list(frame.inverse().rect(*site_exit["zone_rect"]))
    anchors = set()
    for t in canonical_tiles:
        if t["name"] != resource:
            continue
        x, y = _xy(t)
        x = origin["x"] + PAIR_PITCH * math.floor((x - origin["x"]) / PAIR_PITCH)
        y = origin["y"] + BANK_PITCH * math.floor((y - origin["y"]) / BANK_PITCH)
        anchors.update((x + dx, y + dy) for dx in (-4, 0, 4) for dy in (-8, 0, 8))

    def exit_cost(core):
        """On-ore exit cells plus the distance to the corridor start, or None if unreachable."""
        port = core["ports"][0]
        cells = _exit_cells(tiles, port)
        # The native exit starts on the bank's own ore and must leave it in bounds.
        if not 1 <= cells <= MAX_EXIT_CELLS:
            return None
        x, y = frame.point(port["position"]["x"] + cells, port["position"]["y"])
        tx, ty = site_exit["corridor_start"]
        return cells + abs(x - tx) + abs(y - ty)

    def covered(plan):
        seen = {}
        counts = {"left": 0, "right": 0}
        for side in ("left", "right"):
            for e in plan["producer_rows"][side]:
                x, y = _xy(e)
                if not (
                    bounds[0][0] <= x - radius
                    and x + radius < bounds[1][0]
                    and bounds[0][1] <= y - radius
                    and y + radius < bounds[1][1]
                ):
                    return None
                cells = [t for (xx, yy), t in tiles.items() if abs(xx - x) <= radius and abs(yy - y) <= radius]
                if (
                    any(t["name"] != resource for t in cells)
                    or len(cells) < 4
                    or sum(t["amount"] for t in cells) < 1800
                ):
                    return None
                counts[side] += len(cells)
                seen.update({_xy(t): t["amount"] for t in cells})
        return sum(seen.values()), len(seen), counts

    ranked = []
    for x, y in sorted(anchors):
        core = mining_pair(x, y, pairs, resource)
        base = covered(core)
        if not base:
            continue
        slots = pairs
        future = core
        for n in range(pairs, min(MAX_PAIRS, pairs + 2) + 1):
            q = mining_pair(x, y, n, resource)
            if any(overlap(q["mining_footprint"], a) for a in reservations) or not covered(q):
                break
            slots = n
            future = q
        if slots < pairs + policy["minimum_growth_pairs"] or any(
            overlap(core["mining_footprint"], a) for a in reservations
        ):
            continue
        row_tiles = min(base[2].values())
        amount = min(base[0], 100000)
        if site_exit is None:
            score = (slots - pairs, row_tiles, base[1], amount, -x * x - y * y)
        else:
            cost = exit_cost(core)
            if cost is None or (zone is not None and not _inside(future["mining_footprint"], zone)):
                continue
            score = (slots - pairs, row_tiles, -cost, base[1], amount)
        ranked.append(
            dict(
                x=x,
                y=y,
                plan=core,
                future_plan=future,
                reserved_pair_slots=slots,
                unique_resource_amount=base[0],
                unique_resource_tiles=base[1],
                row_resource_tiles=base[2],
                score=score,
            )
        )
    ranked.sort(key=lambda q: (q["score"], q["x"], q["y"]), reverse=True)
    if frame.identity:
        return ranked[:maximum]
    flow = policy["flow"]
    result = []
    for q in ranked[:maximum]:
        x, y = frame.point(q["x"], q["y"])
        result.append(
            dict(
                q,
                x=x,
                y=y,
                plan=turned_bank(q["plan"], flow, (origin["x"], origin["y"])),
                future_plan=turned_bank(q["future_plan"], flow, (origin["x"], origin["y"])),
            )
        )
    return result


def plan_offore_exit(game, builder, obs, core, future, reservations):
    """Use only the shared bounded local escape, then a fixed clear outlet.

    The exit leaves in the collector port's ``facing`` (EAST for the
    canonical layout); the outlet and its three-cell access apron continue
    in that facing.
    """
    source = core["ports"][0]
    facing = source.get("facing", 4)
    if facing not in VECTORS:
        return {"ok": False, "reason": "invalid_registered_mining_exit_source"}
    ux, uy = VECTORS[facing]
    zones = [{"area": a, "resource": "__foreign_reservation__"} for a in reservations]
    zones.append({"area": future["mining_footprint"], "resource": core["resource"]})
    args = dict(
        world_id=obs["world_id"],
        source=source,
        zones=zones,
        planned=future["entities"],
        exit_candidates=mining_exit_candidates(source["position"], facing=facing),
        vector={"x": ux, "y": uy},
    )
    result = game.query(
        "local x=helpers.json_to_table("
        + _payload(args)
        + ");"
        + MINING_EXIT_LUA
        + r"""
if not d or d.world_id~=x.world_id then return {ok=false,reason="mining_exit_world_changed"}end
local function clear_apron(p)
 for step=0,3 do local q={x=p.x+step*x.vector.x,y=p.y+step*x.vector.y};local occupied=mining_patch_at(q,x.source.item,x.zones)
  if occupied then return false end
  for _,spec in ipairs(x.planned)do local size=spec.name=="electric-mining-drill"and 3 or 1
   if math.abs(spec.position.x-q.x)<size/2+.49 and math.abs(spec.position.y-q.y)<size/2+.49 then return false end
  end
  for layer in pairs(prototypes.entity["transport-belt"].collision_mask.layers)do if s.get_tile(q).collides_with(layer)then return false end end
  for _,e in pairs(s.find_entities_filtered{area={{q.x-.49,q.y-.49},{q.x+.49,q.y+.49}}})do
   local rock=e.type=="simple-entity"and(e.name=="big-rock"or e.name=="huge-rock"or e.name=="big-sand-rock")
   local nature=e.force.name=="neutral"and e.minable and(e.type=="tree"or rock)
   if e~=a and e.type~="corpse"and not nature and not e.prototype.collision_mask.colliding_with_tiles_only then return false end
  end
 end;return true end
return select_mining_exit(x.source,x.zones,{},x.planned,x.exit_candidates,clear_apron,true)
"""
    )
    if not result.get("ok"):
        return result
    path = result.get("exit_prefix")
    length = result.get("exit_length")
    if (
        not isinstance(path, list)
        or len(path) < 2
        or type(length) is not int
        or not 0 <= length <= 11
        or len(path) != length + 2
        or path[0] != source["position"]
        or any(not _position(p) for p in path)
    ):
        return {"ok": False, "reason": "mining_exit_native_receipt_changed"}
    belts = []
    for i, p in enumerate(path):
        dx, dy = (path[i + 1]["x"] - p["x"], path[i + 1]["y"] - p["y"]) if i + 1 < len(path) else (ux, uy)
        direction = next((k for k, v in VECTORS.items() if v == (dx, dy)), None)
        if direction is None:
            return {"ok": False, "reason": "mining_exit_native_receipt_changed"}
        belts.append(entity("transport-belt", p["x"], p["y"], direction))
    # Merge before tagging so the existing source retains its exact core scope.
    es = merge_entities(belts, core["entities"])
    annotate_mining_exit(es, core["resource"], source["position"], length, path=path, facing=facing)
    outlet = dict(core["ports"][0], position=deepcopy(path[-1]))
    outlet.pop("mining_resource", None)
    apron = [entity("transport-belt", path[-1]["x"] + i * ux, path[-1]["y"] + i * uy, facing) for i in range(1, 4)]
    check = preflight(game, builder, obs, merge_entities(future["entities"], es, apron))
    if not check.get("ok"):
        return check
    return dict(
        ok=True,
        entities=es,
        port=outlet,
        collector_port=deepcopy(source),
        path=path,
        output_access=apron,
        resource_free_verified=True,
        exit_length=length,
    )


class _BudgetExceeded(RuntimeError):
    pass


class _PlanningGame:
    def __init__(self, game):
        self.game = game
        self.deadline = time.monotonic() + PLANNING_SECONDS
        self.queries = 0

    def __getattr__(self, name):
        return getattr(self.game, name)

    def query(self, body):
        if time.monotonic() >= self.deadline or self.queries >= 64:
            raise _BudgetExceeded("bounded mining planning budget exhausted")
        self.queries += 1
        return self.game.query(body)


def validate_retained_units(obs, prepared):
    actual = {(e["name"], *_xy(e)): e for e in obs.get("entities", [])}
    for old in prepared.get("retained_units", []):
        live = actual.get((old["name"], *_xy(old)))
        if not live or any(live.get(k) != old.get(k) for k in ("unit_number", "direction", "belt_to_ground_type")):
            return {"ok": False, "reason": "mining_retained_native_identity_changed", "entity": old}
    return {"ok": True}


def prepare_extension(game, builder, obs, bank, added_drills):
    """Add grid columns behind the collector, preserving an existing exit verbatim."""
    try:
        if type(added_drills) is not int or added_drills < 1 or added_drills > 16:
            raise ValueError("1..16 additional drills required")
        old = bank["plan"]
        pairs = old["pair_count"] + math.ceil(added_drills / 2)
        contract = bank.get("layout_contract") or old.get("layout_contract")
        if not contract:
            return {"ok": False, "reason": "mining_layout_contract_required_before_extension"}
        validate_policy(contract["policy"], obs["world_id"], builder.catalog.fingerprint, old["resource"])
        if pairs > contract["reserved_pair_slots"]:
            return {"ok": False, "reason": "paired_mining_reserved_columns_exhausted"}
        if not contract["policy"]["orientation_execution_supported"]:
            return {"ok": False, "reason": "mining_orientation_requires_native_exit_and_lane_proof_integration"}
        collector = old.get("collector_port", old["ports"][0])
        # The declared grid in the contract's flow (EAST: the canonical core).
        core = oriented_pair_core(collector["position"], pairs, old["resource"], contract["policy"]["flow"])
        es = merge_entities(core["entities"], old["entities"])
        extra = {
            k: v
            for k, v in old.items()
            if k not in ("ok", "reason", "entities", "ports", "required_items", "clear_obstacles")
        }
        extra.update(producer_rows=core["producer_rows"], pair_count=pairs, drill_count=2 * pairs)
        plan = _plan(es, deepcopy(old["ports"]), **extra)
        guard = validate_extension_contract(bank, plan)
        if not guard.get("ok"):
            return guard
        from .depleted_drill_retirement import effective_mining_plan

        effective_old = effective_mining_plan(game, obs, old, catalog_fingerprint=builder.catalog.fingerprint)
        effective = effective_mining_plan(game, obs, plan, catalog_fingerprint=builder.catalog.fingerprint)
        actual = {(e["name"], *_xy(e)): e for e in obs.get("entities", [])}
        retained = []
        for spec in effective_old["entities"]:
            live = actual.get((spec["name"], *_xy(spec)))
            if (
                not live
                or type(live.get("unit_number")) is not int
                or live["unit_number"] < 1
                or any(live.get(k) != spec.get(k) for k in ("direction", "belt_to_ground_type"))
            ):
                return {"ok": False, "reason": "mining_old_layout_not_installed_exactly", "entity": spec}
            retained.append(
                {
                    k: deepcopy(live[k])
                    for k in ("name", "position", "unit_number", "direction", "belt_to_ground_type")
                    if k in live
                }
            )
        check = preflight(game, builder, obs, effective["entities"])
        if not check.get("ok"):
            return check
        old_keys = {_signature(q) for q in old["entities"]}
        # Existing low-resource/retired grid positions remain historical. Only
        # newly accepted drills must satisfy the new-bank richness threshold.
        fresh = [q for q in plan["entities"] if q["name"] == "electric-mining-drill" and _signature(q) not in old_keys]
        resources = resource_survey(game, effective, required_drills=fresh)
        if not resources.get("ok"):
            return resources
        native = game.query(
            'return {ok=true,belt_speed=prototypes.entity["transport-belt"].belt_speed,force_productivity_bonus=f.mining_drill_productivity_bonus}'
        )
        if not native.get("ok"):
            return native
        product = builder.catalog.entities[old["resource"]]["mining_products"]
        if len(product) != 1 or product[0]["name"] != old["resource"]:
            raise ValueError("one deterministic mining product required")
        profile = capacity_budget(
            resources["rate_per_drill"] * _yield(product[0]),
            len(effective["producer_rows"]["left"]),
            len(effective["producer_rows"]["right"]),
            belt_speed=native["belt_speed"],
            force_productivity_bonus=native["force_productivity_bonus"],
        )
        plan = _plan(es, old["ports"], **extra, clear_obstacles=check["clear_obstacles"])
        result = deepcopy(bank)
        result.update(
            ok=True,
            plan=plan,
            ready=False,
            retained_units=retained,
            capacity_profile=profile,
            nominal_rate_per_minute=profile["collector_ceiling_per_minute"],
            remaining_resource_survey=resources["unique_remaining_resource"],
        )
        return result
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        return {"ok": False, "reason": "invalid_mining_extension_contract", "detail": str(exc)}


def prepare_new_bank(
    game, builder, obs, resource, count, reservations, *, policy=None, site_exit=None, registered=(), planned=None
):
    """Drop-in current paired-bank planner with policy decided before entities/power.

    ``site_exit`` (from :func:`mining_site.site_exit`) turns the bank so its
    collector flows toward the plan zone's output side and ranks sites by
    exit cost (see :func:`candidate_sites`); the record is stored as
    ``plan["site_exit"]``. Without it every bank is the historical EAST one.

    ``registered`` (``[(block name, reserved area)]``) and ``planned``
    (``board.planned_unbuilt_hardware`` tiles of other plans) make a candidate
    fail, and the next one be tried, when its reserved area (growth, exit and
    output access) meets another block's reserved area or any of its hardware
    meets another plan's unbuilt tile. Before, such a bank was saved and its
    board publish blocked (``paired_mining_reserved_area_conflicts_with_registered_block``,
    seeds 2005199778 and 1590659659: the coal bank's exit and output access lay
    on the reserved power coal feed).
    """
    from .board import hardware_conflicts

    catalog = builder.catalog
    try:
        if resource not in RESOURCES or type(count) is not int or not 1 <= count <= 2 * MAX_PAIRS:
            raise ValueError("solid resource and1..16 electric drills required")
        if site_exit is not None and (site_exit.get("resource") != resource or site_exit.get("flow") not in FLOWS):
            raise ValueError("site exit belongs to another resource or has no cardinal flow")
        if policy is not None:
            selected = deepcopy(policy)
        elif site_exit is not None:
            selected = make_policy(
                obs["world_id"], catalog.fingerprint, resource, flow=site_exit["flow"], site_oriented=True
            )
        else:
            selected = make_policy(obs["world_id"], catalog.fingerprint, resource)
        validate_policy(selected, obs["world_id"], catalog.fingerprint, resource)
        if not selected["orientation_execution_supported"]:
            return {
                "ok": False,
                "reason": "mining_orientation_requires_native_exit_and_lane_proof_integration",
                "policy": selected,
            }
        known = obs.get("resources", {}).get(resource, {}).get("position")
        if not _position(known):
            return {"ok": False, "reason": "known_resource_patch_seed_required"}
        area = [[math.floor(known[k]) - 48 for k in ("x", "y")], [math.floor(known[k]) + 48 for k in ("x", "y")]]
        planning = _PlanningGame(game)
        snapshot = survey_patch(planning, obs, catalog, resource, area)
        if not snapshot.get("ok"):
            return snapshot
        sites = candidate_sites(snapshot, selected, math.ceil(count / 2), reservations, site_exit=site_exit)
        failures = []
        product = catalog.entities[resource]["mining_products"]
        if len(product) != 1 or product[0]["name"] != resource:
            raise ValueError("one deterministic same-item mining product required")
        base = snapshot["native"]["mining_speed"] * 60 / snapshot["native"]["mining_time"] * _yield(product[0])
        for candidate in sites:
            core = candidate["plan"]
            future = candidate["future_plan"]
            pairs = core["pair_count"]
            check = preflight(planning, builder, obs, future["entities"])
            if not check.get("ok"):
                failures.append(check.get("reason"))
                continue
            exit_plan = plan_offore_exit(planning, builder, obs, core, future, reservations)
            if not exit_plan.get("ok"):
                failures.append(exit_plan.get("reason"))
                continue
            core = dict(
                core,
                entities=exit_plan["entities"],
                ports=[exit_plan["port"]],
                collector_port=exit_plan["collector_port"],
                collector_connection=[
                    e
                    for e in exit_plan["entities"]
                    if _signature(e) not in {_signature(v) for v in candidate["plan"]["entities"]}
                ],
            )
            bank = dict(
                plan=core, reserved_pair_slots=candidate["reserved_pair_slots"], output_resource_free_verified=True
            )
            contract = layout_contract(bank, selected)
            # Attach power on the fixed outlet edge and forbid future drills/belts.
            reserve = [e for e in future["entities"] if e["name"] != "small-electric-pole"]
            reserve = merge_entities(reserve, [e for e in core["entities"] if e["name"] == "transport-belt"])
            facing = exit_plan["port"]["facing"]
            reserve += [entity("transport-belt", p["x"], p["y"], facing) for p in contract["output_access"]]
            bridge = power_connection(planning, builder, obs, contract["fixed_power_port"], reserve)
            if not bridge.get("ok"):
                failures.append(bridge.get("reason"))
                continue
            es = merge_entities(core["entities"], bridge["entities"])
            future_with_power = merge_entities(
                future["entities"], core["entities"], bridge["entities"], exit_plan["output_access"]
            )
            full = preflight(planning, builder, obs, future_with_power)
            if not full.get("ok"):
                failures.append(full.get("reason"))
                continue
            clash = hardware_conflicts(builder, future_with_power, planned) if planned else []
            if clash:
                failures.append("bank_meets_planned_hardware_of:" + str(clash[0]["block"]))
                continue
            live_resources = resource_survey(planning, core)
            if not live_resources.get("ok"):
                failures.append(live_resources.get("reason"))
                continue
            extra = {k: v for k, v in core.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
            # Clearance belongs to current construction only; reserve future trees without mining them early.
            actual = preflight(planning, builder, obs, es)
            if not actual.get("ok"):
                failures.append(actual.get("reason"))
                continue
            profile = capacity_budget(
                base,
                pairs,
                pairs,
                belt_speed=snapshot["native"]["belt_speed"],
                force_productivity_bonus=snapshot["native"]["force_productivity_bonus"],
            )
            plan = _plan(
                es,
                core["ports"],
                **extra,
                clear_obstacles=actual["clear_obstacles"],
                clearance_world_id=obs["world_id"],
            )
            area = deepcopy(future["mining_footprint"])
            for p in exit_plan["path"] + [e["position"] for e in exit_plan["output_access"]]:
                for axis, k in enumerate(("x", "y")):
                    area[0][axis] = min(area[0][axis], p[k] - 0.5)
                    area[1][axis] = max(area[1][axis], p[k] + 0.5)
            met = [name for name, other in registered if overlap(area, other)]
            if met:
                failures.append("bank_reserved_area_meets_registered_block:" + str(met[0]))
                continue
            plan.update(
                mining_reserved_area=deepcopy(future["mining_footprint"]),
                layout_contract=deepcopy(contract),
                flow_contract={
                    "material_flow": selected["flow"],
                    "column_growth": selected["growth"],
                    "field_growth": selected["field_growth"],
                },
            )
            if site_exit is not None:
                plan["site_exit"] = dict(deepcopy(site_exit), exit_position=deepcopy(exit_plan["port"]["position"]))
            return dict(
                ok=True,
                world_id=obs["world_id"],
                catalog_fingerprint=catalog.fingerprint,
                plan=plan,
                ready=False,
                reserved_pair_slots=candidate["reserved_pair_slots"],
                reserved_area=area,
                mining_reserved_area=future["mining_footprint"],
                nominal_rate_per_minute=profile["collector_ceiling_per_minute"],
                remaining_resource_survey=live_resources["unique_remaining_resource"],
                wire_hops=bridge["wire_hops"],
                layout_contract=contract,
                capacity_profile=profile,
                patch_diagnostic=dict(
                    tick=snapshot["tick"],
                    unique_resource_tiles=candidate["unique_resource_tiles"],
                    unique_resource_amount=candidate["unique_resource_amount"],
                    flow=selected["flow"],
                    growth=selected["growth"],
                    candidate_count=len(sites),
                    native_queries=planning.queries,
                    **({"site_score": list(candidate["score"])} if site_exit is not None else {}),
                ),
            )
        return {
            "ok": False,
            "reason": "no_policy_compliant_paired_bank_with_growth_and_power",
            "candidate_count": len(sites),
            "candidate_failures": failures[-8:],
        }
    except _BudgetExceeded as exc:
        return {"ok": False, "reason": "mining_layout_planning_budget_exceeded", "detail": str(exc)}
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        return {"ok": False, "reason": "invalid_mining_layout_policy_or_catalog", "detail": str(exc)}


CAPACITY_DIAGNOSTIC_LUA = r"""
if not d or d.world_id~=x.world_id then return {ok=false,reason="mining_capacity_world_changed"}end
local rows={};local drill=prototypes.entity["electric-mining-drill"];local ore=prototypes.entity[x.resource]
local radius=drill.mining_drill_radius;local facing=x.facing or 4;local normal=x.normal or {x=0,y=-1}
local collector=x.collector or {x=0,y=x.collector_y}
for _,q in ipairs(x.drills)do local e=target(q.position,q.name);local row={side=q.side,position=q.position,present=false}
 if e then
  if e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.direction~=q.direction
   or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="mining_capacity_native_drill_identity_changed"}end
  row.present=true;row.unit_number=e.unit_number;row.powered=e.is_connected_to_electric_network()and e.energy>0
  row.normal_quality=e.quality.name=="normal";row.speed_bonus=e.speed_bonus;row.productivity_bonus=e.productivity_bonus
  row.working=e.status==defines.entity_status.working;local amount=0
  for _,v in pairs(s.find_entities_filtered{area={{q.position.x-radius,q.position.y-radius},{q.position.x+radius,q.position.y+radius}},type="resource"})do
   if v.name~=x.resource then return {ok=false,reason="mining_capacity_mixed_resource"}end;amount=amount+v.amount
  end
  row.remaining_resource=amount;local p=e.drop_position;local offset=(p.x-collector.x)*normal.x+(p.y-collector.y)*normal.y
  row.drop_lane_verified=math.abs(offset)<.49 and(q.side=="left"and offset>0 or q.side=="right"and offset<0)
  local cell={x=math.floor(p.x)+.5,y=collector.y}
  if normal.x~=0 then cell={x=collector.x,y=math.floor(p.y)+.5}end
  local receiver=target(cell,"transport-belt")
  row.drop_lane_verified=row.drop_lane_verified and receiver and receiver.force==f and receiver.surface==s and receiver.direction==facing or false
 end
 rows[#rows+1]=row
end
return {ok=true,tick=game.tick,drills=rows,mining_speed=drill.mining_speed,mining_time=ore.mineable_properties.mining_time,
 belt_speed=prototypes.entity["transport-belt"].belt_speed,force_productivity_bonus=f.mining_drill_productivity_bonus}
"""


def diagnose_bank_capacity(game, obs, catalog, bank):
    """Installed and resource-backed powered potential; deliberately no timed rate."""
    try:
        if not obs.get("ok", True) or not obs.get("world_id"):
            raise ValueError("fresh bound observation required")
        from .depleted_drill_retirement import effective_mining_plan

        historical = bank["plan"]
        plan = effective_mining_plan(game, obs, historical, catalog_fingerprint=catalog.fingerprint)
        resource = plan["resource"]
        rows = plan.get("producer_rows")
        if rows is None:
            rows = {"left": [], "right": []}
            for q in plan["entities"]:
                if q["name"] == "electric-mining-drill":
                    if q["direction"] not in (0, 8):
                        raise ValueError("unsupported single-row native mining lane")
                    rows["left" if q["direction"] == 8 else "right"].append(q)
        if resource not in RESOURCES:
            raise ValueError("typed solid mining bank required")
        if not 0 <= sum(len(rows[k]) for k in ("left", "right")) <= 64:
            raise ValueError("bounded0..64active drill diagnostic required")
        contract = bank.get("layout_contract") or historical.get("layout_contract")
        if contract:
            validate_policy(contract["policy"], obs["world_id"], catalog.fingerprint, resource)
            valid = validate_extension_contract(bank, historical)
            if not valid.get("ok"):
                return valid
        collector = plan.get("collector_port", plan["ports"][0])
        facing = collector.get("facing", 4)
        if facing not in VECTORS:
            raise ValueError("native cardinal physical lane proof required")
        ux, uy = VECTORS[facing]
        actual = {(e["name"], *_xy(e)): e for e in obs.get("entities", [])}
        specs = []
        for side in ("left", "right"):
            for spec in rows[side]:
                live = actual.get((spec["name"], *_xy(spec)))
                if live and (type(live.get("unit_number")) is not int or live["unit_number"] < 1):
                    raise ValueError("exact native unit required")
                specs.append(dict(spec, side=side, unit_number=live and live["unit_number"]))
        products = catalog.entities[resource]["mining_products"]
        if len(products) != 1 or products[0]["name"] != resource:
            raise ValueError("one deterministic mining product required")
        yield_ = _yield(products[0])
        args = dict(
            world_id=obs["world_id"],
            resource=resource,
            drills=specs,
            collector_y=collector["position"]["y"],
            collector=collector["position"],
            facing=facing,
            # "left" of the collector flow (north of an EAST belt).
            normal={"x": uy, "y": -ux},
        )
        native = game.query("local x=helpers.json_to_table(" + _payload(args) + ");" + CAPACITY_DIAGNOSTIC_LUA)
        if not native.get("ok"):
            return native
        if len(native.get("drills", [])) != len(specs) or any(
            r.get("side") not in ("left", "right") for r in native["drills"]
        ):
            return {"ok": False, "reason": "mining_capacity_native_receipt_changed"}
        base = native["mining_speed"] * 60 / native["mining_time"] * yield_
        planned = capacity_budget(
            base,
            len(rows["left"]),
            len(rows["right"]),
            belt_speed=native["belt_speed"],
            force_productivity_bonus=native["force_productivity_bonus"],
        )
        installed = {"left": 0.0, "right": 0.0}
        active = dict(installed)
        counts = {"planned": len(specs), "installed": 0, "resource_backed_powered": 0, "working": 0}
        missing = []
        inactive = []
        unsupported = []
        for r in native["drills"]:
            if not r.get("present"):
                missing.append(r["position"])
                continue
            counts["installed"] += 1
            speed, productivity = r.get("speed_bonus"), r.get("productivity_bonus")
            if (
                not r.get("normal_quality")
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in (speed, productivity))
                or speed < 0
                or productivity < 0
            ):
                unsupported.append(r["unit_number"])
                continue
            rate = base * (1 + speed) * (1 + productivity)
            installed[r["side"]] += rate
            if r.get("powered") and r.get("remaining_resource", 0) > 0 and r.get("drop_lane_verified"):
                active[r["side"]] += rate
                counts["resource_backed_powered"] += 1
            else:
                inactive.append(r["unit_number"])
            counts["working"] += bool(r.get("working"))
        ceiling = planned["lane_capacity_per_minute"]
        return dict(
            ok=True,
            tick=native["tick"],
            resource=resource,
            counts=counts,
            planned=planned,
            installed_offered_per_minute=installed,
            installed_collector_ceiling_per_minute=sum(min(v, ceiling) for v in installed.values()),
            active_offered_per_minute=active,
            active_collector_ceiling_per_minute=sum(min(v, ceiling) for v in active.values()),
            missing_drill_positions=missing,
            inactive_drill_units=inactive,
            unsupported_drill_units=unsupported,
            physical_lane_receipt=native["drills"],
            all_installed_drop_lanes_verified=not missing
            and all(r.get("drop_lane_verified") for r in native["drills"]),
            nominal_profile_complete=not missing and not unsupported,
            measured_rate_per_minute=None,
            historical_planned_drill_count=sum(q["name"] == "electric-mining-drill" for q in historical["entities"]),
            paid_retired_drill_units=plan.get("retired_drill_units", []),
            scope="native snapshot identity and powered resource-backed potential; output packets, downstream proof and timed throughput remain separate",
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        return {"ok": False, "reason": "invalid_mining_capacity_diagnostic", "detail": str(exc)}
