"""Additive electric mining banks with aligned collectors and typed output ports."""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from . import remedy
from .board import entity
from .power_grid import plan_connection, replan_power_bridge
from .transport import VECTORS, xy, plan_connection as belt_connection, underground_proof
from .planning import overlap, preflight
from .resource_policy import mark_mining_core, mining_reservations
from .mining_site import (
    alternative_exits,
    guide_tuple,
    other_patches,
    patch_exits,
    plan_zone_areas,
    planned_patch,
    seed_in_zone,
    site_exit,
)
from .mining_layout_policy import SURVEY_EMPTY
from .mining_border import block_rects, neighbour_reservations


RESOURCES = {"iron-ore", "copper-ore", "coal", "stone"}
# The builder's reason when a planned entity cannot be placed (deterministic_builder.ensure_plan).
OBSTRUCTED = "reserved block placement is obstructed"
IN_RESERVATION = "new_entity_over_resource_or_mining_reservation"
# A bank re-plans its power bridge around new obstructions at most this often.
MAX_BRIDGE_REPLANS = 2
# Other plans' unbuilt tiles a new bank avoids lie within this many tiles of
# the patch seed (the bank survey spans 48 tiles each way, plus exit and bridge).
PLANNED_TILE_REACH = 72
# An unbuilt bank whose reservation meets a registered block is planned again this often.
MAX_BANK_REPLANS = 2
# A bank whose plan patch has no site tries this many of the plan's other patches (best first).
MAX_OTHER_PATCHES = 2
MAX_OTHER_PATCH_DISTANCE = 200  # tiles from the plan start: farther patches are outposts, not primaries
NO_COMPLIANT_BANK = "no_policy_compliant_paired_bank_with_growth_and_power"


def _key(e):
    return e["name"], e["position"]["x"], e["position"]["y"]


def bridge_poles(bank):
    """The bank's power bridge: planned small poles that are not bank hardware.

    Every pole of the bank itself (the paired core grid and its growth) is
    annotated with ``mining_resource`` by ``mark_mining_core``.
    :func:`power_grid.plan_connection` adds plain poles; only its goal, the
    contract's fixed power port, may carry the annotation and that pole is
    bank hardware. So a bridge pole is a small electric pole without
    ``mining_resource`` that is not on the fixed power port. A bank without a
    layout contract has no recognised bridge.
    """
    contract = bank.get("layout_contract") or bank["plan"].get("layout_contract")
    if not contract:
        return []
    port = contract.get("fixed_power_port")
    return [
        e
        for e in bank["plan"]["entities"]
        if e["name"] == "small-electric-pole" and "mining_resource" not in e and e["position"] != port
    ]


def _reseeded(obs, resource, centroid):
    """``obs`` with the patch seed of ``resource`` moved to ``centroid`` (the survey is centred on it)."""
    x, y = centroid
    row = dict((obs.get("resources") or {}).get(resource) or {}, position={"x": x, "y": y})
    return dict(obs, resources=dict(obs.get("resources") or {}, **{resource: row}))


def _survey_reason(result) -> bool:
    """True when ``result`` failed on the patch survey itself (every side reads the same area)."""
    return result.get("reason") in (
        SURVEY_EMPTY,
        "mining_patch_native_receipt_changed",
        "mining_patch_tile_budget_exceeded",
    )


def _attempt(patch, side, result) -> dict:
    """One compact evidence row of a bank planning attempt."""
    row = {"patch": patch or "seed", "side": side, "reason": result.get("reason") if not result.get("ok") else "ok"}
    if not result.get("ok"):
        row["candidates"] = result.get("candidate_count", 0)
        failures = [f for f in result.get("candidate_failures") or [] if f]
        if failures:
            row["failures"] = failures[-3:]
        if result.get("attempts"):
            row["relaxations"] = [a.get("relaxed") or "planned" for a in result["attempts"]]
    return row


def _needs(trail) -> str:
    """What would have to change for a bank to fit, derived from the attempts (a retry condition).

    * ``patch_survey``: no attempt could read the patch (uncharted or empty around the seed);
    * ``another_patch_or_smaller_row``: no side of any tried patch had a single anchor whose drill
      areas hold only this ore with an off-ore exit, even relaxed (patch geometry);
    * ``registered_block_moved:<name>``: candidates met another block's reservation;
    * ``planned_hardware_moved:<name>``: candidates met another plan's unbuilt hardware;
    * ``power_bridge``: candidates fit but no pole chain reached them;
    * ``exit_path``: candidates fit but no bounded exit left the ore;
    * ``site_preflight``: candidates fit but the live preflight refused them (hardware, water).
    """
    rows = [r for r in trail if isinstance(r, dict) and "reason" in r]
    if rows and all(r["reason"] in (SURVEY_EMPTY, "mining_patch_native_receipt_changed") for r in rows):
        return "patch_survey"
    failures = [f for r in rows for f in r.get("failures", [])]
    for prefix, label in (
        ("bank_reserved_area_meets_registered_block:", "registered_block_moved:"),
        ("bank_meets_planned_hardware_of:", "planned_hardware_moved:"),
    ):
        hit = next((f for f in failures if f.startswith(prefix)), None)
        if hit:
            return label + hit[len(prefix) :]
    if any("pole" in f or "power" in f for f in failures):
        return "power_bridge"
    if any("exit" in f for f in failures):
        return "exit_path"
    if failures:
        return "site_preflight"
    return "another_patch_or_smaller_row"


def _with_evidence(failure, trail):
    """``failure`` with the attempt trail (last 10) and ``needs``."""
    if not trail:
        return failure
    return dict(failure, attempts=trail[-10:], needs=_needs(trail))


def validate_parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"resource", "rate_per_minute"}:
        raise ValueError("mining_outpost requires resource and rate_per_minute")
    if not isinstance(parameters["resource"], str) or parameters["resource"] not in RESOURCES:
        raise ValueError("unsupported solid resource")
    rate = parameters["rate_per_minute"]
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 960:
        raise ValueError("mining rate must be finite in (0,960]")


def mining_bank(x, y, count, resource):
    es = [entity("electric-mining-drill", x + 4 * i, y, 8) for i in range(count)]
    es += [entity("transport-belt", x + dx, y + 2, 4) for dx in range(-1, 4 * (count - 1) + 3)]
    es += [entity("small-electric-pole", x + 4 * i + 2, y) for i in range(count)]
    footprint = mark_mining_core(es, resource)
    port = {
        "kind": "item",
        "item": resource,
        "direction": "output",
        "facing": 4,
        "position": {"x": x + 4 * (count - 1) + 2, "y": y + 2},
        "mining_resource": resource,
    }
    return _plan(
        es,
        [port],
        template="electric_mining_bank",
        resource_cell=True,
        resource=resource,
        drill_count=count,
        mining_footprint=footprint,
        layout_policy="pcb-mining-row-v1",
    )


class MiningOutposts:
    """Electric mining banks per resource, grown to a requested nominal rate.

    ``site`` (a :class:`site.service.SiteService`, optional) supplies the
    master plan: a new primary bank then flows toward its plan zone's output
    side and routes along the reserved feeder corridor (see
    :mod:`mining_site`). Without it every new bank flows EAST as before.
    """

    def __init__(self, game, bootstrap, builder, *, perimeter=None, site=None):
        self.game, self.bootstrap, self.builder = game, bootstrap, builder
        self.perimeter = perimeter
        self.site = site
        self.path = Path(game.cfg.runtime_dir) / "mining-outposts.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @staticmethod
    def find(obs, e):
        return next((a for a in obs["entities"] if a["name"] == e["name"] and a["position"] == e["position"]), None)

    def publish_reservations(self, obs, resource=None):
        """Publish every saved paired bank on the board as block ``<resource>_mining[_n]``.

        A bank whose reserved area meets another registered block is not
        published; the others still are (three lanes share this instance, and
        one conflicting coal bank used to block the iron and copper lanes too,
        seed 2005199778). Returns the blocked status for the first conflict of
        ``resource`` (any resource when None) with ``block`` (the bank) and
        ``conflicting_block``, else None.
        """
        banks = [b for b in self.state.get("banks", []) if b["plan"].get("paired") and b.get("reserved_area")]
        if not banks:
            return None
        path = self.path.parent / "pcb-board.json"
        if not path.exists():
            return None
        board = json.loads(path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "mining_reservation_board_world_changed"}
        counts = {}
        conflict = None
        for bank in banks:
            declared = bank.get("pending_extension", bank)
            owner = bank["plan"]["resource"]
            index = counts.get(owner, 0)
            counts[owner] = index + 1
            name = owner + "_mining" + ("_" + str(index) if index else "")
            area = declared["reserved_area"]
            mine = block_rects(declared)
            met = next(
                (
                    key
                    for key, b in board["blocks"].items()
                    if key != name and any(overlap(r, o) for r in mine for o in block_rects(b))
                ),
                None,
            )
            if met is not None:
                if conflict is None and resource in (None, owner):
                    conflict = {
                        "status": "blocked",
                        "reason": "paired_mining_reserved_area_conflicts_with_registered_block",
                        "block": name,
                        "conflicting_block": met,
                    }
                continue
            extra = {}
            for field in ("mining_reserved_area", "layout_contract", "reserved_rects", "patch_frame"):
                if declared.get(field) is not None:
                    extra[field] = declared[field]
            board["blocks"][name] = dict(declared["plan"], reserved_area=area, **extra)
        _atomic_json(path, board)
        return conflict

    def _replan_conflicting_bank(self, obs, resource, conflict):
        """Drop an unbuilt ``resource`` bank that cannot be published, so the next step plans it again.

        Only a pending bank (not ready, no pending extension) of which no plan
        entity stands is dropped, at most ``MAX_BANK_REPLANS`` times per
        resource (``bank_replans``); the new plan avoids registered blocks and
        planned hardware (``planned_keepouts``). None keeps ``conflict``.
        """
        from .board import built_keys

        banks = [b for b in self.state.get("banks", []) if b["plan"]["resource"] == resource and not b.get("ready")]
        if len(banks) != 1 or banks[0].get("pending_extension"):
            return None
        bank = banks[0]
        built = built_keys(obs)
        if any((e["name"], e["position"]["x"], e["position"]["y"]) in built for e in bank["plan"]["entities"]):
            return None
        replans = self.state.setdefault("bank_replans", {})
        if replans.get(resource, 0) >= MAX_BANK_REPLANS:
            return None
        replans[resource] = replans.get(resource, 0) + 1
        self.state["banks"].remove(bank)
        _atomic_json(self.path, self.state)
        return {
            "status": "waiting",
            "reason": "unbuilt mining bank meets a registered block; planning it again",
            "evidence": {k: conflict.get(k) for k in ("block", "conflicting_block")},
        }

    def planned_keepouts(self, obs, board, resource):
        """What other plans hold that a new ``resource`` bank must not use.

        The same rule as ``board.starter_keepouts``: what is planned is
        published and every planner reads it. Returns

        * ``registered``: ``[(name, reserved area)]`` of every board block and
          every saved bank (banks of the three parallel lanes share this
          instance; a bank saved but not yet published counts too);
        * ``planned``: ``board.planned_unbuilt_hardware`` tiles of those blocks,
          of every saved bank's plan (bridge poles outside its reserved area
          included) and of the plans no board block holds (pending starter
          cells, the builder's power / coal feed, electric coal's poles:
          ``board.unboarded_plans``), within ``PLANNED_TILE_REACH`` of the patch seed;
        * ``tile_areas``: those tiles as one-tile areas, so the site search and
          the exit search treat them like reservations (the coal bank's exit
          and output access ran over the reserved feed belt, seed 2005199778).
        """
        from .board import _Footprints, planned_unbuilt_hardware, unboarded_plans

        blocks = {k: v for k, v in (board.get("blocks") or {}).items() if isinstance(v, dict)}
        registered = [(k, r) for k, v in sorted(blocks.items()) for r in block_rects(v)]
        for index, bank in enumerate(self.state.get("banks", [])):
            name = "mining_outposts:%s:%d" % (bank["plan"].get("resource"), index)
            entities = list(bank["plan"].get("entities", []))
            entities += list((bank.get("pending_extension") or {}).get("plan", {}).get("entities", []))
            blocks[name] = {"entities": entities}
            registered += [(name, r) for r in block_rects(bank)]
        blocks.update(unboarded_plans(self.path.parent, obs))
        planned = planned_unbuilt_hardware(_Footprints(), obs, blocks)
        known = (obs.get("resources", {}).get(resource) or {}).get("position")
        if isinstance(known, dict) and all(isinstance(known.get(k), (int, float)) for k in ("x", "y")):
            reach = PLANNED_TILE_REACH
            planned = {
                t: row
                for t, row in planned.items()
                if abs(t[0] - known["x"]) <= reach and abs(t[1] - known["y"]) <= reach
            }
        tile_areas = [[[x - 0.5, y - 0.5], [x + 0.5, y + 0.5]] for x, y in sorted(planned)]
        return {"registered": registered, "planned": planned, "tile_areas": tile_areas}

    def prepare(self, obs, resource, count, *, extra_reservations=()):
        from .paired_mining import prepare_new_bank

        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
        if board and board.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "mining_planning_board_world_changed"}
        site = getattr(self, "site", None)
        exit_record = site_exit(site, obs.get("world_id"), resource)
        trail = []  # every planning attempt of this call: the failure evidence
        seed = ((obs.get("resources") or {}).get(resource) or {}).get("position")
        if exit_record is not None and isinstance(seed, dict) and not seed_in_zone(exit_record, seed):
            # The observed nearest ore is not the plan's patch (the observation's nearest tile is the
            # nearest of a capped search; or the plan dropped a near patch whose zone met another
            # resource's zone). Seeds 3442106081 and 1541632234, fleet all-1113-2155.
            planned = planned_patch(site, obs.get("world_id"), resource)
            if planned is None or planned["distance"] > MAX_OTHER_PATCH_DISTANCE:
                failure = {
                    "ok": False,
                    "reason": NO_COMPLIANT_BANK,
                    "candidate_count": 0,
                    "candidate_failures": [],
                    "planned_patch_skipped": {
                        "distance": (planned or {}).get("distance"),
                        "limit": MAX_OTHER_PATCH_DISTANCE,
                        "seed": [seed["x"], seed["y"]],
                    },
                }
                reservations, options = self._planning_inputs(obs, board, resource, extra_reservations)
                return self._other_patch(obs, resource, count, reservations, options, failure, exit_record, trail)
            obs = _reseeded(obs, resource, planned["centroid"])
            trail.append({"reseeded": [round(c, 1) for c in planned["centroid"]], "from": [seed["x"], seed["y"]]})
        reservations, options = self._planning_inputs(obs, board, resource, extra_reservations)
        if exit_record is None:
            result = prepare_new_bank(self.game, self.builder, obs, resource, count, reservations, **options)
            trail.append(_attempt(None, None, result))
            return (
                result
                if result.get("ok")
                else self._other_patch(obs, resource, count, reservations, options, result, None, trail)
            )
        result = prepare_new_bank(
            self.game, self.builder, obs, resource, count, reservations, site_exit=exit_record, **options
        )
        trail.append(_attempt(None, exit_record["side"], result))
        if result.get("ok") and (result.get("patch_diagnostic") or {}).get("faces_neighbour") and exit_record:
            # The planned edge faces another resource's patch: take another side whose port does not.
            for alternative in alternative_exits(exit_record):
                other = prepare_new_bank(
                    self.game, self.builder, obs, resource, count, reservations, site_exit=alternative, **options
                )
                if other.get("ok") and not other["patch_diagnostic"].get("faces_neighbour"):
                    other["patch_diagnostic"]["edge_turned_from_neighbour"] = exit_record["side"]
                    return other
            return result
        if result.get("ok"):
            return result
        # The planned side had no usable site; try the zone's other sides
        # before giving up, nearest to the consumer first.
        tried = [exit_record["side"]]
        for alternative in [] if _survey_reason(result) else alternative_exits(exit_record):
            tried.append(alternative["side"])
            other = prepare_new_bank(
                self.game, self.builder, obs, resource, count, reservations, site_exit=alternative, **options
            )
            trail.append(_attempt(None, alternative["side"], other))
            if other.get("ok"):
                return other
            if _survey_reason(other):
                result = other  # the survey answers for every side: no use asking again
                break
        failure = dict(result, exit_sides_tried=tried)
        return self._other_patch(obs, resource, count, reservations, options, failure, exit_record, trail)

    def _planning_inputs(self, obs, board, resource, extra_reservations=()):
        """``(reservations, options)`` for :func:`prepare_new_bank` around the obs patch seed."""
        # Reservation rects (not bounding boxes) of every block and bank, plus other resources' patch frames.
        reservations = neighbour_reservations(board.get("blocks", {}).values(), (), resource)
        reservations += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", []) if r.get("area")]
        reservations += neighbour_reservations((), self.state.get("banks", []), resource)
        reservations += list(extra_reservations)
        shared = self.planned_keepouts(obs, board, resource)
        reservations += shared["tile_areas"]
        options = {k: shared[k] for k in ("registered", "planned") if shared[k]}
        from .mine_trunk import patch_bank, patch_seed

        # A later bank on the patch is another row pair of the same grid (mining_layout_policy.patch_grid).
        anchor = patch_bank(self.state.get("banks"), resource, patch_seed(obs, resource))
        policy = ((anchor or {}).get("layout_contract") or {}).get("policy") or {}
        if policy.get("grid_origin") and policy.get("flow"):
            options["grid_origin"] = {"origin": policy["grid_origin"], "flow": policy["flow"]}
        return reservations, options

    def _other_patch(self, obs, resource, count, reservations, options, failure, exit_record=None, trail=None):
        """Plan the bank on the site plan's next patches of ``resource`` when its own patch has no site.

        Fleet auto-1113-1758 seed 2072381151: the planned coal patch (215
        tiles) held the power coal feed and the electric coal drill, no bank
        candidate was left on any exit side, the bank blocked for good and no
        district followed, while the plan listed a second coal patch 43 tiles
        from the start. The other patch has no plan zone, so its bank must
        also keep clear of every other plan zone; other plans' unbuilt tiles
        are read again around the new patch seed. With the resource's
        ``exit_record`` every side of the other patch is tried, nearest the
        consumer first (:func:`mining_site.patch_exits`); before, only the
        historical east flow was, and on seed 1541632234 (fleet all-1113-2155)
        it faced the iron patch. The failure keeps ``attempts`` (patch, side,
        candidates, failures of each try) and ``needs`` (what would have to
        change for a bank to fit, :func:`_needs`).
        """
        from .paired_mining import prepare_new_bank

        trail = [] if trail is None else trail
        if failure.get("reason") not in (NO_COMPLIANT_BANK, SURVEY_EMPTY):
            return failure  # a budget or policy failure is no answer about the patch
        patches = [
            p
            for p in other_patches(getattr(self, "site", None), obs.get("world_id"), resource)
            if p["distance"] <= MAX_OTHER_PATCH_DISTANCE
        ]
        if not patches:
            return _with_evidence(failure, trail)
        zones = plan_zone_areas(getattr(self, "site", None), obs.get("world_id"), exclude_prefix="mining:" + resource)
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
        tried = []
        for patch in patches[:MAX_OTHER_PATCHES]:
            x, y = patch["centroid"]
            seeded = _reseeded(obs, resource, (x, y))
            tried.append([round(x, 1), round(y, 1)])
            shared = self.planned_keepouts(seeded, board, resource)
            seeded_options = dict(options, registered=list(shared["registered"]) + zones)
            seeded_options.pop("grid_origin", None)  # another patch is gridded afresh
            if shared["planned"]:
                seeded_options["planned"] = shared["planned"]
            exits = patch_exits(exit_record, patch["bbox"]) if exit_record and patch.get("bbox") else []
            for exit_option in exits or [None]:
                other = prepare_new_bank(
                    self.game,
                    self.builder,
                    seeded,
                    resource,
                    count,
                    list(reservations) + shared["tile_areas"],
                    **seeded_options,
                    **({"site_exit": exit_option} if exit_option else {}),
                )
                trail.append(_attempt([round(x, 1), round(y, 1)], (exit_option or {}).get("side"), other))
                if other.get("ok"):
                    other["plan"]["other_patch"] = {"centroid": [x, y], "bbox": patch.get("bbox")}
                    return other
                if _survey_reason(other):
                    break  # this patch's survey failed: its other sides would read the same area
        return _with_evidence(dict(failure, other_patches_tried=tried), trail)

    def bank_powered(self, obs, plan) -> bool:
        """Every drill of ``plan`` stands, is connected and has power (no ore evidence needed)."""
        drills = [self.find(obs, e) for e in plan["entities"] if e["name"] == "electric-mining-drill"]
        return bool(drills or plan.get("retired_drill_units")) and all(
            e and e.get("electric_network_connected") and e.get("energy", 0) > 0 and e.get("status_name") != "no_power"
            for e in drills
        )

    def bank_proof(self, obs, bank):
        from .depleted_drill_retirement import effective_mining_plan, FILE as RETIREMENT_FILE

        try:
            plan = effective_mining_plan(getattr(self, "game", None), obs, bank["plan"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        defense = None
        if bank.get("defense_scope_id"):
            perimeter = getattr(self, "perimeter", None)
            defense = perimeter.coverage_proof(obs, bank["defense_scope_id"]) if perimeter is not None else None
            if not defense:
                return None
        if not underground_proof(obs, plan):
            return None
        lane_proof = None
        if bank["plan"].get("paired"):
            from .paired_mining import lane_evidence

            lane_proof = lane_evidence(self.game, obs, bank)
            if not lane_proof.get("ok"):
                return None
        connection = bank.get("consumer_connection")
        if connection:
            target = self.find(obs, connection["target"])
            if (
                not target
                or target["unit_number"] != connection["target"]["unit_number"]
                or target["direction"] != connection["target"]["direction"]
                or set(target.get("belt_inventory", {})) - {bank["plan"]["resource"]}
            ):
                return None
        drills = [self.find(obs, e) for e in plan["entities"] if e["name"] == "electric-mining-drill"]
        if (not drills and not plan.get("retired_drill_units")) or any(
            not e
            or not e.get("electric_network_connected")
            or e.get("energy", 0) <= 0
            or e.get("status_name") == "no_power"
            for e in drills
        ):
            return None
        depleted = [
            e["unit_number"]
            for e in drills
            if e.get("status_name") in {"no_minable_resources", "no_mineable_resources"}
        ]
        installed = bank["nominal_rate_per_minute"]
        historical_count = sum(q["name"] == "electric-mining-drill" for q in bank["plan"]["entities"])
        available = installed * (len(drills) - len(depleted)) / historical_count
        capacity = None
        if hasattr(self, "path") and (self.path.parent / RETIREMENT_FILE).exists():
            from .mining_layout_policy import diagnose_bank_capacity

            key = (
                obs["world_id"],
                obs["tick"],
                hashlib.sha256(json.dumps(bank["plan"], sort_keys=True).encode()).hexdigest(),
                tuple(plan.get("retired_drill_units", [])),
            )
            cache = getattr(self, "_retirement_capacity_cache", {})
            if key not in cache:
                cache = {k: v for k, v in cache.items() if k[:2] == key[:2]}
                cache[key] = diagnose_bank_capacity(self.game, obs, self.builder.catalog, bank)
                self._retirement_capacity_cache = cache
            capacity = cache[key]
            if not capacity.get("ok") or not capacity.get("all_installed_drop_lanes_verified"):
                return None
            available = capacity["active_collector_ceiling_per_minute"]
        unit_ids = sorted(e["unit_number"] for e in drills)
        counts = 0
        for wanted in plan["entities"]:
            if wanted["name"] == "transport-belt":
                actual = self.find(obs, wanted)
                counts += (actual or {}).get("belt_inventory", {}).get(bank["plan"]["resource"], 0)
        if counts:
            bank["output_evidence"] = {"tick": obs["tick"], "count": counts, "drill_units": unit_ids}
            if available > 0:
                bank["native_drill_bindings"] = [
                    {"unit_number": q["unit_number"], "position": q["position"], "direction": q["direction"]}
                    for q in drills
                ]
        evidence = bank.get("output_evidence")
        if available > 0 and (
            not evidence or evidence["drill_units"] != unit_ids or not 0 <= obs["tick"] - evidence["tick"] <= 600
        ):
            return None
        return {
            "nominal_rate_per_minute": available,
            "installed_nominal_rate_per_minute": installed,
            "exhausted_drill_units": depleted,
            "drill_units": unit_ids,
            "native_output_evidence": evidence,
            "output_port": bank["plan"]["ports"][0],
            "consumer_connection": connection,
            "native_producer_lanes": lane_proof,
            "defense_coverage": defense,
            "native_active_capacity": capacity,
            "paid_retired_drill_units": plan.get("retired_drill_units", []),
            "historical_drill_count": historical_count,
            "inactive": available == 0,
            "scope": "native resource-backed powered potential; historical installed nameplate retained separately",
        }

    def _construction_coverage(self, obs, bank):
        perimeter = getattr(self, "perimeter", None)
        if perimeter is None:
            return None
        area = bank.get("reserved_area") or bank["plan"].get("mining_footprint")
        if area is None:
            cells = self.builder._occupied_by_plan(
                [e for e in bank["plan"]["entities"] if e.get("mining_resource") == bank["plan"]["resource"]]
            )
            if not cells:
                return {"status": "blocked", "reason": "mining_defense_footprint_missing"}
            area = [
                [min(x for x, y in cells) - 0.5, min(y for x, y in cells) - 0.5],
                [max(x for x, y in cells) + 0.5, max(y for x, y in cells) + 0.5],
            ]
        areas = [area]
        scope = (
            "mining-"
            + hashlib.sha256(
                json.dumps([bank["plan"]["resource"], bank["plan"]["ports"], areas], sort_keys=True).encode()
            ).hexdigest()[:20]
        )
        if bank.get("defense_scope_id") not in (None, scope) or bank.get("defense_areas", areas) != areas:
            return {"status": "blocked", "reason": "mining_defense_scope_geometry_changed"}
        if not bank.get("defense_scope_id"):
            bank.update(defense_scope_id=scope, defense_areas=areas)
            _atomic_json(self.path, self.state)
        return perimeter.ensure_coverage(obs, scope, areas)

    def plan_growth(self, obs, resource, target, rate, active) -> dict:
        """What ``step`` adds to the proven banks ``active`` (pure, no state write):
        ``{"bank", "extension"}`` (grow one in place), ``{"connected"}`` (a new module on a trunk)
        or ``{"failure"}``.

        Every proven bank of the resource may grow in place before a new module is connected: a
        connected extra module keeps its own reserved pairs (fleet all-1113-2332 seeds 677884616
        and 3862808667: the first bank's pairs were all built, the extra module had 2 of 3 pairs
        free, only the first bank was tried and the job blocked on a third module's trunk
        connection).

        A standing bank whose output feeds nothing (``dead_end_banks``) is connected to a trunk
        first: a few belts turn its drills into supply before any new drill is paid."""
        dead = [b for b in self.dead_end_banks(obs, resource) if any(b is a for a in active)]
        if dead:
            primaries = self._trunk_primaries(obs, resource, dead[0])
            for bank in dead:
                connected = self.prepare_dead_end_connection(obs, bank, primaries)
                if connected.get("ok"):
                    connected.pop("ok")
                    return {"bank": bank, "extension": connected}
        first = None
        for candidate in active:
            count = sum(e["name"] == "electric-mining-drill" for e in candidate["plan"]["entities"])
            per_drill = candidate["nominal_rate_per_minute"] / count
            added = min(8, math.ceil((target - rate) / per_drill))
            tried = self.prepare_extension(obs, candidate, added)
            if first is None:
                first = (candidate, tried, added)
            if tried.get("ok"):
                return {"bank": candidate, "extension": tried}
        bank, extension, added = first  # no bank grows in place: a new module on a trunk
        connected = {}
        for primary in self._trunk_primaries(obs, resource, bank):
            connected = self.prepare_connected_bank(obs, primary, added)
            # Only a bank whose own trunk does not reach the consumer is skipped (cheap, before
            # any site search): eval-1575138435 iron 300/min traced a proven extra bank whose
            # output ends on the patch (saved_corridor_has_gap at its own port), not the trunk.
            if connected.get("ok") or not str(connected.get("reason", "")).startswith("owned_ore_trunk"):
                break
        if not connected.get("ok"):
            return {"failure": {**connected, "upstream_extension_failure": extension.get("reason")}}
        return {"connected": connected}

    def _trunk_primaries(self, obs, resource, first):
        """Banks a new module may feed, the patch's trunk bank first (``mine_trunk.patch_bank``), then
        ``first`` and every other proven bank of ``resource`` (no dedicated-material bank)."""
        from .mine_trunk import patch_bank, patch_seed

        banks = [
            b
            for b in self.state.get("banks", [])
            if b["plan"]["resource"] == resource and b.get("ready") and not b.get("material_supply_id")
        ]
        trunk = patch_bank(banks, resource, patch_seed(obs, resource))
        ordered = [b for b in (trunk, first) if b is not None] + banks
        return [b for i, b in enumerate(ordered) if all(b is not o for o in ordered[:i])]

    def prepare_connected_bank(self, obs, primary, count):
        """Feed a paid extra module into an identity-bound same-item trunk T."""
        found = self._trunk_targets(obs, primary)
        if not found.get("ok"):
            return found
        targets, head = found["targets"], found["head"]
        failures = []
        rejected = []
        attempts = 0
        for site_index in range(6):
            extra = (
                self.prepare(obs, primary["plan"]["resource"], count, extra_reservations=rejected)
                if rejected
                else self.prepare(obs, primary["plan"]["resource"], count)
            )
            if not extra.get("ok"):
                failures.append(extra.get("reason"))
                break
            # A plan-oriented module routes inside its reserved feeder corridor.
            guide = guide_tuple(extra["plan"].get("site_exit"))
            routed, tried = self._route_to_trunk(obs, extra, targets, head, guide=guide)
            attempts += tried["routes"]
            failures += tried["failures"]
            if routed is not None:
                routed["planning_attempts"] = {"sites": site_index + 1, "routes": attempts}
                return routed
            if not extra.get("reserved_area"):
                break
            rejected.append(extra["reserved_area"])
        return {
            "ok": False,
            "reason": "no_clean_extra_bank_connection_to_owned_ore_trunk",
            "candidates": failures,
            "planning_attempts": {"sites": len(rejected), "routes": attempts},
        }

    def _route_to_trunk(self, obs, extra, targets, head, *, guide=None, built=False):
        """Route ``extra``'s output port onto the trunk (lane head first, then the nearest, middle and
        farthest side-load targets): ``(bank with the merged plan and consumer_connection, tried)``,
        or ``(None, tried)``. ``built``: the bank stands, only the route is preflighted."""
        resource = extra["plan"]["resource"]
        source = extra["plan"]["ports"][0]
        ranked = sorted(targets, key=lambda row: sum(abs(a - b) for a, b in zip(xy(source), xy(row["destination"]))))
        probes = [ranked[i] for i in sorted({0, len(ranked) // 2, len(ranked) - 1})] if ranked else []
        if built:
            # A standing bank: its nearest targets first (a few belts), never a far one first.
            probes = ranked[:3] + [r for r in probes if r not in ranked[:3]]
        probes = ([head] if head else []) + probes
        routing = {} if guide is None else {"guide": guide}
        tried = {"routes": 0, "failures": []}
        for row in probes:
            destination, target = row["destination"], row["target"]
            tried["routes"] += 1
            route = belt_connection(
                self.game, self.builder, obs, source, destination, extra["plan"]["entities"], **routing
            )
            if not route.get("ok"):
                tried["failures"].append(route.get("reason"))
                if route.get("reason") in {
                    "no_safe_bounded_mining_exit",
                    "mining_exit_requires_owned_source",
                    "prospective_mining_core_native_preflight_failed",
                }:
                    tried["stop"] = True
                    break
                continue
            es = {(e["name"], *xy(e)): e for e in extra["plan"]["entities"] + route["entities"]}
            metadata = {
                k: v
                for k, v in extra["plan"].items()
                if k
                not in (
                    "ok",
                    "reason",
                    "entities",
                    "ports",
                    "required_items",
                    "underground_pairs",
                    "clear_obstacles",
                    "clearance_world_id",
                )
            }
            plan = _plan(
                list(es.values()),
                extra["plan"]["ports"],
                **metadata,
                underground_pairs=extra["plan"].get("underground_pairs", []) + route["underground_pairs"],
            )
            check = preflight(self.game, self.builder, obs, route["entities"] if built else plan["entities"])
            if not check.get("ok"):
                tried["failures"].append(check.get("reason"))
                continue
            plan.update(clear_obstacles=check["clear_obstacles"], clearance_world_id=obs["world_id"])
            connected = dict(extra, plan=plan)
            connected["consumer_connection"] = {
                "kind": "second trunk lane head" if row.get("kind") == "lane" else "same-item perpendicular side-load",
                "item": resource,
                "target": target,
                "entry_port": destination,
                "route_metrics": route["route_metrics"],
            }
            return connected, tried
        return None, tried

    def dead_end_banks(self, obs, resource) -> list:
        """Ready banks of ``resource`` whose output port feeds nothing: no consumer connection, not the
        district's source port, no board connection from it, and no belt in front of the live port belt
        (eval-1575138435: an iron bank of 2 drills planned standalone while the other banks were
        unproven; its port belt ended 4 tiles beside the trunk, full of ore, counted as 60/min supply,
        and a coal bank of 2 drills the same)."""
        if getattr(self, "path", None) is None:
            return []
        root = self.path.parent
        district = {}
        try:
            district = json.loads((root / "smelting-district.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
        board = {}
        try:
            board = json.loads((root / "pcb-board.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
        if not isinstance(district, dict) or district.get("world_id") != obs.get("world_id"):
            return []  # no district: no trunk to tell a dead end from
        fed = {xy(p) for p in (district.get("source_ports") or {}).values() if isinstance(p, dict) and "position" in p}
        fed |= {
            (c["source"].get("x"), c["source"].get("y"))
            for c in board.get("connections", [])
            if isinstance(c.get("source"), dict)
        }
        belts = {
            xy(e): e
            for e in obs.get("entities", [])
            if e.get("name") in ("transport-belt", "underground-belt", "splitter")
        }
        found = []
        for bank in self.state.get("banks", []):
            plan = bank["plan"]
            if (
                plan["resource"] != resource
                or not bank.get("ready")
                or bank.get("material_supply_id")
                or bank.get("consumer_connection")
                or bank.get("pending_extension") is not None
            ):
                continue
            port = plan["ports"][0]
            p = xy(port)
            live = belts.get(p)
            if p in fed or live is None or live.get("name") != "transport-belt":
                continue
            v = VECTORS[live.get("direction", 0)]
            ahead = (p[0] + v[0], p[1] + v[1])
            if ahead in belts:
                continue
            found.append(bank)
        return found

    def prepare_dead_end_connection(self, obs, bank, primaries) -> dict:
        """Belts from a standing dead-end bank's port onto a same-item trunk (``dead_end_banks``): the
        bank's plan with the route added and its ``consumer_connection``, built as an extension."""
        failures = []
        for primary in primaries:
            if primary is bank:
                continue
            found = self._trunk_targets(obs, primary)
            if not found.get("ok"):
                failures.append(found.get("reason"))
                continue
            routed, tried = self._route_to_trunk(obs, bank, found["targets"], found["head"], built=True)
            failures += tried["failures"]
            if routed is not None:
                routed["dead_end_connection"] = {"tick": obs.get("tick"), "trunk_port": primary["plan"]["ports"][0]}
                return dict(routed, ok=True)
        return {"ok": False, "reason": "dead_end_bank_connection_failed", "candidates": failures}

    def _trunk_targets(self, obs, primary) -> dict:
        """Side-load targets along ``primary``'s traced trunk and its lane head:
        ``{"ok", "targets", "head"}`` or a failure."""
        path = self.path.parent / "smelting-district.json"
        consumer = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if consumer.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "connected_smelting_consumer_required_for_extra_bank"}
        owned = {(e["name"], *xy(e), e.get("direction", 0)) for e in consumer["plan"]["entities"]}
        surface = {xy(e): e for e in obs["entities"] if e["name"] == "transport-belt"}
        start = primary["plan"]["ports"][0]["position"]
        p = (start["x"], start["y"])
        previous = None
        destination = next(
            (
                p
                for bank in consumer.get("banks", [])
                if bank.get("resource") == primary["plan"]["resource"]
                for p in bank["ports"]
                if p.get("item") == primary["plan"]["resource"] and p.get("direction") == "input"
            ),
            None,
        )
        traced = None
        if destination:
            from .bus_factory import trace_corridor

            from .mine_trunk import tap_passthrough

            # A splitter tap replaced (and released) a trunk belt: the trace passes it.
            traced = trace_corridor(
                consumer["plan"],
                primary["plan"]["ports"][0],
                destination,
                passthrough=tap_passthrough(self.path.parent, obs["world_id"], primary),
            )
            if not traced.get("ok"):
                return {"ok": False, "reason": "owned_ore_trunk_trace_failed", "trace": traced}
            if not underground_proof(obs, consumer["plan"]):
                return {"ok": False, "reason": "owned_ore_trunk_underground_pair_changed"}
            actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
            for spec in traced["entities"]:
                live = actual.get((spec["name"], *xy(spec)))
                if (
                    not live
                    or live.get("direction", 0) != spec.get("direction", 0)
                    or type(live.get("unit_number")) is not int
                ):
                    return {
                        "ok": False,
                        "reason": "owned_ore_trunk_changed_before_expansion",
                        "position": spec["position"],
                    }
        targets = []
        for step in range(min(160, len(traced["entities"])) if traced else 30):
            spec = traced["entities"][step] if traced else None
            if spec and spec["name"] != "transport-belt":
                previous = None
                continue
            if spec:
                p = xy(spec)
            target = surface.get(p)
            if target is None:
                if traced:
                    return {"ok": False, "reason": "owned_ore_trunk_missing_before_expansion", "position": list(p)}
                break
            direction = target["direction"]
            v = VECTORS[direction]
            if step >= 3 and step % 3 == 0 and previous == direction and ("transport-belt", *p, direction) in owned:
                for side in (-1, 1):
                    delta = (-v[1] * side, v[0] * side)
                    q = (p[0] + delta[0], p[1] + delta[1])
                    if q in surface:
                        continue
                    facing = next(d for d, vector in VECTORS.items() if vector == (-delta[0], -delta[1]))
                    destination = {
                        "item": primary["plan"]["resource"],
                        "kind": "item",
                        "direction": "input",
                        "position": {"x": q[0], "y": q[1]},
                        "facing": facing,
                    }
                    targets.append(
                        {
                            "destination": destination,
                            "target": {k: target[k] for k in ("name", "position", "direction", "unit_number")},
                        }
                    )
            previous = direction
            p = (p[0] + v[0], p[1] + v[1])
        payload = json.dumps(
            json.dumps(
                {"targets": targets, "zones": mining_reservations(self.game, obs["world_id"])}, separators=(",", ":")
            )
        )
        legal = self.game.query(
            """local args=helpers.json_to_table("""
            + payload
            + """);local indices={}
for index,row in ipairs(args.targets)do local p=row.destination.position;local clear=s.count_entities_filtered{area={{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}},type="resource"}==0
 local target=target(row.target.position,"transport-belt");if not target or target.unit_number~=row.target.unit_number or target.direction~=row.target.direction or target.force~=f then clear=false end
 if clear then for i=1,2 do for _,item in pairs(target.get_transport_line(i).get_contents())do if item.name~=row.destination.item then clear=false;break end end end end
 if clear then for _,zone in ipairs(args.zones)do local b=zone.area;if p.x+.49>b[1][1]and p.x-.49<b[2][1]and p.y+.49>b[1][2]and p.y-.49<b[2][2]then clear=false;break end end end
 if clear then indices[#indices+1]=index end
end;return {ok=true,indices=indices}"""
        )
        if not legal.get("ok"):
            return legal
        targets = [targets[i - 1] for i in legal.get("indices", [])]
        from .mine_trunk import lane_head

        # A built second trunk lane takes the extra module at its head (2-2 balanced, lane-preserving)
        # before any side-load onto the first lane (mine_trunk.lane_plan).
        head = lane_head(self.path.parent, obs, primary)
        return {"ok": True, "targets": targets, "head": head}

    def prepare_extension(self, obs, bank, added_drills):
        """Grow upstream while keeping the installed downstream port untouched."""
        if bank["plan"].get("paired"):
            from .paired_mining import prepare_extension

            return prepare_extension(self.game, self.builder, obs, bank, added_drills)
        old = bank["plan"]
        drills = [e for e in old["entities"] if e["name"] == "electric-mining-drill"]
        if len(drills) + added_drills > 8:
            return {"ok": False, "reason": "connected_bank_exceeds_eight_drills"}
        first = min(drills, key=lambda e: e["position"]["x"])
        base = mining_bank(
            first["position"]["x"] - 4 * added_drills,
            first["position"]["y"],
            len(drills) + added_drills,
            old["resource"],
        )
        if base["ports"] != old["ports"]:
            return {"ok": False, "reason": "extension_changed_owned_output_port"}
        old_keys = {(e["name"], e["position"]["x"], e["position"]["y"]) for e in old["entities"]}
        fresh = [e for e in base["entities"] if (e["name"], e["position"]["x"], e["position"]["y"]) not in old_keys]
        native = self.builder.can_place(fresh)
        if not native.get("ok"):
            return {"ok": False, "reason": "upstream_extension_placement_obstructed", "blocked": native.get("blocked")}
        board = self.path.parent / "pcb-board.json"
        areas = (
            [p["reserved_area"] for p in json.loads(board.read_text(encoding="utf-8")).get("blocks", {}).values()]
            if board.exists()
            else []
        )
        payload = json.dumps(
            json.dumps({"entities": fresh, "resource": old["resource"], "reserved": areas}, separators=(",", ":"))
        )
        survey = self.game.query(
            """
local args=helpers.json_to_table("""
            + payload
            + """);local amount=0;local proto=prototypes.entity["electric-mining-drill"]
for _,e in ipairs(args.entities) do
 for _,area in ipairs(args.reserved) do
  if e.position.x+2>area[1][1] and e.position.x-2<area[2][1] and e.position.y+2>area[1][2] and e.position.y-2<area[2][2] then
   return {ok=false,reason="extension_intrudes_reserved_block"}
  end
 end
 if e.name=="electric-mining-drill" then
  local p=e.position;local r=proto.mining_drill_radius;local count=0;local available=0
  for _,ore in pairs(s.find_entities_filtered{area={{p.x-r,p.y-r},{p.x+r,p.y+r}},type="resource"}) do
   if ore.name~=args.resource then return {ok=false,reason="extension_mixed_resource"} end
   count=count+1;available=available+ore.amount
  end
  if count<4 or available<1800 then return {ok=false,reason="extension_insufficient_resource_coverage"} end
  amount=amount+available
 end
end
return {ok=true,remaining=amount,rate=proto.mining_speed*60/prototypes.entity[args.resource].mineable_properties.mining_time}
"""
        )
        if not survey.get("ok"):
            return survey
        pole = next(e for e in fresh if e["name"] == "small-electric-pole")
        power = plan_connection(
            self.game,
            self.builder,
            obs,
            pole["position"],
            [e for e in base["entities"] if e["name"] != "small-electric-pole"],
        )
        if not power.get("ok"):
            return power
        all_entities = {
            (e["name"], e["position"]["x"], e["position"]["y"]): e
            for e in old["entities"] + power["entities"] + base["entities"]
        }
        full = _plan(
            list(all_entities.values()),
            base["ports"],
            resource_cell=True,
            resource=old["resource"],
            drill_count=len(drills) + added_drills,
            layout_policy="pcb-mining-row-v1",
        )
        footprints = [self.builder._occupied_by_plan([e]) for e in full["entities"]]
        if sum(map(len, footprints)) != len(set().union(*footprints)):
            return {"ok": False, "reason": "extension_prospective_footprints_overlap"}
        return {
            "ok": True,
            "plan": full,
            "nominal_rate_per_minute": survey["rate"] * (len(drills) + added_drills),
            "remaining_resource_survey": bank.get("remaining_resource_survey", 0) + survey["remaining"],
            "wire_hops": bank.get("wire_hops", 0) + power["wire_hops"],
            "ready": False,
            "expansion_policy": "preserve existing drills, collector and output port; grow upstream",
        }

    def _recover_obstructed_bridge(self, obs, bank, result, defer=None):
        """Re-plan a pending bank's unbuilt power bridge around a new obstruction; None keeps ``result``.

        Jobs run concurrently: after a bank was planned another job may build
        on a tile its bridge still needs (live: a science cell inserter on a
        planned bridge pole), and a retry re-read the same saved plan forever.
        Rules:

        * Only ``reserved block placement is obstructed`` on a bridge pole
          (:func:`bridge_poles`) of a bank that is not ready is handled. An
          obstructed drill, collector belt or exit returns ``result`` unchanged.
        * The obstruction must be a real observed entity covering the pole's
          tile that is not the planned pole itself; otherwise ``result`` stays.
        * At most :data:`MAX_BRIDGE_REPLANS` re-plans per bank, counted in
          ``bank["power_bridge_replans"]``; after that ``result`` stays.
        * Built bridge poles and all bank hardware stay; only unbuilt bridge
          poles are replaced (:func:`power_grid.replan_power_bridge`). Nothing
          is removed from the world.
        * The new plan passes the same checks as at prepare time (whole future
          footprint with growth and output access, then the current plan with
          its clearance), is persisted, the board is republished and building
          continues with one ``ensure_plan`` call on the new plan.
        * Unchanged by design: ``layout_contract`` / ``contract_id`` (fixed
          ports and growth only), ``defense_scope_id`` (resource, ports,
          reserved area), ``reserved_area`` and ``mining_reserved_area``
          (bridge poles are outside them). The proof cache keys on the plan
          hash and recomputes by itself; retirement authorities exist only for
          ready banks.
        """
        reason = result.get("reason")
        if result.get("status") != "blocked" or reason not in (OBSTRUCTED, IN_RESERVATION) or bank.get("ready"):
            return None
        evidence = result.get("evidence") or {}
        blocked = evidence.get("entity")
        if reason == IN_RESERVATION and blocked is None:
            # The preflight refusal carries its fields at the top level of the result.
            evidence = {k: result[k] for k in ("entity", "position") if k in result}
            blocked = evidence.get("entity")
        if reason == IN_RESERVATION and isinstance(blocked, str) and isinstance(evidence.get("position"), dict):
            # The preflight names the entity and its position separately. A bridge
            # pole planned inside the bank's own reserved columns (plans saved
            # before the planner kept bridges out of them) is re-planned the same way.
            blocked = {"name": blocked, "position": evidence["position"]}
        if not isinstance(blocked, dict) or blocked.get("name") != "small-electric-pole":
            return None
        bridge = bridge_poles(bank)
        if _key(blocked) not in {_key(e) for e in bridge}:
            return None
        tiles = self.builder._occupied_by_plan([blocked])
        obstruction = [
            e
            for e in obs.get("entities", [])
            if e.get("name") != "character"
            and isinstance(e.get("position"), dict)
            and _key(e) != _key(blocked)
            and self.builder._occupied_by_plan([e]) & tiles
        ]
        if not obstruction and reason == OBSTRUCTED:
            return None
        count = bank.get("power_bridge_replans", 0)
        if count >= MAX_BRIDGE_REPLANS:
            return None
        if reason == IN_RESERVATION and self._adopt_growth_pole(bank, blocked):
            bank["power_bridge_replans"] = count + 1
            _atomic_json(self.path, self.state)
            reservation = self.publish_reservations(obs, bank["plan"]["resource"])
            if reservation:
                return reservation
            return self.builder.ensure_plan(obs, bank["plan"], **({"defer": defer} if defer else {}))
        names = sorted({e["name"] for e in obstruction}) or ["mining reservation"]
        failure = self._replan_bridge(obs, bank, bridge, blocked, names)
        if failure is not None:
            return dict(result, evidence=dict(evidence, power_bridge_replan=failure))
        bank["power_bridge_replans"] = count + 1
        _atomic_json(self.path, self.state)
        reservation = self.publish_reservations(obs, bank["plan"]["resource"])
        if reservation:
            return reservation
        return self.builder.ensure_plan(obs, bank["plan"], **({"defer": defer} if defer else {}))

    def _adopt_growth_pole(self, bank, blocked) -> bool:
        """Turn a bridge pole that stands on one of the bank's own future pole slots into that slot's pole.

        The growth plan already puts an annotated pole on that tile, so the
        bridge pole only lacked the mining annotation the preflight requires.
        True when the plan entity was annotated.
        """
        from .mining_layout_policy import oriented_pair_core

        plan = bank["plan"]
        contract = bank.get("layout_contract") or plan.get("layout_contract")
        try:
            future = oriented_pair_core(
                contract["collector_port"]["position"],
                contract["reserved_pair_slots"],
                plan["resource"],
                contract["policy"]["flow"],
            )
        except (KeyError, TypeError, ValueError):
            return False
        slot = next(
            (e for e in future["entities"] if e["name"] == "small-electric-pole" and _key(e) == _key(blocked)), None
        )
        mine = next((e for e in plan["entities"] if _key(e) == _key(blocked)), None)
        if slot is None or mine is None or not slot.get("mining_resource"):
            return False
        mine.update({k: slot[k] for k in ("mining_resource", "mining_footprint") if k in slot})
        return True

    def _replan_bridge(self, obs, bank, bridge, blocked, obstruction):
        """Replace ``bank["plan"]``'s unbuilt bridge in memory; a failure reason, or None on success."""
        from .mining_layout_policy import oriented_pair_core
        from .smelting import merge_entities

        plan = bank["plan"]
        contract = bank.get("layout_contract") or plan.get("layout_contract")
        try:
            future = oriented_pair_core(
                contract["collector_port"]["position"],
                contract["reserved_pair_slots"],
                plan["resource"],
                contract["policy"]["flow"],
            )
            facing = plan["ports"][0]["facing"]
            access = [entity("transport-belt", p["x"], p["y"], facing) for p in contract["output_access"]]
        except (KeyError, TypeError, ValueError):
            return "power_bridge_replan_contract_invalid"
        # The same keep-outs as at prepare time: future drills/belts, the
        # bank's own belts and the reserved output access cells.
        from .mining_layout_policy import _future_poles, _reserved_tiles

        reserved = [e for e in future["entities"] + plan["entities"] if e["name"] != "small-electric-pole"] + access
        slots = [dict(x=x, y=y) for x, y in _future_poles(future)]
        reserved += _reserved_tiles(future["mining_footprint"], contract["fixed_power_port"], slots)
        replanned = replan_power_bridge(
            self.game,
            self.builder,
            obs,
            plan,
            power_port=contract["fixed_power_port"],
            bridge=bridge,
            reserved=reserved,
            avoid=[blocked["position"]],
        )
        if not replanned.get("ok"):
            return replanned.get("reason")
        try:
            whole = merge_entities(future["entities"], replanned["entities"], access)
        except ValueError:
            return "power_bridge_replan_conflicts_with_growth"
        full = preflight(self.game, self.builder, obs, whole)
        if not full.get("ok"):
            return full.get("reason", "power_bridge_replan_preflight_failed")
        actual = preflight(self.game, self.builder, obs, replanned["entities"])
        if not actual.get("ok"):
            return actual.get("reason", "power_bridge_replan_preflight_failed")
        metadata = {
            k: v
            for k, v in plan.items()
            if k not in ("ok", "reason", "entities", "ports", "required_items", "clear_obstacles", "clearance_world_id")
        }
        bank["plan"] = _plan(
            replanned["entities"],
            plan["ports"],
            **metadata,
            clear_obstacles=actual["clear_obstacles"],
            clearance_world_id=obs["world_id"],
        )
        bank["wire_hops"] = replanned["wire_hops"]
        bank.setdefault("power_bridge_history", []).append(
            {
                "tick": obs.get("tick"),
                "obstructed": dict(blocked["position"]),
                "obstruction": list(obstruction),
                "kept": [dict(e["position"]) for e in replanned["kept"]],
                "removed": [dict(e["position"]) for e in replanned["removed"]],
                "added": [dict(e["position"]) for e in replanned["added"]],
            }
        )
        return None

    def step(self, obs, parameters):
        validate_parameters(parameters)
        from .early_start import deferral, ledger_jobs, technologies_coming

        # Early start: while the drill research is coming, the bank is planned
        # and published and everything but its drills is built (Deferral).
        defer = None
        if not obs.get("technologies", {}).get("electric-mining-drill"):
            coming = technologies_coming(obs, ledger_jobs(self.path.parent, obs.get("world_id")))
            if "electric-mining-drill" not in coming:
                return {"status": "blocked", "reason": "electric_mining_research_required"}
            defer = deferral(self.path.parent, obs, self.builder.catalog, "mining_outpost")
        if not self.state:
            self.state = {"world_id": obs["world_id"], "banks": []}
        if self.state["world_id"] != obs["world_id"]:
            return {"status": "blocked", "reason": "outpost_world_changed"}
        resource, target = parameters["resource"], parameters["rate_per_minute"]
        reservation = self.publish_reservations(obs, resource)
        if reservation:
            return self._replan_conflicting_bank(obs, resource, reservation) or reservation
        # A dedicated-material bank (material_supply.py) belongs to its supply: its output is not
        # this target's, it is not extended or built here, and its rate is not counted twice.
        banks = [
            b for b in self.state["banks"] if b["plan"]["resource"] == resource and not b.get("material_supply_id")
        ]
        for bank in banks:
            from .depleted_drill_retirement import effective_mining_plan

            try:
                effective = effective_mining_plan(getattr(self, "game", None), obs, bank["plan"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return {"status": "blocked", "reason": "mining_retirement_active_plan_invalid", "detail": str(exc)}
            if bank.get("ready") and any(self.find(obs, e) is None for e in effective["entities"]):
                return {"status": "blocked", "reason": "existing_outpost_damaged_requires_repair_before_expansion"}
            # A proven bank only has to be powered to be extended. The full proof
            # wants ore on both collector lanes, and a bank whose ore is eaten as
            # fast as it is mined (the very case that needs more drills) shows an
            # empty belt: the extension waited an hour and died (seed 2644599892).
            if bank.get("ready") and self.bank_proof(obs, bank) is None and not self.bank_powered(obs, effective):
                return {"status": "waiting", "reason": "restore existing outpost power before adding capacity"}
        active_proofs = [(b, self.bank_proof(obs, b)) for b in banks if b.get("ready")]
        for bank, proof in active_proofs:
            # A proven bank whose quiet-start defense deferral lapsed (enemies or risk now near) has no
            # proof until its turrets stand: arm it before counting it as missing capacity
            # (eval-1575138435, 257 min: iron banks of 180 + 60/min unproven, the job planned a third module).
            if proof is None and bank.get("defense_scope_id"):
                coverage = self._construction_coverage(obs, bank)
                if coverage is not None:
                    return coverage
        active = [b for b, p in active_proofs if p]
        rate = sum(p["nominal_rate_per_minute"] for _, p in active_proofs if p)
        if rate >= target:
            _atomic_json(self.path, self.state)
            return {
                "status": "succeeded",
                "reason": "powered electric mining banks have native output",
                "evidence": self.proof(obs, parameters),
            }
        pending = next((b for b in banks if not b.get("ready")), None)
        if active and pending is None:
            bank = next((b for b in active if b.get("pending_extension") is not None), active[0])
            extension = bank.get("pending_extension")
            if extension is None:
                growth = self.plan_growth(obs, resource, target, rate, active)
                if growth.get("failure"):
                    return {"status": "blocked", **growth["failure"]}
                if growth.get("connected"):
                    self.state["banks"].append(growth["connected"])
                    _atomic_json(self.path, self.state)
                    return {
                        "status": "waiting",
                        "reason": "reserved extra mining module and same-item trunk connection",
                    }
                bank, extension = growth["bank"], growth["extension"]
                bank["pending_extension"] = extension
                _atomic_json(self.path, self.state)
            reservation = self.publish_reservations(obs, resource)
            if reservation:
                return reservation
            from .mining_layout_policy import validate_retained_units

            retained = validate_retained_units(obs, extension)
            if not retained.get("ok"):
                return dict(retained, status="blocked")
            coverage = self._construction_coverage(obs, extension)
            if coverage is not None:
                return coverage
            result = self.builder.ensure_plan(obs, extension["plan"])
            if result.get("status") != "succeeded":
                return result
            proof = self.bank_proof(obs, extension)
            if not proof:
                return {"status": "waiting", "reason": "waiting for expanded powered bank output"}
            bank.pop("pending_extension")
            bank.update(extension)
            bank["ready"] = True
            _atomic_json(self.path, self.state)
            return {"status": "waiting", "reason": "upstream mining extension proved; remeasure absolute target"}
        if pending is None:
            metadata = self.game.query(
                'return {rate=prototypes.entity["electric-mining-drill"].mining_speed*60/prototypes.entity['
                + json.dumps(resource)
                + "].mineable_properties.mining_time}"
            )
            if not isinstance(metadata.get("rate"), (int, float)) or metadata["rate"] <= 0:
                return {"status": "blocked", "reason": "mining_rate_query_failed"}
            # A shrink rung of the remedy ladder plans fewer drills now (growth adds the rest later).
            count = remedy.scaled_count(min(8, math.ceil((target - rate) / metadata["rate"])))
            pending = self.prepare(obs, resource, count)
            if not pending.get("ok"):
                detail = {
                    k: pending[k]
                    for k in (
                        "candidate_count",
                        "candidate_failures",
                        "exit_sides_tried",
                        "other_patches_tried",
                        "planned_patch_skipped",
                        "attempts",
                        "needs",
                        "charted_chunks",
                        "area",
                    )
                    if k in pending
                }
                return {
                    "status": "blocked",
                    "reason": pending.get("reason", "mining_bank_preflight_failed"),
                    **({"evidence": detail} if detail else {}),
                }
            self.state["banks"].append(pending)
            _atomic_json(self.path, self.state)
        reservation = self.publish_reservations(obs, resource)
        if reservation:
            return self._replan_conflicting_bank(obs, resource, reservation) or reservation
        coverage = self._construction_coverage(obs, pending)
        if coverage is not None:
            return coverage
        result = self.builder.ensure_plan(obs, pending["plan"], **({"defer": defer} if defer else {}))
        recovered = self._recover_obstructed_bridge(obs, pending, result, defer)
        if recovered is not None:
            result = recovered
        if result.get("status") != "succeeded":
            return result
        proof = self.bank_proof(obs, pending)
        if proof:
            pending["ready"] = True
            _atomic_json(self.path, self.state)
            return {"status": "waiting", "reason": "new electric bank output observed; remeasure whole target"}
        return {"status": "waiting", "reason": "waiting for real electric drill output onto aligned collector"}

    def proof(self, obs, parameters):
        proofs = [
            self.bank_proof(obs, b)
            for b in self.state.get("banks", [])
            if b.get("ready") and b["plan"]["resource"] == parameters["resource"] and not b.get("material_supply_id")
        ]
        proofs = [p for p in proofs if p]
        total = sum(p["nominal_rate_per_minute"] for p in proofs)
        if total < parameters["rate_per_minute"]:
            return None
        _atomic_json(self.path, self.state)
        return {
            "tick": obs["tick"],
            "resource": parameters["resource"],
            "nominal_rate_per_minute": total,
            "banks": proofs,
            "scope": "powered automatic extraction with native collector output; rate is catalog capacity, not a sustained throughput measurement",
        }
