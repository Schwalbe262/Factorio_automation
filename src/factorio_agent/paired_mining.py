"""Opposing electric drill rows directly feed one shared collector belt."""

from __future__ import annotations

import json
import math
from pathlib import Path
from factorio_agent.core.deterministic_bootstrap import SURVEY_LAYOUT_TICKS
from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .transport import xy
from .smelting import port, merge_entities
from .planning import preflight, overlap
from .power_grid import plan_connection as power_connection
from .resource_policy import mark_mining_core
from .paired_smelting import key


def mining_pair(right_x, top_y, pairs, resource):
    left = right_x - 4 * (pairs - 1)
    es = []
    rows = {"left": [], "right": []}
    for lane, dy, direction in (("left", 0, 8), ("right", 4, 0)):
        for i in range(pairs):
            x = left + 4 * i
            drill = entity("electric-mining-drill", x, top_y + dy, direction)
            rows[lane].append(drill)
            es += [drill, entity("small-electric-pole", x + 2, top_y + dy)]
    es += [entity("transport-belt", left + dx, top_y + 2, 4) for dx in range(-1, 4 * (pairs - 1) + 3)]
    footprint = mark_mining_core(es, resource)
    output = dict(
        port(resource, right_x + 2, top_y + 2, 4, "output"),
        lane_items={"left": resource, "right": resource},
        mining_resource=resource,
    )
    return _plan(
        es,
        [output],
        resource_cell=True,
        resource=resource,
        drill_count=2 * pairs,
        pair_count=pairs,
        producer_rows=rows,
        paired=True,
        mining_footprint=footprint,
        layout_policy="pcb-paired-mining-v1",
    )


def resource_survey(game, plan, *, required_drills=None):
    required = None if required_drills is None else {(q["name"], *xy(q)) for q in required_drills}
    drills = [
        dict(q, require_coverage=required is None or (q["name"], *xy(q)) in required)
        for q in plan["entities"]
        if q["name"] == "electric-mining-drill"
    ]
    payload = json.dumps(json.dumps({"resource": plan["resource"], "drills": drills}, separators=(",", ":")))
    return game.query(
        """
local args=helpers.json_to_table("""
        + payload
        + """);local proto=prototypes.entity["electric-mining-drill"];local radius=proto.mining_drill_radius
local seen={};local amount=0;local unique=0
for _,drill in ipairs(args.drills) do
 local p=drill.position;local count=0;local available=0
 for _,ore in pairs(s.find_entities_filtered{area={{p.x-radius,p.y-radius},{p.x+radius,p.y+radius}},type="resource"}) do
  if ore.name~=args.resource then return {ok=false,reason="paired_mining_mixed_resource"} end
  count=count+1;available=available+ore.amount
  local key=ore.position.x..":"..ore.position.y
  if not seen[key] then seen[key]=true;unique=unique+1;amount=amount+ore.amount end
 end
 if drill.require_coverage and(count<4 or available<1800)then return {ok=false,reason="paired_mining_insufficient_resource",position=p} end
end
return {ok=true,unique_remaining_resource=amount,unique_resource_tiles=unique,
 rate_per_drill=proto.mining_speed*60/prototypes.entity[args.resource].mineable_properties.mining_time}
"""
    )


def retain_output(core, output):
    """A short two-turn collar preserves an installed downstream interface."""
    source = core["ports"][0]
    output = dict(output, lane_items=source["lane_items"], mining_resource=core["resource"])
    sx, sy = xy(source)
    tx, ty = xy(output)
    if (sx, sy) == (tx, ty):
        return dict(core, collector_port=source, collector_connection=[])
    if sx + 2 > tx or sy > ty:
        return None
    collar = [entity("transport-belt", sx, sy, 4)]
    if sy == ty:
        collar += [entity("transport-belt", sx + i, ty, 4) for i in range(1, int(tx - sx) + 1)]
    else:
        bend = sx + 1
        collar += [entity("transport-belt", bend, sy + i, 8) for i in range(int(ty - sy))]
        collar += [entity("transport-belt", bend + i, ty, 4) for i in range(int(tx - bend) + 1)]
    return dict(
        core,
        entities=merge_entities(core["entities"], collar),
        ports=[output],
        collector_port=source,
        collector_connection=collar,
    )


def lane_evidence(game, obs, bank):
    """Native proof that each producer row drops on its own collector lane.

    The collector port's ``facing`` (missing: east, the historical layout)
    gives the belt flow; "left" drills sit on the left of that flow (north
    of an east belt, east of a south belt) and must drop on the left lane.
    """
    from .depleted_drill_retirement import effective_mining_plan

    effective = effective_mining_plan(game, obs, bank["plan"])
    payload = json.dumps(json.dumps(effective, separators=(",", ":")))
    return game.query(
        """
local plan=helpers.json_to_table("""
        + payload
        + """);local collector=plan.collector_port or plan.ports[1];local facing=collector.facing or 4
local origin=collector.position;local nx,ny=0,-1
if facing==8 then nx,ny=1,0 elseif facing==12 then nx,ny=0,1 elseif facing==0 then nx,ny=-1,0 end
local rows={};for _,side in ipairs({"left","right"}) do
 local units={};for _,spec in ipairs(plan.producer_rows[side]) do
  local e=target(spec.position,"electric-mining-drill")
  if not e or e.direction~=spec.direction then return {ok=false,reason="paired_drill_missing_or_rotated",position=spec.position} end
  if not e.is_connected_to_electric_network() or e.energy<=0 then return {ok=false,reason="paired_drill_unpowered",position=spec.position} end
  local p=e.drop_position;local offset=(p.x-origin.x)*nx+(p.y-origin.y)*ny
  if math.abs(offset)>.49 or (side=="left" and offset<=0) or (side=="right" and offset>=0) then
   return {ok=false,reason="paired_drill_does_not_drop_on_declared_collector_lane"}
  end
  local cell={x=math.floor(p.x)+.5,y=origin.y}
  if nx~=0 then cell={x=origin.x,y=math.floor(p.y)+.5} end
  local receiver=target(cell,"transport-belt")
  if not receiver or receiver.direction~=facing then return {ok=false,reason="paired_drill_drop_receiver_missing"} end
  units[#units+1]={unit_number=e.unit_number,direction=e.direction,drop_position=pos(p)}
 end
 rows[side]=units
end
local lanes={{},{}}
for _,spec in ipairs(plan.entities) do if spec.name=="transport-belt" then
 local belt=target(spec.position,"transport-belt")
 if not belt or belt.direction~=spec.direction then return {ok=false,reason="paired_collector_missing_or_rotated"} end
 for index=1,2 do for _,item in pairs(belt.get_transport_line(index).get_contents()) do
  lanes[index][item.name]=(lanes[index][item.name] or 0)+item.count
 end end
end end
return {ok=true,tick=game.tick,producer_rows=rows,left=lanes[1],right=lanes[2]}
"""
    )


def prepare_extension(game, builder, obs, bank, added_drills):
    if bank.get("layout_contract") or bank["plan"].get("layout_contract"):
        from .mining_layout_policy import prepare_extension as prepare_policy_extension

        return prepare_policy_extension(game, builder, obs, bank, added_drills)
    old = bank["plan"]
    pairs = old["pair_count"] + math.ceil(added_drills / 2)
    if pairs > bank.get("reserved_pair_slots", old["pair_count"]):
        return {"ok": False, "reason": "paired_mining_reserved_columns_exhausted"}
    output = old.get("collector_port", old["ports"][0])["position"]
    core = mining_pair(output["x"] - 2, output["y"] - 2, pairs, old["resource"])
    core = retain_output(core, old["ports"][0])
    core["entities"] = merge_entities(old["entities"], core["entities"])
    core["underground_pairs"] = old.get("underground_pairs", [])
    from .depleted_drill_retirement import effective_mining_plan

    effective = effective_mining_plan(
        game, obs, core, catalog_fingerprint=getattr(getattr(builder, "catalog", None), "fingerprint", None)
    )
    native = preflight(game, builder, obs, effective["entities"])
    if not native.get("ok"):
        return native
    prior = {(q["name"], *xy(q)) for q in old["entities"]}
    fresh = [q for q in core["entities"] if q["name"] == "electric-mining-drill" and (q["name"], *xy(q)) not in prior]
    resources = resource_survey(game, effective, required_drills=fresh)
    if not resources.get("ok"):
        return resources
    extra = {k: v for k, v in core.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
    full = _plan(
        core["entities"],
        core["ports"],
        **extra,
        clear_obstacles=native["clear_obstacles"],
        clearance_world_id=obs["world_id"],
    )
    count = sum(q["name"] == "electric-mining-drill" for q in effective["entities"])
    return {
        "ok": True,
        "plan": full,
        "ready": False,
        "nominal_rate_per_minute": count * resources["rate_per_drill"],
        "reserved_pair_slots": bank["reserved_pair_slots"],
        "reserved_area": bank["reserved_area"],
        "remaining_resource_survey": resources["unique_remaining_resource"],
        "wire_hops": bank.get("wire_hops", 0),
        "consumer_connection": bank.get("consumer_connection"),
    }


def prepare_new_bank(game, builder, obs, resource, count, reservations, **options):
    """Plan declared grid/growth and a fixed off-resource outlet before acceptance.

    ``options`` (for example ``site_exit``) are passed on unchanged.
    """
    from .mining_layout_policy import prepare_new_bank as prepare_policy_bank

    return prepare_policy_bank(game, builder, obs, resource, count, reservations, **options)


class PairedMining:
    def __init__(self, game, bootstrap, builder, catalog, outposts, smelting):
        self.game, self.bootstrap, self.builder, self.catalog = game, bootstrap, builder, catalog
        self.outposts, self.smelting = outposts, smelting
        self.path = Path(game.cfg.runtime_dir) / "paired-mining.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def retires_legacy(self, e, obs):
        return self.state.get("world_id") == obs["world_id"] and any(
            e["name"] == w["name"] and e["position"] == w["position"] for w in self.state.get("legacy_cells", [])
        )

    def prepare(self, obs):
        old = self.outposts.state
        if old.get("world_id") != obs["world_id"] or not self.smelting.state.get("paired"):
            return {"ok": False, "reason": "paired_smelting_and_current_mining_required"}
        board = json.loads((self.path.parent / "pcb-board.json").read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "paired_mining_board_world_changed"}
        selected = [b for b in old["banks"] if b["plan"]["resource"] in ("iron-ore", "copper-ore")]
        specs = [e for b in selected for e in b["plan"]["entities"]]
        legacy = []
        for resource in ("iron-ore", "copper-ore"):
            survey = self.bootstrap._existing_cells(resource, max_age_ticks=SURVEY_LAYOUT_TICKS)
            if not survey.get("ok"):
                return survey
            for cell in survey["cells"]:
                if cell["drill"]["name"] != "burner-mining-drill":
                    continue
                for wanted in (cell["drill"], cell.get("receiver")):
                    if wanted is None:
                        continue
                    actual = next(
                        (
                            e
                            for e in obs["entities"]
                            if e["name"] == wanted["name"] and e["position"] == wanted["position"]
                        ),
                        None,
                    )
                    if actual:
                        legacy.append(actual)
        orphaned = self.game.query("""
local found={}
for _,e in pairs(s.find_entities_filtered{name="stone-furnace",force=f,position={0,0},radius=160}) do
 local ores=s.find_entities_filtered{area=e.bounding_box,type="resource"};local raw=false;local other=false
 for _,ore in pairs(ores) do
  if ore.name=="iron-ore" or ore.name=="copper-ore" then raw=true else other=true end
 end
 if raw and not other and #s.find_entities_filtered{position=e.position,radius=3,type="inserter",force=f}==0 then
  found[#found+1]=e.unit_number
 end
end
return {ok=true,units=found}
""")
        if not orphaned.get("ok"):
            return orphaned
        legacy_units = {e["unit_number"] for e in legacy} | set(orphaned["units"])
        legacy = [e for e in obs["entities"] if e.get("unit_number") in legacy_units]
        owned = specs + legacy
        wanted_keys = {key(e) for e in owned}
        ignored = {
            e["unit_number"] for e in obs["entities"] if key(e) in wanted_keys and e["name"] != "small-electric-pole"
        }
        banks = []
        obstacles = {}
        for resource in ("iron-ore", "copper-ore"):
            current = [b for b in selected if b["plan"]["resource"] == resource]
            primary = next(b for b in current if not b.get("consumer_connection"))
            output = primary["plan"]["ports"][0]["position"]
            right_x, top_y = output["x"] - 2, output["y"] - 2
            furnace = next(b for b in self.smelting.state["banks"] if b["resource"] == resource)
            capacity = (
                furnace["count"]
                * self.catalog.entities["stone-furnace"]["crafting_speed"]
                * 60
                / self.catalog.recipes[furnace["product"]]["energy"]
            )
            speed = self.game.query(
                'return {rate=prototypes.entity["electric-mining-drill"].mining_speed*60/prototypes.entity['
                + json.dumps(resource)
                + "].mineable_properties.mining_time}"
            )["rate"]
            old_rate = sum(b["nominal_rate_per_minute"] for b in current)
            pairs = math.ceil(max(old_rate, 0.9 * capacity) / (2 * speed))
            poles = [e for b in current for e in b["plan"]["entities"] if e["name"] == "small-electric-pole"]
            fixed = primary["plan"]["ports"][0]
            failure = {}
            for dx, dy in ((0, 0), (-2, 0), (-2, -2), (-2, -4), (-4, -2), (-4, -4)):
                candidate = retain_output(mining_pair(right_x + dx, top_y + dy, pairs, resource), fixed)
                candidate["entities"] = merge_entities(poles, candidate["entities"])
                native = preflight(self.game, self.builder, obs, candidate["entities"], ignored_units=ignored)
                if not native.get("ok"):
                    failure = native
                    continue
                coverage = resource_survey(self.game, candidate)
                if not coverage.get("ok"):
                    failure = coverage
                    continue
                core = candidate
                right_x += dx
                top_y += dy
                break
            else:
                return {"ok": False, "resource": resource, **failure}
            obstacles.update({(e["name"], *xy(e)): e for e in native["clear_obstacles"]})
            slots = pairs
            for candidate in range(pairs + 1, min(8, pairs + 2) + 1):
                future = retain_output(mining_pair(right_x, top_y, candidate, resource), fixed)
                future["entities"] = merge_entities(poles, future["entities"])
                if not preflight(self.game, self.builder, obs, future["entities"], ignored_units=ignored).get(
                    "ok"
                ) or not resource_survey(self.game, future).get("ok"):
                    break
                slots = candidate
            left = right_x - 4 * (slots - 1)
            area = [
                [left - 1.5, top_y - 1.5],
                [max(right_x + 2.5, output["x"] + 0.5), max(top_y + 5.5, output["y"] + 0.5)],
            ]
            if any(overlap(area, b["reserved_area"]) for b in board["blocks"].values()):
                return {
                    "ok": False,
                    "reason": "paired_mining_expansion_intrudes_registered_block",
                    "resource": resource,
                }
            extra = {k: v for k, v in core.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
            plan = _plan(core["entities"], core["ports"], **extra)
            banks.append(
                {
                    "ok": True,
                    "plan": plan,
                    "ready": False,
                    "nominal_rate_per_minute": 2 * pairs * speed,
                    "reserved_pair_slots": slots,
                    "reserved_area": area,
                    "remaining_resource_survey": coverage["unique_remaining_resource"],
                    "unique_resource_tiles": coverage["unique_resource_tiles"],
                    "wire_hops": 0,
                }
            )
        es = merge_entities(*(b["plan"]["entities"] for b in banks))
        final = preflight(self.game, self.builder, obs, es, ignored_units=ignored)
        if not final.get("ok"):
            return final
        obstacles.update({(e["name"], *xy(e)): e for e in final["clear_obstacles"]})
        new_keys = {key(e) for e in es}
        remove = [e for e in obs["entities"] if key(e) in wanted_keys and key(e) not in new_keys]
        remove.sort(key=lambda e: e["name"] not in ("electric-mining-drill", "burner-mining-drill"))
        return {
            "ok": True,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "banks": banks,
            "legacy_cells": legacy,
            "remove": remove,
            "phase": "recover",
            "old_mining": old,
            "plan": _plan(
                es,
                [b["plan"]["ports"][0] for b in banks],
                resource_cell=True,
                clear_obstacles=list(obstacles.values()),
                clearance_world_id=obs["world_id"],
                layout_policy="pcb-paired-mining-migration-v1",
            ),
        }

    def step(self, obs):
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            _atomic_json(self.path, self.state)
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "paired_mining_world_or_catalog_changed"}
        if self.state["phase"] == "verified":
            proof = self.proof(obs)
            return {
                "status": "succeeded" if proof else "waiting",
                "reason": "reverify current opposing mining banks",
                "evidence": proof or {},
            }
        if self.state["phase"] == "recover":
            for wanted in self.state["remove"]:
                current = next(
                    (e for e in obs["entities"] if e["name"] == wanted["name"] and e["position"] == wanted["position"]),
                    None,
                )
                if not current:
                    continue
                if current["unit_number"] != wanted["unit_number"]:
                    return {"status": "blocked", "reason": "paired_mining_recovery_identity_changed"}
                return {
                    "type": "mine",
                    "name": current["name"],
                    "position": current["position"],
                    "count": 1,
                    "expected_entity_unit": current["unit_number"],
                    "expected_entity_world_id": obs["world_id"],
                    "reason": "normally recover obsolete single-row ore banks and starter burners",
                }
            self.state["phase"] = "build"
            _atomic_json(self.path, self.state)
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        for bank in self.state["banks"]:
            proof = lane_evidence(self.game, obs, bank)
            if not proof.get("ok"):
                if proof.get("reason") != "paired_drill_unpowered":
                    return {"status": "blocked", **proof}
                pole = min(
                    (e for e in bank["plan"]["entities"] if e["name"] == "small-electric-pole"),
                    key=lambda e: math.dist(xy(e), xy(proof)),
                )
                bridge = power_connection(
                    self.game,
                    self.builder,
                    obs,
                    pole["position"],
                    [e for e in self.state["plan"]["entities"] if e["name"] != "small-electric-pole"],
                )
                if bridge.get("ok") and bridge["entities"]:
                    es = merge_entities(self.state["plan"]["entities"], bridge["entities"])
                    extra = {
                        k: v
                        for k, v in self.state["plan"].items()
                        if k not in ("ok", "reason", "entities", "ports", "required_items")
                    }
                    self.state["plan"] = _plan(es, self.state["plan"]["ports"], **extra)
                    _atomic_json(self.path, self.state)
                    return {"status": "waiting", "reason": "normal power connection reserved for opposing mining rows"}
                return {"status": "waiting", "reason": "waiting for powered opposing drill rows", "detail": proof}
        proof = self.proof(obs)
        if not proof:
            return {"status": "waiting", "reason": "waiting for native ore on both shared collector lanes"}
        self.publish(obs)
        return {
            "status": "succeeded",
            "reason": "opposing electric drill rows directly supply both lanes with fixed outputs and reserved expansion",
            "evidence": proof,
        }

    def publish(self, obs):
        for bank in self.state["banks"]:
            bank["ready"] = True
        keep = [b for b in self.outposts.state["banks"] if b["plan"]["resource"] not in ("iron-ore", "copper-ore")]
        self.outposts.state["banks"] = keep + self.state["banks"]
        _atomic_json(self.outposts.path, self.outposts.state)
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        for bank in self.state["banks"]:
            board["blocks"][bank["plan"]["resource"] + "_mining"] = dict(
                bank["plan"], reserved_area=bank["reserved_area"]
            )
        _atomic_json(path, board)
        self.state["phase"] = "verified"
        _atomic_json(self.path, self.state)

    def proof(self, obs):
        if not self.state or self.state["world_id"] != obs["world_id"]:
            return None
        banks = self.state["banks"]
        if self.state["phase"] == "verified":
            if self.outposts.state.get("world_id") != obs["world_id"]:
                return None
            banks = [b for b in self.outposts.state["banks"] if b["plan"]["resource"] in ("iron-ore", "copper-ore")]
            if not all(b["plan"].get("paired") for b in banks):
                return None
        if {b["plan"]["resource"] for b in banks} != {"iron-ore", "copper-ore"}:
            return None
        result = []
        for bank in banks:
            evidence = lane_evidence(self.game, obs, bank)
            item = bank["plan"]["resource"]
            from .depleted_drill_retirement import FILE as RETIREMENT_FILE

            current = None
            required = ("left", "right")
            nominal = bank["nominal_rate_per_minute"]
            if (self.path.parent / RETIREMENT_FILE).exists():
                current = self.outposts.bank_proof(obs, bank)
                if not current:
                    return None
                native = current.get("native_active_capacity")
                if not native:
                    return None
                required = tuple(side for side in ("left", "right") if native["active_offered_per_minute"][side] > 0)
                nominal = current["nominal_rate_per_minute"]
            if not evidence.get("ok") or any(
                not isinstance(evidence[side], dict)
                or set(evidence[side]) - {item}
                or (side in required and evidence[side].get(item, 0) < 1)
                for side in ("left", "right")
            ):
                return None
            result.append(
                {
                    "resource": item,
                    "nominal_rate_per_minute": nominal,
                    "historical_installed_nominal_per_minute": bank["nominal_rate_per_minute"],
                    "reserved_pair_slots": bank["reserved_pair_slots"],
                    "fixed_output": bank["plan"]["ports"][0],
                    "native_lane_evidence": evidence,
                    "unique_remaining_resource": bank["remaining_resource_survey"],
                    "active_source": current,
                    "required_productive_lanes": list(required),
                }
            )
        return {
            "tick": obs["tick"],
            "banks": result,
            "scope": "independent opposing native drill drops and ore on both collector lanes; nominal rates are not sustained throughput measurements",
        }
