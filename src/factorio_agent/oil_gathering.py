"""Normally paid, defended crude extraction and its natural research trigger."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
import re
from pathlib import Path

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.core.deterministic_state import _atomic_json
from . import plan_needs, remedy
from .board import entity
from .petroleum import refinery_origin
from .planning import overlap, preflight
from .power_grid import plan_connection as power_connection
from .resource_policy import mark_pumpjack
from .smelting import merge_entities
from .transport import VECTORS, xy

# Crude-field survey (rule, no fixed coordinates): wells are read from generated chunks
# around the refinery origin (petroleum.refinery_origin, the crude consumer) and tried
# nearest connection length first (Manhattan tiles: pipes run orthogonally), ring by ring,
# OIL_WELLS_PER_RING wells per ring. The planner's own maximum is OIL_SURVEY_RINGS[-1];
# the remedy rung ``site:wider_survey`` (option ``oil_survey_radius``) widens it in
# OIL_SURVEY_RING_STEP rings up to the hard maximum OIL_SURVEY_RADIUS_MAX.
OIL_SURVEY_RINGS = (256, 384, 512)
OIL_SURVEY_RING_STEP = 128
OIL_SURVEY_RADIUS_MAX = 1024
OIL_WELLS_PER_RING = 8
OIL_SURVEY_WELL_LIMIT = 256
# Scouting (paid ``move``): when nothing fits and ungenerated chunks lie inside the radius,
# the actor walks to a generated, enemy-free chunk within OIL_SCOUT_WINDOW_CHUNKS of the
# nearest one and waits up to OIL_SCOUT_GENERATE_WAIT_TICKS for it to generate; after
# OIL_SCOUT_MAX_TARGETS targets given up the survey blocks with its needs.
OIL_SCOUT_WINDOW_CHUNKS = 4
OIL_SCOUT_CANDIDATES = 16
OIL_SCOUT_ARRIVE_TILES = 12  # an assisted move lands within 8 tiles (find_non_colliding_position)
OIL_SCOUT_GENERATE_WAIT_TICKS = 1800
OIL_SCOUT_MAX_TARGETS = 24
# A stand is a tile the actor fits on, found within this many tiles of the chunk centre and inside
# the chunk (eval-1575138435, 320 min: the stand beside chunk 24,-6 was deep water, every move
# failed position_blocked and the job died repeated_executor_failure).
OIL_SCOUT_STAND_RADIUS = 16
# Moves sent toward one target without arriving before it is given up like an ungenerated one.
OIL_SCOUT_MOVES_PER_TARGET = 2


def survey_radius():
    """The planner's ring maximum, or the remedy's wider one, never above the hard maximum."""
    value = remedy.option("oil_survey_radius", OIL_SURVEY_RINGS[-1])
    if type(value) is not int:
        value = OIL_SURVEY_RINGS[-1]
    return max(OIL_SURVEY_RINGS[-1], min(OIL_SURVEY_RADIUS_MAX, value))


def survey_rings(radius):
    """Outer connection lengths of the survey rings, innermost first, the last one ``radius``."""
    rings = [r for r in OIL_SURVEY_RINGS if r <= radius]
    while rings[-1] + OIL_SURVEY_RING_STEP <= radius:
        rings.append(rings[-1] + OIL_SURVEY_RING_STEP)
    if rings[-1] < radius:
        rings.append(radius)
    return rings


OIL_SURVEY_LUA = r"""
local x=helpers.json_to_table(PAYLOAD);local c=x.center;local R=x.radius;local wells={}
for _,e in pairs(s.find_entities_filtered{position=c,radius=R,name="crude-oil"})do local d=math.abs(e.position.x-c.x)+math.abs(e.position.y-c.y)
 if e.amount>0 and d<=R then wells[#wells+1]={position=pos(e.position),amount=e.amount,distance=d}end end
table.sort(wells,function(p,q)if p.distance~=q.distance then return p.distance<q.distance end;if p.position.x~=q.position.x then return p.position.x<q.position.x end;return p.position.y<q.position.y end)
local selected={};for i=1,math.min(x.limit,#wells)do selected[#selected+1]=wells[i]end
local skip={};for _,k in ipairs(x.abandoned or{})do skip[k]=true end
local unknown=0;local targets={};local n=math.ceil(R/32)+1;local ox=math.floor(c.x/32);local oy=math.floor(c.y/32)
for cx=ox-n,ox+n do for cy=oy-n,oy+n do local d=math.abs(cx*32+16-c.x)+math.abs(cy*32+16-c.y)
 if d<=R and not s.is_chunk_generated({x=cx,y=cy})then unknown=unknown+1
  if not skip[cx..","..cy]then targets[#targets+1]={x=cx,y=cy,distance=d}end end
end end
table.sort(targets,function(p,q)if p.distance~=q.distance then return p.distance<q.distance end;if p.x~=q.x then return p.x<q.x end;return p.y<q.y end)
local scout=nil;local w=x.window
for i=1,math.min(x.candidates,#targets)do local t=targets[i];local best=nil;local bd=nil
 for gx=t.x-w,t.x+w do for gy=t.y-w,t.y+w do
  if s.is_chunk_generated({x=gx,y=gy})then local gd=(gx-t.x)^2+(gy-t.y)^2
   if(not bd or gd<bd)and s.count_entities_filtered{area={{gx*32-32,gy*32-32},{gx*32+64,gy*32+64}},force="enemy",limit=1}==0 then
    local land=s.find_non_colliding_position("character",{x=gx*32+16.5,y=gy*32+16.5},x.stand_radius,0.5)
    if land and math.abs(land.x-gx*32-16)<=16 and math.abs(land.y-gy*32-16)<=16 then best={x=gx,y=gy,stand=pos(land)};bd=gd end end end
 end end
 if best then scout={chunk={x=t.x,y=t.y},distance=t.distance,stand=best.stand};break end
end
return {ok=true,wells=selected,unknown_chunks=unknown,scout=scout}
"""


def oil_core(catalog, position, direction=0, power_offset=(0, 2), pipe_count=3):
    """Use the installed four rotated INTERNAL port positions plus facing."""
    if direction not in VECTORS or type(pipe_count) is not int or not 1 <= pipe_count <= 8:
        raise ValueError("oil core requires cardinal orientation and one to eight pipes")
    prototype = catalog.entities["pumpjack"]
    box = prototype.get("selection_box", {})
    if box.get("left_top") != {"x": -1.5, "y": -1.5} or box.get("right_bottom") != {"x": 1.5, "y": 1.5}:
        raise ValueError("installed pumpjack must have the supported three by three footprint")
    boxes = [b for b in prototype["fluidbox_prototypes"] if b.get("production_type") == "output"]
    if len(boxes) != 1 or boxes[0]["index"] != 1:
        raise ValueError("one native crude output fluidbox required")
    connections = [
        c
        for c in boxes[0]["pipe_connections"]
        if c.get("connection_type", "normal") == "normal" and c.get("flow_direction") == "output"
    ]
    if len(connections) != 1 or len(connections[0].get("positions", [])) != 4:
        raise ValueError("pumpjack must export four rotated native output positions")
    connection = connections[0]
    facing = (connection["direction"] + direction) % 16
    if facing not in VECTORS:
        raise ValueError("cardinal native fluid connection required")
    internal = connection["positions"][direction // 4]
    dx, dy = VECTORS[facing]
    x, y = position["x"], position["y"]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y)):
        raise ValueError("finite native oil well position required")
    pump = entity("pumpjack", x, y, direction)
    footprint = mark_pumpjack(pump)
    pipes = [
        entity("pipe", x + internal["x"] + dx * (i + 1), y + internal["y"] + dy * (i + 1)) for i in range(pipe_count)
    ]
    pole = entity("small-electric-pole", x + power_offset[0], y + power_offset[1])
    es = pipes + [pole, pump]
    geometry = FactoryBuilder.__new__(FactoryBuilder)
    cells = geometry._occupied_by_plan(es)
    area = [
        [min(a for a, b in cells) - 0.5, min(b for a, b in cells) - 0.5],
        [max(a for a, b in cells) + 0.5, max(b for a, b in cells) + 0.5],
    ]
    port = {
        "kind": "fluid",
        "item": "crude-oil",
        "direction": "output",
        "facing": facing,
        "name": "pipe",
        "position": deepcopy(pipes[-1]["position"]),
        "fluidbox_index": 1,
    }
    return _plan(
        es,
        [port],
        pumpjack=pump,
        pipes=pipes,
        power_port=pole,
        resource="crude-oil",
        mining_footprint=footprint,
        mining_reserved_area=footprint,
        reserved_area=area,
        layout_policy="native-defended-crude-well-v1",
    )


def _xy(well):
    return (well["position"]["x"], well["position"]["y"])


def _key(e):
    return e["name"] + ":" + str(e["position"]["x"]) + ":" + str(e["position"]["y"])


class OilGathering:
    def __init__(self, game, builder, catalog, perimeter, *, source_key="crude-oil"):
        if not isinstance(source_key, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", source_key):
            raise ValueError("oil source key must be a bounded registry name")
        if source_key in ("water", "petroleum-gas", "heavy-oil", "light-oil", "sulfuric-acid", "lubricant"):
            raise ValueError("oil source key cannot replace another canonical fluid")
        self.game, self.builder, self.catalog, self.perimeter = game, builder, catalog, perimeter
        self.source_key = source_key
        self.block_id = (
            "oil_gathering" if source_key == "crude-oil" else "oil_gathering_" + source_key.replace("-", "_")
        )
        filename = "oil-gathering.json" if source_key == "crude-oil" else "oil-gathering-" + source_key + ".json"
        self.path = Path(game.cfg.runtime_dir) / filename
        self.sources_path = self.path.parent / "fluid-sources.json"
        self.scout_path = self.path.parent / (
            "oil-scout.json" if source_key == "crude-oil" else "oil-scout-" + source_key + ".json"
        )
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if self.state and source_key != "crude-oil" and self.state.get("source_key") != source_key:
            raise ValueError("independent oil source checkpoint belongs to another profile")

    def _identity(self, obs, value):
        return (
            obs.get("ok", True)
            and isinstance(obs.get("world_id"), str)
            and bool(obs["world_id"])
            and value.get("world_id") == obs["world_id"]
            and value.get("catalog_fingerprint") == self.catalog.fingerprint
        )

    def prepare(self, obs):
        """Read-only: at most eight wells, four rotations and four service poles."""
        if not obs.get("ok", True) or not isinstance(obs.get("world_id"), str) or not obs["world_id"]:
            return {"ok": False, "reason": "oil_gathering_requires_valid_world_observation"}
        if not obs.get("technologies", {}).get("oil-gathering") or not obs.get("enabled_recipes", {}).get("pumpjack"):
            return {"ok": False, "reason": "natural_oil_gathering_and_enabled_pumpjack_required"}
        trigger = self.catalog.technologies.get("oil-processing", {}).get("research_trigger", {})
        if trigger.get("type") != "mine-entity" or trigger.get("entities") != ["crude-oil"]:
            return {"ok": False, "reason": "installed_oil_processing_crude_extraction_trigger_required"}
        if self.state:
            return (
                deepcopy(self.state)
                if self._identity(obs, self.state)
                else {"ok": False, "reason": "oil_gathering_world_or_catalog_changed"}
            )
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
        if not self._identity(obs, board):
            return {"ok": False, "reason": "oil_gathering_board_identity_changed"}
        if self.source_key != "crude-oil" and self.sources_path.exists():
            registry = json.loads(self.sources_path.read_text(encoding="utf-8"))
            if not self._identity(obs, registry):
                return {"ok": False, "reason": "fluid_source_registry_identity_changed"}
            if registry.get("sources", {}).get(self.source_key):
                return {"ok": False, "reason": "oil_source_exists_without_intake_checkpoint"}
        reserved = [b["reserved_area"] for b in board.get("blocks", {}).values() if b.get("reserved_area")]
        reserved += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", []) if r.get("area")]
        # Connection length is measured to the crude consumer: the refinery site petroleum seeks.
        ox, oy = refinery_origin(board)
        center = {"x": ox, "y": oy}
        radius = survey_radius()
        abandoned = self._scout_state(obs).get("abandoned", [])
        args = {
            "center": center,
            "radius": radius,
            "limit": OIL_SURVEY_WELL_LIMIT,
            "abandoned": abandoned,
            "window": OIL_SCOUT_WINDOW_CHUNKS,
            "candidates": OIL_SCOUT_CANDIDATES,
            "stand_radius": OIL_SCOUT_STAND_RADIUS,
        }
        query = OIL_SURVEY_LUA.replace("PAYLOAD", json.dumps(json.dumps(args, separators=(",", ":"))))
        if self.source_key != "crude-oil":
            query = query.replace(
                "if e.amount>0 and",
                'if e.amount>0 and s.count_entities_filtered{position=e.position,radius=.1,name="pumpjack"}==0 and',
            )
        wells = self.game.query(query)
        if not wells.get("ok"):
            return wells
        rows = wells.get("wells", [])
        failures = []
        seen = set()
        for outer in survey_rings(radius):
            ring = [w for w in rows if _xy(w) not in seen and w.get("distance", 0) <= outer][:OIL_WELLS_PER_RING]
            for well in ring:
                seen.add(_xy(well))
                found = self._candidate(obs, well, reserved, failures)
                if found is not None:
                    return found
        survey = {
            "center": center,
            "radius": radius,
            "wells_known": len(rows),
            "wells_tried": len(seen),
            "unknown_chunks": wells.get("unknown_chunks", 0),
            "scouts_abandoned": len(abandoned),
        }
        scout = wells.get("scout")
        if isinstance(scout, dict) and len(abandoned) < OIL_SCOUT_MAX_TARGETS:
            # Not a plan and not a dead end: the field may lie in ungenerated area the actor can explore.
            chunk = scout.get("chunk") or {}
            return {
                "status": "waiting",
                "reason": "crude_field_survey_needs_scouting",
                "scout": scout,
                "survey": survey,
                "wait_for": "chunk (%s,%s) generated by the scouting actor: no crude-oil field fits within %d tiles"
                % (chunk.get("x"), chunk.get("y"), radius),
            }
        if not rows:
            reason = "no_crude_field_within_survey_radius"
            change = "a crude-oil field within %d tiles (charted)" % radius
        else:
            reason = "no_defended_crude_well_with_native_outlet_and_power"
            change = "a crude-oil well within %d tiles (charted) with a clear outlet, power and defense" % radius
        if survey["unknown_chunks"]:
            change += "; %d chunks unexplored, scouting exhausted" % survey["unknown_chunks"]
        return {
            "ok": False,
            "reason": reason,
            "candidate_failures": failures[-16:],
            "survey": survey,
            "plan_needs": plan_needs.make(
                "site",
                reason,
                item="crude-oil",
                at=center,
                limit="survey radius %d<=%d" % (radius, OIL_SURVEY_RADIUS_MAX),
                tried=len(seen),
                rejected_by=Counter(str(f) for f in failures),
                change=change,
            ),
        }

    def _candidate(self, obs, well, reserved, failures):
        """The first fitting rotation / service pole of one well (a plan or an error), else None."""
        for direction in VECTORS:
            for offset in ((0, 2), (-2, 0), (2, 0), (0, -2)):
                try:
                    core = oil_core(self.catalog, well["position"], direction, offset)
                except (ValueError, KeyError) as exc:
                    return {"ok": False, "reason": "unsupported_native_pumpjack_geometry", "detail": str(exc)}
                if any(overlap(core["reserved_area"], r) for r in reserved):
                    continue
                check = preflight(self.game, self.builder, obs, core["entities"])
                if not check.get("ok"):
                    failures.append(check.get("reason"))
                    continue
                payload = json.dumps(json.dumps(core["entities"], separators=(",", ":")))
                isolated = self.game.query(
                    """local specs=helpers.json_to_table("""
                    + payload
                    + """)
for _,spec in ipairs(specs)do if spec.name=="pipe"then local p=spec.position
 for _,other in pairs(s.find_entities_filtered{area={{p.x-1.49,p.y-1.49},{p.x+1.49,p.y+1.49}},force=f})do
  for _ in pairs(other.prototype.fluidbox_prototypes or {})do return {ok=false,reason="oil_outlet_near_existing_fluid_network"}end
 end end end;return {ok=true}"""
                )
                if not isolated.get("ok"):
                    failures.append(isolated.get("reason"))
                    continue
                power = power_connection(
                    self.game,
                    self.builder,
                    obs,
                    core["power_port"]["position"],
                    [e for e in core["entities"] if e["name"] != "small-electric-pole"],
                )
                if not power.get("ok"):
                    failures.append(power.get("reason"))
                    continue
                es = merge_entities(power["entities"], core["entities"])
                check = preflight(self.game, self.builder, obs, es)
                if not check.get("ok"):
                    failures.append(check.get("reason"))
                    continue
                scope = (
                    "oil-"
                    + hashlib.sha256(
                        json.dumps([obs["world_id"], well["position"], core["ports"]], sort_keys=True).encode()
                    ).hexdigest()[:20]
                )
                defense = self.perimeter.prepare_coverage(obs, scope, [core["reserved_area"]])
                if not defense.get("ok"):
                    return {"ok": False, "reason": "oil_well_defense_preflight_failed", "defense": defense}
                plan = _plan(
                    es,
                    core["ports"],
                    reserved_area=core["reserved_area"],
                    resource="crude-oil",
                    mining_reserved_area=core["mining_reserved_area"],
                    clear_obstacles=check["clear_obstacles"],
                    clearance_world_id=obs["world_id"],
                    layout_policy="native-defended-crude-well-v1",
                )
                actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
                bindings = {
                    _key(e): actual[(e["name"], *xy(e))]["unit_number"] for e in es if (e["name"], *xy(e)) in actual
                }
                return {
                    "ok": True,
                    "world_id": obs["world_id"],
                    "catalog_fingerprint": self.catalog.fingerprint,
                    "phase": "build",
                    "prepared_tick": obs.get("tick"),
                    "plan": plan,
                    "core": core,
                    "well": well,
                    "defense_scope_id": scope,
                    "defense_preview": defense,
                    "bindings": bindings,
                    "wire_hops": power["wire_hops"],
                    **({"source_key": self.source_key} if self.source_key != "crude-oil" else {}),
                }
        return None

    def _scout_state(self, obs):
        """Read-only: this world's scouting record (targets given up), else empty."""
        if not self.scout_path.exists():
            return {}
        state = json.loads(self.scout_path.read_text(encoding="utf-8"))
        return state if state.get("world_id") == obs.get("world_id") else {}

    def _scout(self, obs, prepared):
        """Paid exploration: walk the actor beside the nearest ungenerated chunk, then wait for it.

        Generation follows the actor (a connected player; client-free: ``emulate_player_presence``).
        A target still ungenerated ``OIL_SCOUT_GENERATE_WAIT_TICKS`` after arrival is given up; after
        ``OIL_SCOUT_MAX_TARGETS`` such targets ``prepare`` blocks with its needs.
        """
        scout = prepared["scout"]
        chunk, stand = scout.get("chunk"), scout.get("stand")
        actor = obs.get("position")
        if not all(isinstance(v, dict) for v in (chunk, stand, actor)):
            return {"status": "blocked", "reason": "oil_scout_requires_actor_and_target_positions"}
        key = "%d,%d" % (chunk["x"], chunk["y"])
        state = self._scout_state(obs) or {"world_id": obs["world_id"], "abandoned": []}
        distance = math.hypot(actor["x"] - stand["x"], actor["y"] - stand["y"])
        if distance > OIL_SCOUT_ARRIVE_TILES:
            last = state.get("distance")
            if state.get("target") != key or (isinstance(last, (int, float)) and distance < last - 1):
                # A new target, or the last move brought the actor closer (a walk in progress).
                state.update(target=key, arrived_tick=None, moves=0)
            elif int(state.get("moves") or 0) >= OIL_SCOUT_MOVES_PER_TARGET:
                # The moves did not get the actor there (blocked or unreachable stand): next target.
                state["abandoned"] = (state.get("abandoned", []) + [key])[-OIL_SCOUT_MAX_TARGETS:]
                state.update(target=None, arrived_tick=None, moves=0, distance=None)
                _atomic_json(self.scout_path, state)
                return {
                    "status": "waiting",
                    "reason": "oil_scout_target_unreachable",
                    "chunk": key,
                    "wait_for": "the next scouting target",
                }
            state["moves"] = int(state.get("moves") or 0) + 1
            state["distance"] = round(distance, 1)
            _atomic_json(self.scout_path, state)
            return {
                "type": "move",
                "position": deepcopy(stand),
                "reason": "scout beside ungenerated chunk %s for a crude-oil field" % key,
            }
        tick = obs.get("tick") if type(obs.get("tick")) is int else 0
        if state.get("target") != key or type(state.get("arrived_tick")) is not int:
            state.update(target=key, arrived_tick=tick)
            _atomic_json(self.scout_path, state)
        elif tick - state["arrived_tick"] >= OIL_SCOUT_GENERATE_WAIT_TICKS:
            state["abandoned"] = (state.get("abandoned", []) + [key])[-OIL_SCOUT_MAX_TARGETS:]
            state.update(target=None, arrived_tick=None)
            _atomic_json(self.scout_path, state)
            return {
                "status": "waiting",
                "reason": "oil_scout_target_stayed_ungenerated",
                "chunk": key,
                "wait_for": "the next scouting target",
            }
        return {"status": "waiting", "reason": prepared["reason"], "chunk": key, "wait_for": prepared["wait_for"]}

    def _save(self):
        _atomic_json(self.path, self.state)

    def _bind(self, obs):
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        changed = False
        for spec in self.state["plan"]["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            expected = self.state["bindings"].get(_key(spec))
            if expected and (not live or live.get("unit_number") != expected):
                return {
                    "status": "blocked",
                    "reason": "owned_oil_asset_missing_or_replaced",
                    "entity": spec["name"],
                    "position": spec["position"],
                }
            if live:
                if type(live.get("unit_number")) is not int or (
                    spec["name"] == "pumpjack" and live.get("direction") != spec["direction"]
                ):
                    return {"status": "blocked", "reason": "oil_asset_identity_or_direction_changed"}
                if expected is None:
                    self.state["bindings"][_key(spec)] = live["unit_number"]
                    changed = True
        if changed:
            self._save()
        return None

    def _reserve(self, obs):
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        if not self._identity(obs, board):
            return {"status": "blocked", "reason": "oil_gathering_board_identity_changed"}
        name = self.block_id
        area = self.state["core"]["reserved_area"]
        old = board["blocks"].get(name)
        if old and old.get("defense_scope_id") != self.state["defense_scope_id"]:
            return {"status": "blocked", "reason": "oil_gathering_reservation_identity_changed"}
        if old and any(
            old.get(k) != self.state["plan"].get(k)
            for k in ("entities", "ports", "reserved_area", "mining_reserved_area")
        ):
            return {"status": "blocked", "reason": "oil_gathering_reservation_changed"}
        if any(
            k != name and overlap(area, b["reserved_area"])
            for k, b in board["blocks"].items()
            if b.get("reserved_area")
        ):
            return {"status": "blocked", "reason": "oil_gathering_reservation_conflict"}
        board["blocks"][name] = dict(self.state["plan"], defense_scope_id=self.state["defense_scope_id"])
        _atomic_json(path, board)
        return None

    def step(self, obs):
        if not self.state:
            prepared = self.prepare(obs)
            if prepared.get("scout"):
                return self._scout(obs, prepared)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        if not self._identity(obs, self.state):
            return {"status": "blocked", "reason": "oil_gathering_world_or_catalog_changed"}
        result = self._bind(obs)
        if result:
            return result
        result = self._reserve(obs)
        if result:
            return result
        result = self.perimeter.ensure_coverage(
            obs, self.state["defense_scope_id"], [self.state["core"]["reserved_area"]]
        )
        if result is not None:
            return result
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        evidence = self.proof(obs)
        if not evidence:
            return {
                "status": "waiting",
                "reason": "waiting for exact powered pumpjack, connected native crude and natural oil-processing research",
            }
        registry = (
            json.loads(self.sources_path.read_text(encoding="utf-8"))
            if self.sources_path.exists()
            else {"world_id": obs["world_id"], "catalog_fingerprint": self.catalog.fingerprint, "sources": {}}
        )
        if not self._identity(obs, registry):
            return {"status": "blocked", "reason": "fluid_source_registry_identity_changed"}
        old = registry["sources"].get(self.source_key)
        if old and (
            old.get("unit_number") != evidence["source"]["unit_number"]
            or old.get("defense_scope_id") != self.state["defense_scope_id"]
        ):
            return {"status": "blocked", "reason": "published_crude_output_identity_changed"}
        registry["sources"][self.source_key] = evidence["source"]
        _atomic_json(self.sources_path, registry)
        self.state.update(phase="verified", proof=evidence)
        self._save()
        return {
            "status": "succeeded",
            "reason": "native crude extraction naturally unlocked oil processing",
            "evidence": evidence,
        }

    def proof(self, obs):
        if not self.state or not self._identity(obs, self.state):
            return None
        defense = self.perimeter.coverage_proof(obs, self.state["defense_scope_id"])
        if not defense:
            return None
        chain = [self.state["core"]["pumpjack"]] + self.state["core"]["pipes"]
        if any(_key(e) not in self.state["bindings"] for e in chain):
            return None
        payload = json.dumps(
            json.dumps(
                {
                    "chain": [dict(e, unit_number=self.state["bindings"][_key(e)]) for e in chain],
                    "well": self.state["well"]["position"],
                },
                separators=(",", ":"),
            )
        )
        result = self.game.query(
            """local args=helpers.json_to_table("""
            + payload
            + """);local previous=nil;local rows={}
local well=target(args.well,"crude-oil");if not well or well.amount<=0 or well.position.x~=args.well.x or well.position.y~=args.well.y then return {ok=false,reason="native_crude_well_missing"}end
for index,spec in ipairs(args.chain)do local e=target(spec.position,spec.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=spec.unit_number or e.position.x~=spec.position.x or e.position.y~=spec.position.y then return {ok=false,reason="native_oil_asset_identity_changed"}end
 if index==1 and(e.direction~=spec.direction or not e.is_connected_to_electric_network()or e.energy<=0)then return {ok=false,reason="pumpjack_not_normally_powered"}end
 if previous then local connected=false;for _,peer in pairs(previous.get_fluid_box_neighbours(1)or{})do if peer.entity==e and peer.index==1 then connected=true end end
  local reverse=false;for _,peer in pairs(e.get_fluid_box_neighbours(1)or{})do if peer.entity==previous and peer.index==1 then reverse=true end end
  if not connected or not reverse then return {ok=false,reason="native_crude_pipe_peer_missing"}end end
 local fluids=e.get_fluid_contents();for name in pairs(fluids)do if name~="crude-oil"then return {ok=false,reason="crude_output_contaminated"}end end
 rows[#rows+1]={name=e.name,unit_number=e.unit_number,position=pos(e.position),crude=fluids["crude-oil"]or 0};previous=e
end
if rows[#rows].crude<=0 or not f.technologies["oil-processing"].researched then return {ok=false,reason="native_crude_and_natural_research_required"}end
return {ok=true,tick=game.tick,chain=rows,oil_processing_researched=true}"""
        )
        if not result.get("ok"):
            return None
        source = dict(
            self.state["plan"]["ports"][0],
            unit_number=result["chain"][-1]["unit_number"],
            world_id=obs["world_id"],
            defense_scope_id=self.state["defense_scope_id"],
        )
        return {
            "tick": result["tick"],
            "source": source,
            "native": result,
            "defense_coverage": defense,
            "scope": "paid native extraction and research trigger; local finite pipe buffer, no sustained-throughput claim",
        }
