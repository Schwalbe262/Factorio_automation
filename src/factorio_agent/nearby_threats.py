"""Bounded charted-Nauvis threat observation; no combat or construction effects."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from .actor_client import DETACHED_ACTOR_LUA


PRODUCTIVE_NAMES = frozenset(
    {
        "burner-mining-drill",
        "electric-mining-drill",
        "stone-furnace",
        "steel-furnace",
        "electric-furnace",
        "assembling-machine-1",
        "assembling-machine-2",
        "assembling-machine-3",
        "chemical-plant",
        "oil-refinery",
        "pumpjack",
        "lab",
        "boiler",
        "steam-engine",
        "offshore-pump",
    }
)
MAX_REFERENCES = 256
MAX_CHUNKS = 256


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def validate_survey(maximum_distance, maximum_entities, danger_distance):
    if (
        type(maximum_distance) not in (int, float)
        or not math.isfinite(maximum_distance)
        or not 1 <= maximum_distance <= 96
        or type(maximum_entities) is not int
        or not 1 <= maximum_entities <= 128
        or type(danger_distance) not in (int, float)
        or not math.isfinite(danger_distance)
        or not 0 < danger_distance <= maximum_distance
    ):
        raise ValueError("survey requires distance1..96, entity cap1..128 and positive danger distance within survey")


def factory_reference_specs(board, obs, catalog):
    """Registered productive buildings only; military posts can never move this origin."""
    if board.get("world_id") != obs.get("world_id") or board.get("catalog_fingerprint") != catalog.fingerprint:
        raise ValueError("nearby_threats_board_world_or_catalog_changed")
    actual = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in obs.get("entities", [])}
    found = {}
    for block, value in sorted(board.get("blocks", {}).items()):
        for e in value.get("entities", []):
            if e.get("name") not in PRODUCTIVE_NAMES:
                continue
            p = e.get("position", {})
            if set(p) != {"x", "y"} or any(type(v) not in (int, float) or not math.isfinite(v) for v in p.values()):
                raise ValueError("nearby_threats_invalid_productive_reference")
            key = e["name"], p["x"], p["y"]
            live = actual.get(key)
            spec = {"name": e["name"], "position": dict(p), "registered_block": block}
            if live is not None:
                if type(live.get("unit_number")) is not int or live["unit_number"] < 1:
                    continue
                spec["expected_unit"] = live["unit_number"]
            found.setdefault(key, spec)
    if not found:
        raise ValueError("nearby_threats_original_productive_factory_reference_required")
    if len(found) > MAX_REFERENCES:
        raise ValueError("nearby_threats_productive_reference_budget_exceeded")
    return list(found.values())


def classify_report(report, danger_distance=48):
    """Range/armor comparisons; damage figures are bounds, not combat simulation."""
    result = deepcopy(report)
    gun = result.get("gun", {})
    ammo = result.get("ammo", {})
    mod = result.get("force_modifiers", {})
    raw = None
    values = [ammo.get("physical_damage"), gun.get("damage_modifier"), mod.get("ammo_damage"), mod.get("turret_damage")]
    if all(type(v) in (int, float) and math.isfinite(v) for v in values):
        raw = values[0] * values[1] * (1 + values[2]) * (1 + values[3])
    targets = []
    for q in result.get("targets", []):
        q = deepcopy(q)
        kind = q.get("type")
        enemy_range = q.get("combat", {}).get("range")
        q["role"] = "spawner" if kind == "unit-spawner" else ("worm" if kind == "turret" else "mobile")
        q["dangerously_close"] = q["factory_distance"] <= danger_distance
        native_range = gun.get("range")
        q["gun_range_advantage"] = (
            (native_range - enemy_range)
            if type(native_range) in (int, float) and type(enemy_range) in (int, float)
            else None
        )
        q["range_disadvantage"] = q["role"] == "worm" and (
            q["gun_range_advantage"] is None or q["gun_range_advantage"] <= 0
        )
        resistance = q.get("combat", {}).get("resistances", {}).get("physical", {})
        decrease, percent = resistance.get("decrease", 0), resistance.get("percent", 0)
        lower = None
        # LuaPrototype resistance.percent is a FRACTION (native .15 = 15%).
        if (
            raw is not None
            and all(type(v) in (int, float) and math.isfinite(v) for v in (decrease, percent))
            and 0 <= percent <= 1
            and decrease >= 0
        ):
            lower = max(0, raw - decrease) * (1 - percent)
        q["estimated_physical_damage_per_round_lower_bound"] = lower
        q["armor_warning"] = (
            "unresolved_native_damage_or_resistance"
            if lower is None
            else ("raw_damage_at_or_below_flat_armor" if lower <= 0 else None)
        )
        if lower and type(q.get("health")) in (int, float):
            q["static_health_rounds_bound_without_healing"] = math.ceil(q["health"] / lower)
        targets.append(q)
    targets.sort(key=lambda q: (not q["dangerously_close"], q["factory_distance"], q["unit_number"]))
    result.update(
        targets=targets,
        danger_distance=danger_distance,
        raw_physical_damage_per_round=raw,
        dangerously_close_units=[q["unit_number"] for q in targets if q["dangerously_close"]],
        stationary_targets=[q["unit_number"] for q in targets if q["role"] in ("spawner", "worm")],
        scope="charted native snapshot relative to original productive buildings; no kill, survivability or sustained-fire certification",
    )
    return result


def attack_contract(report, catalog):
    """A finite ordinary-cost proposal only. It deliberately emits no action."""
    stationary = [q for q in report.get("targets", []) if q.get("role") in ("spawner", "worm")]
    close = [q for q in stationary if q.get("dangerously_close")]
    primary = min(close, key=lambda q: (q["factory_distance"], q["unit_number"])) if close else None
    cluster = [
        q
        for q in stationary
        if primary
        and math.dist((q["position"]["x"], q["position"]["y"]), (primary["position"]["x"], primary["position"]["y"]))
        <= 48
    ]
    cluster.sort(key=lambda q: (q["factory_distance"], q["unit_number"]))
    reasons = []
    if report.get("truncated"):
        reasons.append("complete_local_enemy_survey_required")
    if not close:
        reasons.append("no_dangerously_close_charted_stationary_target")
    if len(cluster) > 12:
        reasons.append("stationary_cluster_exceeds_twelve_target_budget")
    if any(q.get("range_disadvantage") for q in cluster):
        reasons.append("worms_outrange_guns_explicit_under_fire_damage_budget_required")
    if any(q.get("armor_warning") for q in cluster):
        reasons.append("native_ammo_or_armor_feasibility_unresolved")
    if not report.get("ammo", {}).get("magazine_size"):
        reasons.append("native_normal_magazine_prototype_required")
    if not (
        report.get("actor", {}).get("character_controller")
        or report.get("actor", {}).get("remote_character_controller")
        or report.get("actor", {}).get("detached_character")
    ):
        reasons.append("ordinary_owned_character_required_before_combat")
    # The site planner and actual-combat executor are intentionally not part of
    # this survey module. Existing defense inventories are never raid supplies.
    requirements = {"gun-turret": 11, "firearm-magazine": 220, "stone-wall": 72, "repair-pack": 1}
    snapshot_rounds = (
        sum(q["static_health_rounds_bound_without_healing"] for q in cluster)
        if all(type(q.get("static_health_rounds_bound_without_healing")) is int for q in cluster)
        else None
    )
    size = report.get("ammo", {}).get("magazine_size")
    rounds = 220 * size if type(size) is int and size > 0 else None
    for name in requirements:
        if name not in catalog.recipes:
            reasons.append("ordinary_recipe_unavailable:" + name)
        if report.get("recipes_enabled") and not report["recipes_enabled"].get(name):
            reasons.append("native_recipe_required:" + name)
    return {
        "kind": "nearby_nest_bastion_proposal",
        "ready_for_attack": False,
        "target_identities": [
            {k: deepcopy(q[k]) for k in ("name", "unit_number", "position", "world_id", "surface_index", "force_index")}
            for q in cluster[:12]
        ],
        "primary_target_unit": primary["unit_number"] if primary else None,
        "stationary_cluster_count": len(cluster),
        "maximum_stages": 3,
        "guns_per_stage": 3,
        "spare_guns": 2,
        "initial_full_normal_magazines_per_gun": 20,
        "requirements": requirements,
        "maximum_game_seconds": 180,
        "maximum_post_losses": 2,
        "stationary_snapshot_rounds_without_healing": snapshot_rounds,
        "prestock_round_budget": rounds,
        "stationary_only_round_margin": rounds - snapshot_rounds
        if rounds is not None and snapshot_rounds is not None
        else None,
        "maximum_targets": 12,
        "blockers": reasons
        + [
            "native_whole_site_service_resource_and_damage_preflight_required",
            "paid_projectile_executor_not_implemented",
        ],
        "staging": "prestocks all normal guns/full magazines/repair packs before first stage; retain all existing defensive ammo",
        "placement": "each new armed group requires at least two mutually covering guns, protected PCB/mining/ports and an open rear service lane",
        "stop_conditions": "no damage progress after ten seconds of an armed firing opportunity, two post losses, or180game seconds; retain defensive rear line",
        "proof": "exact initial enemy identities plus actual native death/cause and spent-round evidence; absence alone does not establish a projectile kill",
        "scope": "bounded proposal; no placement coordinates, gameplay action or predicted victory",
    }


class NearbyThreats:
    def __init__(self, game, catalog):
        self.game, self.catalog = game, catalog

    def survey(self, obs, *, maximum_distance=96, maximum_entities=128, danger_distance=48):
        validate_survey(maximum_distance, maximum_entities, danger_distance)
        if not obs.get("ok", True) or not isinstance(obs.get("world_id"), str) or not obs["world_id"]:
            return {"ok": False, "reason": "nearby_threats_observed_world_required"}
        try:
            board = json.loads((Path(self.game.cfg.runtime_dir) / "pcb-board.json").read_text(encoding="utf-8"))
            references = factory_reference_specs(board, obs, self.catalog)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "reason": "nearby_threats_factory_context_unavailable", "detail": str(exc)}
        args = {
            "world_id": obs["world_id"],
            "references": references,
            "maximum_distance": maximum_distance,
            "maximum_entities": maximum_entities,
            "maximum_chunks": MAX_CHUNKS,
        }
        result = self.game.query(NEARBY_THREATS_SURVEY_LUA.replace("PAYLOAD", _payload(args)))
        if not result.get("ok"):
            return result
        if (
            result.get("world_id") != obs["world_id"]
            or result.get("surface_name") != "nauvis"
            or len(result.get("targets", [])) > maximum_entities
            or any(
                type(q.get("unit_number")) is not int
                or q["unit_number"] < 1
                or q.get("world_id") != obs["world_id"]
                or q.get("surface_index") != result.get("surface_index")
                or q.get("force_index") != result.get("enemy_force_index")
                or q.get("charted") is not True
                or q.get("type") not in ("unit", "unit-spawner", "turret")
                or type(q.get("factory_distance")) not in (int, float)
                or not math.isfinite(q["factory_distance"])
                or not 0 <= q["factory_distance"] <= maximum_distance
                for q in result.get("targets", [])
            )
        ):
            return {"ok": False, "reason": "nearby_threats_native_survey_receipt_invalid"}
        result["catalog_fingerprint"] = self.catalog.fingerprint
        result["survey_id"] = hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return classify_report(result, danger_distance)

    def prepare(self, obs):
        report = self.survey(obs)
        return (
            report
            if not report.get("ok")
            else {"ok": True, "survey": report, "proposal": attack_contract(report, self.catalog)}
        )

    def preflight_bastions(self, obs, stages, *, backstop_position):
        """Read-only review of at most nine positions; never an attack action."""
        from .defense_hardening import wall_bastion, SERVICE_ACCESS_LUA, _box
        from .defense_perimeter import protected_factory_areas
        from .resource_policy import mining_reservations
        from .planning import overlap

        def valid(p):
            return (
                isinstance(p, dict)
                and set(p) == {"x", "y"}
                and all(type(v) in (int, float) and math.isfinite(v) and v % 1 == 0 for v in p.values())
            )

        if (
            not isinstance(stages, list)
            or not 1 <= len(stages) <= 3
            or any(not isinstance(s, list) or len(s) != 3 or any(not valid(p) for p in s) for s in stages)
            or not valid(backstop_position)
        ):
            raise ValueError("one to three three-gun integer bastions and an integer rear support position required")
        positions = [p for stage in stages for p in stage]
        points = positions + [backstop_position]
        bounds = [
            [math.floor(min(p[k] for p in points)) - 4 for k in ("x", "y")],
            [math.ceil(max(p[k] for p in points)) + 4 for k in ("x", "y")],
        ]
        if (bounds[1][0] - bounds[0][0] + 1) * (bounds[1][1] - bounds[0][1] + 1) > 4096 or max(
            bounds[1][i] - bounds[0][i] for i in (0, 1)
        ) > 96:
            return {"ok": False, "reason": "nearby_threats_local_service_budget_exceeded"}
        try:
            root = Path(self.game.cfg.runtime_dir)
            board = json.loads((root / "pcb-board.json").read_text(encoding="utf-8"))
            if (
                board.get("world_id") != obs.get("world_id")
                or board.get("catalog_fingerprint") != self.catalog.fingerprint
            ):
                return {"ok": False, "reason": "nearby_threats_candidate_board_identity_changed"}
            protected = protected_factory_areas(self.game, obs["world_id"], self.catalog.fingerprint)
            reserved = [b["reserved_area"] for b in board["blocks"].values() if b.get("reserved_area")]
            reserved += [r["area"] for r in mining_reservations(self.game, obs["world_id"])]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "reason": "nearby_threats_candidate_context_unavailable", "detail": str(exc)}

        def near(area):
            return (
                area[0][0] < bounds[1][0] + 1
                and area[1][0] > bounds[0][0]
                and area[0][1] < bounds[1][1] + 1
                and area[1][1] > bounds[0][1]
            )

        bastions = [dict(wall_bastion(p, 4), stage=i + 1) for i, stage in enumerate(stages) for p in stage]
        entities = [e for b in bastions for e in b["entities"]]
        if any(overlap(_box(e), _box(q)) for i, e in enumerate(entities) for q in entities[i + 1 :]):
            return {"ok": False, "reason": "nearby_threats_prospective_bastion_overlap"}
        args = {
            "world_id": obs["world_id"],
            "bastions": bastions,
            "entities": [e for b in bastions for e in b["entities"]],
            "protected": [r for r in protected if near(r)],
            "reserved": [r for r in reserved if near(r)],
            "bounds": bounds,
            "hotspot": backstop_position,
        }
        body = BASTION_PREFLIGHT_LUA.replace("PAYLOAD", _payload(args)).replace("SERVICE_BODY", SERVICE_ACCESS_LUA)
        return self.game.query(body)


NEARBY_THREATS_SURVEY_LUA = (
    DETACHED_ACTOR_LUA
    + r"""
local args=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=args.world_id then return {ok=false,reason="nearby_threats_native_world_changed"}end
if s.name~="nauvis"then return {ok=false,reason="nearby_threats_charted_nauvis_only"}end
local enemy=game.forces.enemy
if not enemy then return {ok=false,reason="nearby_threats_enemy_force_missing"}end
local function optional(fn)local ok,value=pcall(fn);if ok then return value end end
local function combat(proto)
 local attack=optional(function()return proto.attack_parameters end)or{};local resist={}
 for key,row in pairs(optional(function()return proto.resistances end)or{})do
  local name=type(key)=="string"and key or row.type
  if name then resist[name]={decrease=row.decrease or 0,percent=row.percent or 0}end
 end
 return {range=attack.range,range_mode=attack.range_mode,min_range=attack.min_range,cooldown=attack.cooldown,
  damage_modifier=attack.damage_modifier,resistances=resist}
end
local references={};local missing=0
for _,q in ipairs(args.references)do local e=target(q.position,q.name)
 if e and e.valid and e.force==f and e.surface==s and e.unit_number and e.position.x==q.position.x and e.position.y==q.position.y
 and(not q.expected_unit or q.expected_unit==e.unit_number)then
  references[#references+1]={name=e.name,position=e.position,unit_number=e.unit_number,registered_block=q.registered_block,
   world_id=d.world_id,surface_index=s.index,force_index=f.index}
 else missing=missing+1 end
end
if #references==0 then return {ok=false,reason="nearby_threats_no_confirmed_original_factory_assets"}end
local chunks={};local radius=args.maximum_distance
for _,q in ipairs(references)do local p=q.position
 for cx=math.floor((p.x-radius)/32),math.floor((p.x+radius)/32)do
  for cy=math.floor((p.y-radius)/32),math.floor((p.y+radius)/32)do
   local dx=math.max(cx*32-p.x,0,p.x-(cx+1)*32);local dy=math.max(cy*32-p.y,0,p.y-(cy+1)*32)
   if dx*dx+dy*dy<=radius*radius then local key=cx..":"..cy;chunks[key]={x=cx,y=cy}end
  end
 end
end
local chunk_list={};for _,q in pairs(chunks)do chunk_list[#chunk_list+1]=q end
if #chunk_list>args.maximum_chunks then return {ok=false,reason="nearby_threats_chunk_budget_exceeded",chunks=#chunk_list}end
table.sort(chunk_list,function(a,b)return a.x==b.x and a.y<b.y or a.x<b.x end)
local targets={};local guns={};local seen={};local seen_guns={};local charted=0;local truncated=false
local hostile=not f.get_friend(enemy)and not f.get_cease_fire(enemy)
local function nearest(p)local best=nil;local ref=nil
 for _,q in ipairs(references)do local value=(p.x-q.position.x)^2+(p.y-q.position.y)^2
  if not best or value<best then best=value;ref=q end end
 return math.sqrt(best),ref
end
local function magazines(inv)
 local full,rounds,partial=0,0,0;local by_item={}
 if inv then for i=1,#inv do local t=inv[i]
  if t.valid_for_read and(t.name=="firearm-magazine"or t.name=="piercing-rounds-magazine")and t.quality.name=="normal"
   and t.health==1 and t.spoil_tick==0 and not t.is_item_with_tags and not t.is_item_with_inventory and not t.is_item_with_entity_data then
   local size=t.prototype.magazine_size;local used=t.ammo<size and 1 or 0
   local row=by_item[t.name]or{full_normal_magazines=0,normal_rounds=0,partial_normal_magazines=0};by_item[t.name]=row
   row.full_normal_magazines=row.full_normal_magazines+t.count-used;row.partial_normal_magazines=row.partial_normal_magazines+used
   row.normal_rounds=row.normal_rounds+(t.count-1)*size+t.ammo
   if t.name=="firearm-magazine"then full=full+t.count-used;partial=partial+used;rounds=rounds+(t.count-1)*size+t.ammo end
  end
 end end
 return {full_normal_magazines=full,normal_rounds=rounds,partial_normal_magazines=partial,by_item=by_item}
end
for _,chunk in ipairs(chunk_list)do
 if f.is_chunk_charted(s,chunk)then charted=charted+1
  local box={{chunk.x*32,chunk.y*32},{(chunk.x+1)*32-.0001,(chunk.y+1)*32-.0001}}
  if hostile then
   local found=s.find_entities_filtered{area=box,force=enemy,type={"unit","unit-spawner","turret"},limit=args.maximum_entities+1}
   if #found>args.maximum_entities then truncated=true end
   for _,e in ipairs(found)do if e.valid and e.unit_number and not seen[e.unit_number]then seen[e.unit_number]=true
    local distance,reference=nearest(e.position)
    if distance<=radius then
     if #targets>=args.maximum_entities then truncated=true else
      targets[#targets+1]={name=e.name,type=e.type,unit_number=e.unit_number,position=e.position,
       world_id=d.world_id,surface_index=s.index,force_index=e.force.index,force_name=e.force.name,
       health=e.health,max_health=e.max_health,factory_distance=distance,nearest_factory_unit=reference.unit_number,
       nearest_factory_name=reference.name,nearest_factory_position=reference.position,combat=combat(e.prototype),charted=true}
     end
    end
   end end
  end
  local friendly=s.find_entities_filtered{area=box,force=f,name="gun-turret",limit=args.maximum_entities+1}
  if #friendly>args.maximum_entities then truncated=true end
  for _,e in ipairs(friendly)do if e.valid and e.unit_number and not seen_guns[e.unit_number]then seen_guns[e.unit_number]=true
   if nearest(e.position)<=radius then
    if #guns>=args.maximum_entities then truncated=true else
     guns[#guns+1]={name=e.name,position=e.position,unit_number=e.unit_number,world_id=d.world_id,
      surface_index=s.index,force_index=f.index,health=e.health,max_health=e.max_health,quality=e.quality.name,
      ammo=magazines(e.get_inventory(defines.inventory.turret_ammo))}
    end
   end
  end end
 end
end
local gun_proto=prototypes.entity["gun-turret"];local gun=combat(gun_proto)
gun.max_health=optional(function()return gun_proto.get_max_health("normal")end)or optional(function()return gun_proto.max_health end)
gun.requires_power=not not optional(function()return gun_proto.electric_energy_source_prototype end)
local function ammunition(name)
 local item=prototypes.item[name];local ammo_type=optional(function()return item.get_ammo_type("turret")end)
 if not ammo_type then ammo_type=optional(function()return item.get_ammo_type()end)end
 local damage=0;local nodes=0;local visited={}
 local function walk(value,depth)
  if type(value)~="table"or depth>8 or visited[value]or nodes>=128 then return end;visited[value]=true;nodes=nodes+1
  if value.type=="damage"and value.damage and value.damage.type=="physical"then damage=damage+value.damage.amount end
  for _,v in pairs(value)do if type(v)=="table"then walk(v,depth+1)end end
 end
 if ammo_type then walk(ammo_type,0)end
 return {name=name,magazine_size=item and item.magazine_size,physical_damage=ammo_type and damage or nil,
  category=optional(function()return item.ammo_category.name end),ammo_type_available=not not ammo_type,
  recipe_enabled=f.recipes[name]and f.recipes[name].enabled or false}
end
local ammo=ammunition("firearm-magazine");local category=ammo.category
local recipes={};for _,name in ipairs({"gun-turret","stone-wall","repair-pack","firearm-magazine","piercing-rounds-magazine"})do
 recipes[name]=f.recipes[name]and f.recipes[name].enabled or false end
local modifiers={ammo_damage=category and optional(function()return f.get_ammo_damage_modifier(category)end),
 gun_speed=category and optional(function()return f.get_gun_speed_modifier(category)end),
 turret_damage=optional(function()return f.get_turret_attack_modifier("gun-turret")end)}
local actor={};if a and a.valid then
 actor={unit_number=a.unit_number,position=a.position,health=a.health,max_health=a.max_health,
  main_ammo=magazines(a.get_main_inventory()),equipped_ammo=magazines(a.get_inventory(defines.inventory.character_ammo)),
  controller_type=a.player and a.player.controller_type,character_controller=a.player and a.player.controller_type==defines.controllers.character,
  remote_character_controller=a.player and a.player.controller_type==defines.controllers.remote and a.player.connected
   and a.player.name=="FactoryAutomaton"and a.player.character==a and d.crafting_player_index==a.player.index
   and d.crafting_actor_unit_number==a.unit_number,
  detached_character=detached_actor()}
end
return {ok=true,tick=game.tick,world_id=d.world_id,surface_name=s.name,surface_index=s.index,
 force_index=f.index,force_name=f.name,enemy_force_index=enemy.index,enemy_force_name=enemy.name,hostile=hostile,
 evolution_factor=optional(function()return enemy.get_evolution_factor(s)end),references=references,missing_references=missing,
 candidate_chunks=#chunk_list,charted_chunks=charted,truncated=truncated,targets=targets,friendly_guns=guns,
 gun=gun,ammo=ammo,ammo_options={ammo,ammunition("piercing-rounds-magazine")},recipes_enabled=recipes,force_modifiers=modifiers,actor=actor}
"""
)


BASTION_PREFLIGHT_LUA = r"""
local x=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=x.world_id or s.name~="nauvis"then return {ok=false,reason="nearby_threats_candidate_world_or_surface_changed"}end
local function overlaps(a,b)return a[1][1]<b[2][1]and a[2][1]>b[1][1]and a[1][2]<b[2][2]and a[2][2]>b[1][2]end
local function charted(box)
 for cx=math.floor(box[1][1]/32),math.floor((box[2][1]-.0001)/32)do
  for cy=math.floor(box[1][2]/32),math.floor((box[2][2]-.0001)/32)do
   if not f.is_chunk_charted(s,{x=cx,y=cy})then return false end
  end
 end;return true
end
local enemy=game.forces.enemy;local rows={};local guns={};local seen={}
local gun_range=prototypes.entity["gun-turret"].attack_parameters.range;local coverage=gun_range-2
if coverage<=0 then return {ok=false,reason="nearby_threats_native_gun_range_missing"}end
local region={{x.bounds[1][1]-gun_range,x.bounds[1][2]-gun_range},{x.bounds[2][1]+gun_range,x.bounds[2][2]+gun_range}}
for cx=math.floor(region[1][1]/32),math.floor(region[2][1]/32)do
 for cy=math.floor(region[1][2]/32),math.floor(region[2][2]/32)do
  local box={{cx*32,cy*32},{(cx+1)*32-.0001,(cy+1)*32-.0001}}
  if charted(box)then for _,e in ipairs(s.find_entities_filtered{area=box,force=f,name="gun-turret",limit=129})do
   if e.unit_number and not seen[e.unit_number]then seen[e.unit_number]=true
    local stock=0;local inv=e.get_inventory(defines.inventory.turret_ammo)
    if inv then for i=1,#inv do local t=inv[i];if t.valid_for_read and t.name=="firearm-magazine"and t.quality.name=="normal"then stock=stock+t.count-(t.ammo<t.prototype.magazine_size and 1 or 0)end end end
    local priorities={};local ok,value=pcall(function()return e.priority_targets end)
    if ok then for _,p in pairs(value or{})do priorities[#priorities+1]=p.name end end
    local ignored=nil;pcall(function()ignored=e.ignore_unprioritised_targets end)
    if #guns>=128 then return {ok=false,reason="nearby_threats_candidate_support_budget_exceeded"}end
    guns[#guns+1]={unit_number=e.unit_number,position=e.position,health=e.health,max_health=e.max_health,
      full_normal_magazines=stock,priority_api_available=ok,priority_targets=priorities,ignore_unprioritised_targets=ignored}
   end
  end end
 end
end
for _,q in ipairs(x.bastions)do local row={position=q.position,stage=q.stage,currently_clear=true,blockers={},enemy_collisions={},obstacles={},rear_cover=0}
 local seen_foes={};local seen_obstacles={}
 for _,e in ipairs(q.entities)do local h=e.name=="gun-turret"and 1 or .5;local p=e.position;local box={{p.x-h,p.y-h},{p.x+h,p.y+h}}
  if not charted(box)then row.currently_clear=false;row.blockers[#row.blockers+1]="uncharted_footprint"else
   for _,area in ipairs(x.protected)do if overlaps(box,area)then row.currently_clear=false;row.blockers[#row.blockers+1]="protected_factory_or_future_hardware";break end end
   if s.count_entities_filtered{area=box,type="resource"}>0 then row.currently_clear=false;row.blockers[#row.blockers+1]="resource_patch"end
   if not s.can_place_entity{name=e.name,position=p,direction=0,force=f}then
    row.currently_clear=false;local foes=s.find_entities_filtered{area=box,force=enemy,type={"unit","unit-spawner","turret"},limit=16}
    if #foes==0 then
     row.blockers[#row.blockers+1]="current_structure_or_terrain"
     for _,ob in ipairs(s.find_entities_filtered{area=box,limit=16})do
      local key=ob.name..":"..ob.position.x..":"..ob.position.y
      if not seen_obstacles[key]then seen_obstacles[key]=true;row.obstacles[#row.obstacles+1]={name=ob.name,type=ob.type,position=ob.position,
       unit_number=ob.unit_number,force_index=ob.force.index}end
     end
    end
    for _,foe in ipairs(foes)do if not seen_foes[foe.unit_number]then seen_foes[foe.unit_number]=true
     row.enemy_collisions[#row.enemy_collisions+1]={name=foe.name,type=foe.type,position=foe.position,
      unit_number=foe.unit_number,health=foe.health,conditional_after_actual_death=foe.type~="unit"}end end
   end
  end
 end
 for _,g in ipairs(guns)do if g.health==g.max_health and g.full_normal_magazines>=10
  and(g.position.x-q.position.x)^2+(g.position.y-q.position.y)^2<=coverage^2 then row.rear_cover=row.rear_cover+1 end end
 row.future_rear_cover=0
 for _,prior in ipairs(x.bastions)do if prior.stage==q.stage-1 and(prior.position.x-q.position.x)^2+(prior.position.y-q.position.y)^2<=coverage^2 then row.future_rear_cover=row.future_rear_cover+1 end end
 rows[#rows+1]=row
end
local function service()
SERVICE_BODY
end
local access=charted({x.bounds[1],{x.bounds[2][1]+1,x.bounds[2][2]+1}})and service()or {ok=false,reason="nearby_threats_service_area_uncharted"}
return {ok=true,ready_for_attack=false,tick=game.tick,world_id=d.world_id,surface_index=s.index,force_index=f.index,
 sites=rows,current_support_guns=guns,service=access,gun_range=gun_range,mutual_coverage_radius=coverage,
 scope="nine-site charted native placement/ore/PCB and local current walkability only; future enemy collisions are not buildable yet"}
"""
