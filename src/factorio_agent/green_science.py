"""Belt-fed green science, local intermediates and an explicit lab lane join."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from types import SimpleNamespace
from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .manufacturing import GREEN_CORE_DEPTH, GREEN_CORE_LENGTH, green_core, raw_demand
from .manufacturing import connect_power_groups, lab_green_entry, lab_green_entry_outside
from .planning import preflight, overlap, survey_slots
from .science import nominal_plate_supply
from .smelting import merge_entities
from .site.placement import allocated_placements, settle
from .site.transform import Transform, apply as apply_transform, in_canonical, plan_transform
from .site.transform import point as transform_point
from .transport import plan_connection, xy, underground_proof


def validate_rate(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"rate_per_minute"}:
        raise ValueError("logistic_science requires rate_per_minute")
    r = parameters["rate_per_minute"]
    if type(r) not in (int, float) or not math.isfinite(r) or not 0 < r <= 30:
        raise ValueError("green science target must be finite in (0,30]")


# Narrow green core footprint (canonical x across, y along its plate edge)
# and the district block it needs: GREEN_CORE_LENGTH along the bus by
# GREEN_CORE_DEPTH deep, so the west (plate) edge can face the bus.
GREEN_FOOTPRINT = (GREEN_CORE_DEPTH, GREEN_CORE_LENGTH)
GREEN_BLOCK = (GREEN_CORE_LENGTH, GREEN_CORE_DEPTH)
# Development-map anchors, only for a controller built without a site service.
LEGACY_GREEN_ANCHORS = (
    (40.5, -72.5),
    (56.5, -72.5),
    (72.5, -72.5),
    (40.5, -8.5),
    (-96.5, -72.5),
    (-96.5, -8.5),
    (104.5, -72.5),
)


def planned_green_cores(site, world_id, count):
    """Allocator blocks for the narrow green core, turned so plate inputs face the bus edge.

    The narrow core (``green_core(..., narrow=True)``) reserves
    ``GREEN_FOOTPRINT`` from its north-west corner with both plate inputs on
    its west edge, so the block is requested ``GREEN_CORE_LENGTH`` along the
    bus by ``GREEN_CORE_DEPTH`` deep and the fit turns the west edge to the bus.
    Empty when the plan's district strips are shallower or the bus is full.
    """

    def build(x0, y0):
        return green_core(x0 + 10.5, y0 + 10.5, count, narrow=True)

    def inputs(core):
        return [p for p in core["ports"] if p["direction"] == "input"]

    found = []
    placements = allocated_placements(
        site,
        world_id,
        kind="science",
        name="logistic_science",
        size=GREEN_FOOTPRINT,
        block=GREEN_BLOCK,
        build=build,
        inputs=inputs,
    )
    for allocation, placement in placements:
        core = placement.plan
        core["site_transform"] = placement.transform.to_dict()
        core["allocation"] = allocation
        found.append(core)
    return found


class LogisticScience:
    def __init__(self, game, builder, catalog, smelting, science, site=None):
        self.game, self.builder, self.catalog, self.smelting, self.science = game, builder, catalog, smelting, science
        self.site = site
        self.path = Path(game.cfg.runtime_dir) / "logistic-science.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def prepare(self, obs, parameters):
        validate_rate(parameters)
        if not self.science.proof(obs):
            return {"ok": False, "reason": "commissioned_red_science_required"}
        required = {"logistic-science-pack", "fast-inserter"}
        if not required.issubset(k for k, v in obs.get("enabled_recipes", {}).items() if v):
            return {"ok": False, "reason": "native_green_science_and_fast_inserter_research_required"}
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or self.smelting.state.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "green_supply_world_changed"}
        each = (
            self.catalog.entities["assembling-machine-1"]["crafting_speed"]
            * 60
            / self.catalog.recipes["logistic-science-pack"]["energy"]
        )
        count = math.ceil(parameters["rate_per_minute"] * 1.15 / each)
        if count > 7:
            return {"ok": False, "reason": "green_science_row_exceeds_seven_slots"}
        sized = self.supply_sized_count(count, each)
        if not sized.get("ok"):
            return sized
        count = sized["count"]
        demand = raw_demand(self.catalog, "logistic-science-pack", count * each)
        for recipe, equivalent in demand["machine_equivalents"].items():
            if recipe != "logistic-science-pack" and equivalent > 1:
                return {"ok": False, "reason": "one_intermediate_cell_insufficient", "recipe": recipe}
        source_plan = deepcopy(self.smelting.state["plan"])
        removed = []
        branches = []
        sources = {}
        # The branch is defined in the district's canonical frame (plates
        # leaving east) and turned like the district (identity when unturned).
        turn = Transform.from_dict(source_plan.get("site_transform"))
        canonical = apply_transform(turn.inverse(), {"reserved_area": source_plan["reserved_area"]})
        district_x1 = canonical["reserved_area"][1][0]  # east edge, where the plate ports sit
        taken = self.builder._occupied_by_plan(obs["entities"] + source_plan["entities"])

        def mapped(x, y):
            return xy({"position": transform_point(turn, {"x": x, "y": y})})

        for p in source_plan["ports"]:
            if p["item"] not in ("iron-plate", "copper-plate"):
                continue
            x, y = xy(apply_transform(turn.inverse(), p))
            anchor = mapped(x - 1, y)
            heading = turn.direction(4)
            old = next(
                (
                    e
                    for e in obs["entities"]
                    if e["name"] == "transport-belt" and xy(e) == anchor and e["direction"] == heading
                ),
                None,
            )
            if not old:
                return {"ok": False, "reason": "owned_plate_branch_anchor_missing", "item": p["item"]}
            if not any(e["name"] == "transport-belt" and xy(e) == xy(old) for e in source_plan["entities"]):
                return {"ok": False, "reason": "plate_branch_anchor_not_owned"}
            # The splitter's second lane runs east beside the port row until
            # its first tile outside the district reservation: a route may
            # only start from a registered, built port inside a reservation.
            stub = [x]
            while stub[-1] - 0.49 < district_x1:
                stub.append(stub[-1] + 1)
            side = next(
                (s for s in (-1, 1) if not any(mapped(bx, y + s) in taken for bx in [x - 1, *stub])),
                None,
            )
            if side is None:
                return {"ok": False, "reason": "plate_branch_lane_blocked", "item": p["item"]}
            removed.append(old)
            branches += apply_transform(
                turn,
                [dict(entity("splitter", x - 1, y + side * 0.5, 4), _width=2, _height=1)]
                + [entity("transport-belt", bx, y + side, 4) for bx in stub],
            )
            sources[p["item"]] = apply_transform(turn, dict(p, position={"x": stub[-1], "y": y + side}, facing=4))
        ignored = {e["unit_number"] for e in removed}
        source_plan["entities"] = [
            e for e in source_plan["entities"] if not any(e["name"] == r["name"] and xy(e) == xy(r) for r in removed)
        ]
        source_plan = _plan(
            merge_entities(source_plan["entities"], branches),
            source_plan["ports"],
            **{
                k: v for k, v in source_plan.items() if k not in ("ok", "reason", "entities", "ports", "required_items")
            },
        )
        labs_plan = self.science.state["labs"]
        if self.site is not None:
            entry = in_canonical(labs_plan, lab_green_entry_outside)
        else:  # development-map lab rows without a board reservation contract
            entry = in_canonical(labs_plan, lab_green_entry)
            entry["pairs"] = [entry["pair"]]
        lab_heading = plan_transform(labs_plan).direction(12)
        jx, jy = xy({"position": entry["join"]})
        join = next(
            (
                e
                for e in obs["entities"]
                if e["name"] == "transport-belt" and xy(e) == (jx, jy) and e["direction"] == lab_heading
            ),
            None,
        )
        if not join:
            return {"ok": False, "reason": "straight_lab_lane_join_anchor_missing"}
        lab_port = entry["port"]
        join_es = entry["entities"]
        join_pairs = entry["pairs"]
        virtual = {**obs, "entities": [e for e in obs["entities"] if e.get("unit_number") not in ignored]}
        adapter = SimpleNamespace(
            _occupied_by_plan=self.builder._occupied_by_plan,
            can_place=lambda es: preflight(self.game, self.builder, obs, es, ignored_units=ignored),
        )
        candidates = []
        # Sites come from the master plan; the fixed development-map anchors
        # are only for a controller built without a site service.
        if self.site is not None:
            cores = planned_green_cores(self.site, obs["world_id"], count)
            if not cores:
                return self.no_allocation(obs["world_id"])
        else:
            cores = [green_core(x, y, count) for x, y in LEGACY_GREEN_ANCHORS]
        for core in cores:
            if not any(overlap(core["reserved_area"], b["reserved_area"]) for b in board["blocks"].values()):
                candidates.append(core)
        survey = survey_slots(self.game, [[c["reserved_area"]] for c in candidates])
        if not survey.get("ok"):
            settle(self.site, obs["world_id"], "logistic_science", None)
            return survey
        failures = []
        for index in survey.get("legal", []):
            core = candidates[index - 1]
            es = merge_entities(core["entities"], branches, join_es)
            routes = []
            check = preflight(self.game, self.builder, obs, es, ignored_units=ignored)
            if not check.get("ok"):
                failures.append(check)
                continue
            obstacles = {(e["name"], *xy(e)): e for e in check["clear_obstacles"]}
            connections = core["connections"] + [
                (sources[p["item"]], p) for p in core["ports"] if p["direction"] == "input"
            ]
            connections.append((core["ports"][-1], lab_port))
            valid = True
            for source, destination in connections:
                route = plan_connection(self.game, adapter, virtual, source, destination, es, ignored_units=ignored)
                if not route.get("ok"):
                    failures.append(
                        {
                            "reason": route.get("reason"),
                            "item": source["item"],
                            "source": source["position"],
                            "destination": destination["position"],
                            "area": core["reserved_area"],
                        }
                    )
                    valid = False
                    break
                routes.append(route)
                es = merge_entities(es, route["entities"])
                obstacles.update({(e["name"], *xy(e)): e for e in route["clear_obstacles"]})
            if not valid:
                continue
            power = connect_power_groups(self.game, self.builder, obs, list(core["cells"].values()), es)
            if not power.get("ok"):
                failures.append(power)
                continue
            es = merge_entities(es, power["entities"])
            check = preflight(self.game, self.builder, obs, es, ignored_units=ignored)
            if not check.get("ok"):
                failures.append(check)
                continue
            obstacles.update({(e["name"], *xy(e)): e for e in check["clear_obstacles"]})
            pairs = join_pairs + [p for r in routes for p in r["underground_pairs"]]
            plan = _plan(
                es,
                core["ports"],
                reserved_area=core["reserved_area"],
                underground_pairs=pairs,
                clear_obstacles=list(obstacles.values()),
                clearance_world_id=obs["world_id"],
                layout_policy="pcb-green-science-cells-v1",
            )
            settle(self.site, obs["world_id"], "logistic_science", core.get("allocation"))
            return {
                "ok": True,
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "phase": "recover",
                "plan": plan,
                "core": core,
                "source_plan": source_plan,
                "remove": removed,
                "lab_join": {k: join[k] for k in ("name", "position", "direction", "unit_number")},
                "requested_rate": parameters["rate_per_minute"],
                "nominal_machine_rate": count * each,
                "rated_demand": demand,
                "route_metrics": [r["route_metrics"] for r in routes],
            }
        settle(self.site, obs["world_id"], "logistic_science", None)
        return {"ok": False, "reason": "no_green_science_site_with_clean_connections", "failures": failures}

    def supply_sized_count(self, count, each):
        """Green machines the connected plates can feed, at most ``count``.

        The splitter branch shares each district plate belt with red science:
        green may use what red's row leaves, and never less than the half a
        saturated splitter hands each branch. Like red science, the row is
        sized down to that budget; only a budget below one machine blocks.
        """
        root = self.path.parent
        mining = json.loads((root / "mining-outposts.json").read_text(encoding="utf-8"))
        smelting = self.smelting.state
        if mining.get("world_id") != smelting.get("world_id"):
            return {"ok": False, "reason": "green_raw_supply_world_changed"}
        per_machine = raw_demand(self.catalog, "logistic-science-pack", each)["raw_items_per_minute"]
        red = raw_demand(self.catalog, "automation-science-pack", self.science.state.get("nominal_machine_rate", 0))
        available = nominal_plate_supply(self.catalog, mining, smelting, per_machine)
        budget = {
            item: max(available[item] - red["raw_items_per_minute"].get(item, 0), available[item] / 2)
            for item in per_machine
        }
        fit = min(count, *(int(budget[item] / rate + 1e-9) for item, rate in per_machine.items()))
        if fit < 1:
            item = min(per_machine, key=lambda name: budget[name] / per_machine[name])
            return {
                "ok": False,
                "reason": "green_science_needs_connected_supply_headroom",
                "item": item,
                "needed": per_machine[item],
                "available_nominal": budget[item],
            }
        return {"ok": True, "count": fit}

    def no_allocation(self, world_id):
        """Blocked reason when the master plan has no block for the core."""
        try:
            depth = self.site.plan(world_id).district_depth
        except Exception:  # noqa: BLE001 - the reason is diagnostic only
            depth = None
        reason = "green_science_needs_deeper_district" if depth is not None and depth < GREEN_CORE_DEPTH else None
        return {
            "ok": False,
            "reason": reason or "no_green_science_district_block",
            "block_along": GREEN_CORE_LENGTH,
            "block_depth": GREEN_CORE_DEPTH,
            "district_depth": depth,
        }

    def repair_lab_join(self, obs):
        """Migrate an early development join that omitted the upstream lab."""
        if not plan_transform(self.science.state["labs"]).identity:
            return None  # only canonical development-map lab rows have the old join
        entry = lab_green_entry(self.science.state["labs"])
        new_x = entry["join"]["x"]
        old = self.state["lab_join"]
        old_x, y = xy(old)
        if old_x == new_x:
            return None
        if new_x - old_x != 4:
            return {"status": "blocked", "reason": "unsupported_lab_join_migration_geometry"}
        obsolete = {(old_x, y + 6), (old_x, y + 5), (old_x, y + 2), (old_x, y + 1)}
        wanted = [e for e in self.state["plan"]["entities"] if xy(e) in obsolete]
        remove = [e for e in obs["entities"] if any(e["name"] == w["name"] and xy(e) == xy(w) for w in wanted)]
        join = next(
            (
                e
                for e in obs["entities"]
                if e["name"] == "transport-belt" and xy(e) == (new_x, y) and e["direction"] == 12
            ),
            None,
        )
        if not join:
            return {"status": "blocked", "reason": "upstream_lab_join_anchor_missing"}
        es = [e for e in self.state["plan"]["entities"] if not (xy(e) in obsolete)]
        es += [entity("transport-belt", old_x + i, y + 6, 4 if i < 4 else 0) for i in range(5)]
        es += [
            dict(entity("underground-belt", new_x, y + 5, 0), belt_to_ground_type="input"),
            dict(entity("underground-belt", new_x, y + 2, 0), belt_to_ground_type="output"),
            entity("transport-belt", new_x, y + 1, 0),
        ]
        check = preflight(self.game, self.builder, obs, es, ignored_units=[e["unit_number"] for e in remove])
        if not check.get("ok"):
            return {"status": "blocked", **check}
        pairs = [p for p in self.state["plan"]["underground_pairs"] if p["input"] != [old_x, y + 5]]
        pairs.append({"input": [new_x, y + 5], "output": [new_x, y + 2], "direction": 0, "span": 3})
        extra = {
            k: v
            for k, v in self.state["plan"].items()
            if k not in ("ok", "reason", "entities", "ports", "required_items", "underground_pairs")
        }
        self.state["plan"] = _plan(es, self.state["plan"]["ports"], **extra, underground_pairs=pairs)
        self.state["lab_join"] = {k: join[k] for k in ("name", "position", "direction", "unit_number")}
        self.state["pending_recovery"] = remove
        self.state["phase"] = "build"
        self.state.pop("lab_lane_evidence", None)
        self.state.pop("commissioned_lab_units", None)
        _atomic_json(self.path, self.state)
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        board["blocks"]["logistic_science"] = self.state["plan"]
        _atomic_json(path, board)
        return None

    def step(self, obs, parameters):
        validate_rate(parameters)
        if not self.state:
            prepared = self.prepare(obs, parameters)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            _atomic_json(self.path, self.state)
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "green_science_world_or_catalog_changed"}
        repair = self.repair_lab_join(obs)
        if repair:
            return repair
        for wanted in self.state.get("pending_recovery", []):
            current = next(
                (e for e in obs["entities"] if e["name"] == wanted["name"] and e["position"] == wanted["position"]),
                None,
            )
            if not current:
                continue
            if current["unit_number"] != wanted["unit_number"]:
                return {"status": "blocked", "reason": "green_lab_join_recovery_identity_changed"}
            return {
                "type": "mine",
                "name": current["name"],
                "position": current["position"],
                "count": 1,
                "expected_entity_unit": current["unit_number"],
                "expected_entity_world_id": obs["world_id"],
                "reason": "normally recover only the obsolete green lab entry hardware",
            }
        if self.state.pop("pending_recovery", None) is not None:
            _atomic_json(self.path, self.state)
        if parameters["rate_per_minute"] > self.state["requested_rate"]:
            return {"status": "blocked", "reason": "green_science_expansion_requires_new_row"}
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "green_science_board_world_changed"}
        if not self.state.get("published"):
            self.smelting.state["plan"] = self.state["source_plan"]
            _atomic_json(self.smelting.path, self.smelting.state)
            board["blocks"]["smelting_district"] = self.state["source_plan"]
            board["blocks"]["logistic_science"] = self.state["plan"]
            _atomic_json(board_path, board)
            self.state["published"] = True
            _atomic_json(self.path, self.state)
        if self.state["phase"] == "recover":
            for wanted in self.state["remove"]:
                current = next(
                    (e for e in obs["entities"] if e["name"] == wanted["name"] and e["position"] == wanted["position"]),
                    None,
                )
                if not current:
                    continue
                if current["unit_number"] != wanted["unit_number"]:
                    return {"status": "blocked", "reason": "green_plate_branch_recovery_identity_changed"}
                return {
                    "type": "mine",
                    "name": current["name"],
                    "position": current["position"],
                    "count": 1,
                    "expected_entity_unit": current["unit_number"],
                    "expected_entity_world_id": obs["world_id"],
                    "reason": "normally recover one owned plate belt for a paid shared-consumer splitter",
                }
            self.state["phase"] = "build"
            _atomic_json(self.path, self.state)
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs)
        if not proof:
            return {
                "status": "waiting",
                "reason": "waiting for native green intermediates, every science machine and automatic lab delivery",
            }
        self.state["phase"] = "verified"
        _atomic_json(self.path, self.state)
        return {
            "status": "succeeded",
            "reason": "normally belt-fed green science reaches the free lab belt lane",
            "evidence": proof,
        }

    def proof(self, obs):
        if (
            not self.state
            or self.state["world_id"] != obs["world_id"]
            or not underground_proof(obs, self.state["plan"])
        ):
            return None
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        machines = []
        for wanted in self.state["plan"]["entities"]:
            e = actual.get((wanted["name"], *xy(wanted)))
            if not e or e["direction"] != wanted.get("direction", 0):
                return None
            if wanted.get("recipe"):
                if e.get("recipe") != wanted["recipe"] or e.get("products_finished", 0) < 1:
                    return None
                machines.append(e)
            if e["name"] in ("assembling-machine-1", "inserter", "fast-inserter") and (
                not e.get("electric_network_connected") or e.get("energy", 0) <= 0
            ):
                return None
        join = self.state["lab_join"]
        live = actual.get((join["name"], *xy(join)))
        if not live or live["unit_number"] != join["unit_number"] or live["direction"] != join["direction"]:
            return None
        labs = [actual.get(("lab", *xy(e))) for e in self.science.state["labs"]["entities"] if e["name"] == "lab"]
        if any(not e or not e.get("electric_network_connected") or e.get("energy", 0) <= 0 for e in labs):
            return None
        green_stock = sum(e.get("inventory", {}).get("logistic-science-pack", 0) for e in labs)
        if not green_stock:
            return None
        lab_units = [e["unit_number"] for e in labs]
        if all(e.get("inventory", {}).get("logistic-science-pack", 0) > 0 for e in labs):
            self.state["commissioned_lab_units"] = lab_units
        if self.state.get("commissioned_lab_units") != lab_units:
            return None
        turn = plan_transform(self.science.state["labs"])
        heading = turn.direction(12)
        belts = [e for e in self.science.state["labs"]["entities"] if e["name"] == "transport-belt"]
        # Green side-loads onto the lab belt's left lane in the canonical
        # (westbound) frame; a mirrored lab row carries it on the right lane.
        green_lane, red_lane = ("right", "left") if turn.mirror else ("left", "right")
        payload = json.dumps(json.dumps({"join": join, "belts": belts, "heading": heading}, separators=(",", ":")))
        lane = self.game.query(
            """
local args=helpers.json_to_table("""
            + payload
            + """);local spec=args.join;local e=target(spec.position,spec.name)
if not e or e.unit_number~=spec.unit_number or e.direction~=spec.direction then return {ok=false} end
local result={ok=true,tick=game.tick,left={},right={}}
for _,b in ipairs(args.belts) do
 local belt=target(b.position,b.name);if not belt or belt.direction~=args.heading then return {ok=false} end
 for _,row in ipairs({{"left",defines.transport_line.left_line},{"right",defines.transport_line.right_line}}) do
  for _,v in pairs(belt.get_transport_line(row[2]).get_contents()) do result[row[1]][v.name]=(result[row[1]][v.name] or 0)+v.count end
 end
end
return result
"""
        )
        if not lane.get("ok") or not isinstance(lane.get(green_lane), dict):
            return None
        mixed = set(lane[green_lane]) - {"logistic-science-pack"}
        if mixed or set(lane.get(red_lane) or {}) - {"automation-science-pack"}:
            return None
        if lane[green_lane].get("logistic-science-pack", 0) > 0:
            self.state["lab_lane_evidence"] = dict(lane, receiver_unit=join["unit_number"])
            _atomic_json(self.path, self.state)
        recent = self.state.get("lab_lane_evidence")
        if (
            not recent
            or recent["receiver_unit"] != join["unit_number"]
            or not 0 <= lane["tick"] - recent["tick"] <= 600
        ):
            return None
        greens = [e for e in machines if e.get("recipe") == "logistic-science-pack"]
        return {
            "tick": obs["tick"],
            "science_units": [e["unit_number"] for e in greens],
            "machine_products_finished": [e["products_finished"] for e in greens],
            "intermediate_units": [
                {"unit": e["unit_number"], "recipe": e["recipe"], "products_finished": e["products_finished"]}
                for e in machines
                if e not in greens
            ],
            "lab_units": [e["unit_number"] for e in labs],
            "lab_green_stock": green_stock,
            "native_lab_lane_join": lane,
            "recent_green_lane_evidence": recent,
            "requested_rate_per_minute": self.state["requested_rate"],
            "nominal_machine_rate_per_minute": self.state["nominal_machine_rate"],
            "rated_raw_demand": self.state["rated_demand"]["raw_items_per_minute"],
            "scope": "native automatic production and lab commissioning; throughput limited by actual raw supply/power and requires a separate loaded window",
        }
