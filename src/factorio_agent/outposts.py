"""Additive electric mining banks with aligned collectors and typed output ports."""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .power_grid import plan_connection
from .transport import VECTORS, xy, plan_connection as belt_connection, underground_proof
from .planning import overlap, preflight
from .resource_policy import mark_mining_core, mining_reservations


RESOURCES = {"iron-ore", "copper-ore", "coal", "stone"}


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
    def __init__(self, game, bootstrap, builder, *, perimeter=None):
        self.game, self.bootstrap, self.builder = game, bootstrap, builder
        self.perimeter = perimeter
        self.path = Path(game.cfg.runtime_dir) / "mining-outposts.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @staticmethod
    def find(obs, e):
        return next((a for a in obs["entities"] if a["name"] == e["name"] and a["position"] == e["position"]), None)

    def publish_reservations(self, obs):
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
        for bank in banks:
            declared = bank.get("pending_extension", bank)
            resource = bank["plan"]["resource"]
            index = counts.get(resource, 0)
            counts[resource] = index + 1
            name = resource + "_mining" + ("_" + str(index) if index else "")
            area = declared["reserved_area"]
            if any(key != name and overlap(area, b["reserved_area"]) for key, b in board["blocks"].items()):
                return {
                    "status": "blocked",
                    "reason": "paired_mining_reserved_area_conflicts_with_registered_block",
                    "block": name,
                }
            extra = {}
            for field in ("mining_reserved_area", "layout_contract"):
                if declared.get(field) is not None:
                    extra[field] = declared[field]
            board["blocks"][name] = dict(declared["plan"], reserved_area=area, **extra)
        _atomic_json(path, board)
        return None

    def prepare(self, obs, resource, count, *, extra_reservations=()):
        from .paired_mining import prepare_new_bank

        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
        if board and board.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "mining_planning_board_world_changed"}
        reservations = [b["reserved_area"] for b in board.get("blocks", {}).values()]
        reservations += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", []) if r.get("area")]
        reservations += [b["reserved_area"] for b in self.state.get("banks", []) if b.get("reserved_area")]
        reservations += list(extra_reservations)
        return prepare_new_bank(self.game, self.builder, obs, resource, count, reservations)

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

    def prepare_connected_bank(self, obs, primary, count):
        """Feed a paid extra module into an identity-bound same-item trunk T."""
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

            traced = trace_corridor(consumer["plan"], primary["plan"]["ports"][0], destination)
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
            source = extra["plan"]["ports"][0]
            ranked = sorted(
                targets, key=lambda row: sum(abs(a - b) for a, b in zip(xy(source), xy(row["destination"])))
            )
            probes = [ranked[i] for i in sorted({0, len(ranked) // 2, len(ranked) - 1})] if ranked else []
            for row in probes:
                destination, target = row["destination"], row["target"]
                attempts += 1
                route = belt_connection(self.game, self.builder, obs, source, destination, extra["plan"]["entities"])
                if not route.get("ok"):
                    failures.append(route.get("reason"))
                    if route.get("reason") in {
                        "no_safe_bounded_mining_exit",
                        "mining_exit_requires_owned_source",
                        "prospective_mining_core_native_preflight_failed",
                    }:
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
                check = preflight(self.game, self.builder, obs, plan["entities"])
                if not check.get("ok"):
                    failures.append(check.get("reason"))
                    continue
                plan.update(clear_obstacles=check["clear_obstacles"], clearance_world_id=obs["world_id"])
                extra["plan"] = plan
                extra["consumer_connection"] = {
                    "kind": "same-item perpendicular side-load",
                    "item": primary["plan"]["resource"],
                    "target": target,
                    "entry_port": destination,
                    "route_metrics": route["route_metrics"],
                }
                extra["planning_attempts"] = {"sites": site_index + 1, "routes": attempts}
                return extra
            if not extra.get("reserved_area"):
                break
            rejected.append(extra["reserved_area"])
        return {
            "ok": False,
            "reason": "no_clean_extra_bank_connection_to_owned_ore_trunk",
            "candidates": failures,
            "planning_attempts": {"sites": len(rejected), "routes": attempts},
        }

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

    def step(self, obs, parameters):
        validate_parameters(parameters)
        if not obs.get("technologies", {}).get("electric-mining-drill"):
            return {"status": "blocked", "reason": "electric_mining_research_required"}
        if not self.state:
            self.state = {"world_id": obs["world_id"], "banks": []}
        if self.state["world_id"] != obs["world_id"]:
            return {"status": "blocked", "reason": "outpost_world_changed"}
        reservation = self.publish_reservations(obs)
        if reservation:
            return reservation
        resource, target = parameters["resource"], parameters["rate_per_minute"]
        banks = [b for b in self.state["banks"] if b["plan"]["resource"] == resource]
        for bank in banks:
            from .depleted_drill_retirement import effective_mining_plan

            try:
                effective = effective_mining_plan(getattr(self, "game", None), obs, bank["plan"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return {"status": "blocked", "reason": "mining_retirement_active_plan_invalid", "detail": str(exc)}
            if bank.get("ready") and any(self.find(obs, e) is None for e in effective["entities"]):
                return {"status": "blocked", "reason": "existing_outpost_damaged_requires_repair_before_expansion"}
            if bank.get("ready") and self.bank_proof(obs, bank) is None:
                return {"status": "waiting", "reason": "restore existing outpost power/output before adding capacity"}
        active_proofs = [(b, self.bank_proof(obs, b)) for b in banks if b.get("ready")]
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
            bank = active[0]
            extension = bank.get("pending_extension")
            if extension is None:
                count = sum(e["name"] == "electric-mining-drill" for e in bank["plan"]["entities"])
                per_drill = bank["nominal_rate_per_minute"] / count
                added = min(8, math.ceil((target - rate) / per_drill))
                extension = self.prepare_extension(obs, bank, added)
                if not extension.get("ok"):
                    connected = self.prepare_connected_bank(obs, bank, added)
                    if not connected.get("ok"):
                        return {"status": "blocked", **connected, "upstream_extension_failure": extension.get("reason")}
                    self.state["banks"].append(connected)
                    _atomic_json(self.path, self.state)
                    return {
                        "status": "waiting",
                        "reason": "reserved extra mining module and same-item trunk connection",
                    }
                bank["pending_extension"] = extension
                _atomic_json(self.path, self.state)
            reservation = self.publish_reservations(obs)
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
            count = min(8, math.ceil((target - rate) / metadata["rate"]))
            pending = self.prepare(obs, resource, count)
            if not pending.get("ok"):
                return {"status": "blocked", "reason": pending.get("reason", "mining_bank_preflight_failed")}
            self.state["banks"].append(pending)
            _atomic_json(self.path, self.state)
        reservation = self.publish_reservations(obs)
        if reservation:
            return reservation
        coverage = self._construction_coverage(obs, pending)
        if coverage is not None:
            return coverage
        result = self.builder.ensure_plan(obs, pending["plan"])
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
            if b.get("ready") and b["plan"]["resource"] == parameters["resource"]
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
