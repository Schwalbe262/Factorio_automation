"""Migrate an unfinished starter coal feed to a validated PCB-style trunk.

Old paid hardware is recovered through normal, identity-guarded mining. A hot
operating plant requires a future parallel cutover capability and is rejected.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from factorio_ai.deterministic_state import _atomic_json
from .routing import route_trunks


def identity(entity):
    return (entity["name"], entity["position"]["x"], entity["position"]["y"], entity.get("direction",0))


def compact_coal_cell(position):
    x,y = position["x"],position["y"]
    drill = {"name":"burner-mining-drill","position":dict(position),"direction":4}
    entities = [drill]
    for dx,dy,direction in ((1.5,-.5,4),(2.5,-.5,8),(2.5,.5,8),(2.5,1.5,4)):
        entities.append({"name":"transport-belt","position":{"x":x+dx,"y":y+dy},"direction":direction})
    # Direction is the pickup side: east pickup, west drop into the drill.
    entities.append({"name":"burner-inserter","position":{"x":x+1.5,"y":y+.5},"direction":4})
    return _plan(entities,[{"kind":"item","item":"coal","direction":"output","facing":4,
                          "position":{"x":x+2.5,"y":y+1.5}}],drill=drill,template="compact_starter_coal")


class V2FactoryBuilder(FactoryBuilder):
    """Use fixed trunk routing and compact starter cells for future v2 plans."""
    _coal_plan=staticmethod(compact_coal_cell)

    def plan_route(self,name,source,destination,**kwargs):
        if name=="transport-belt":
            return route_trunks(source,destination,**kwargs)
        return super().plan_route(name,source,destination,**kwargs)

    def water_sites(self,radius=192,limit=24):
        sites=super().water_sites(radius=radius,limit=max(limit,128))
        coal=self.coal_sites()
        if coal:
            sites.sort(key=lambda site: min(abs(site["position"]["x"]-c["x"])+abs(site["position"]["y"]-c["y"])
                                           for c in coal)+.2*site["distance"]**.5)
        return sites[:limit]


class RegularizePower:
    def __init__(self, game, builder):
        self.game,self.builder=game,builder
        self.path=Path(game.cfg.runtime_dir)/"power-layout-migration.json"
        self.state=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def prepare(self,obs):
        self.builder._sync(obs)
        saved=self.builder.state
        if not saved.get("power_plan") or not saved.get("coal_plan"):
            return {"ok":False,"reason":"no_existing_power_and_coal_plan"}
        if saved.get("power_verified_once") or any(s.get("observed") for s in saved.get("seeds",{}).values()):
            return {"ok":False,"reason":"operating_supply_requires_parallel_cutover"}
        old=deepcopy(saved["coal_plan"]);power=saved["power_plan"]
        cell=compact_coal_cell(old["drill"]["position"])
        source=cell["ports"][0];destination=next(p for p in power["ports"] if p.get("item")=="coal")
        old_keys={identity(e) for e in old["entities"]};power_keys={identity(e) for e in power["entities"]}
        ignored=[e["unit_number"] for e in obs["entities"] if identity(e) in old_keys-power_keys
                 and e["name"]!="burner-mining-drill"]
        reusable=[e["unit_number"] for e in obs["entities"] if identity(e) in power_keys
                  and e["position"]==destination["position"] and e["name"]=="transport-belt"]
        a,b=source["position"],destination["position"]
        bounds={"min_x":min(a["x"],b["x"])-24,"max_x":max(a["x"],b["x"])+24,
                "min_y":min(a["y"],b["y"])-24,"max_y":max(a["y"],b["y"])+24}
        args=json.dumps(json.dumps({"bounds":bounds,"ignored":ignored+reusable},separators=(",",":")))
        survey=self.game.query('''
local args=helpers.json_to_table('''+args+''');local ignored={};for _,id in ipairs(args.ignored) do ignored[id]=true end
local blocked={};local clearable={};local b=args.bounds
for x=b.min_x,b.max_x,1 do for y=b.min_y,b.max_y,1 do
 local p={x=x,y=y};local legal=s.can_place_entity{name="transport-belt",position=p,force=f,
  build_check_type=defines.build_check_type.manual_ghost,forced=true}
 local entities=s.find_entities_filtered{area={{x-.4,y-.4},{x+.4,y+.4}}}
 if not legal then
  local replacing_owned=false;for _,e in pairs(entities) do if ignored[e.unit_number] then replacing_owned=true end end
  if replacing_owned then
   legal=true;local tile_mask=s.get_tile(p).prototype.collision_mask.layers
   for layer in pairs(prototypes.entity["transport-belt"].collision_mask.layers) do
    if tile_mask[layer] then legal=false;break end
   end
  end
 end
 local natural={}
 if legal then
  for _,e in pairs(entities) do
   if e~=a and e.type~="resource" and not ignored[e.unit_number] and not e.prototype.collision_mask.colliding_with_tiles_only then
    local rock=e.type=="simple-entity" and (e.name=="big-rock" or e.name=="huge-rock" or e.name=="big-sand-rock")
    if e.force.name=="neutral" and e.minable and (e.type=="tree" or rock) then
     natural[#natural+1]={name=e.name,position=pos(e.position),unit_number=e.unit_number}
    else legal=false;break end
   end
  end
 end
 if not legal then blocked[#blocked+1]=p elseif #natural>0 then clearable[#clearable+1]={position=p,entities=natural} end
end end
return {ok=true,blocked=blocked,clearable=clearable}
''')
        if not survey.get("ok"):
            return {"ok":False,"reason":survey.get("reason","corridor_survey_failed")}
        blocked_tiles={(p["x"],p["y"]) for p in survey.get("blocked",[])}
        compact_tiles={(e["position"]["x"],e["position"]["y"]) for e in cell["entities"]
                       if e["name"]!="burner-mining-drill"}
        if compact_tiles & blocked_tiles:
            return {"ok":False,"reason":"compact_coal_cell_has_protected_obstruction"}
        occupied=self.builder._occupied_by_plan(power["entities"]+cell["entities"])
        occupied.update((p["x"],p["y"]) for p in survey.get("blocked",[]))
        occupied.discard((a["x"],a["y"]));occupied.discard((b["x"],b["y"]))
        _atomic_json(self.path.parent/"power-route-survey.json",{"source":source,"destination":destination,
                     "occupied":[list(p) for p in sorted(occupied)],"bounds":bounds})
        route=route_trunks(a,b,occupied=occupied,bounds=bounds,start_direction=source["facing"],
                           end_direction=destination["facing"],max_turns=6,max_detour_ratio=1.35,max_nodes=100000)
        if not route["ok"]:
            return route
        belts=[{"name":"transport-belt",**segment} for segment in route["segments"]]
        entities={identity(e):e for e in cell["entities"]+belts}
        plan=_plan(list(entities.values()),cell["ports"],drill=cell["drill"],template="pcb_starter_coal",
                   route_metrics=route["metrics"],route_path=route["path"],layout_policy="pcb-trunks-v1")
        route_tiles={(p["x"],p["y"]) for p in route["path"]}|compact_tiles
        clear={}
        for row in survey.get("clearable",[]):
            if (row["position"]["x"],row["position"]["y"]) in route_tiles:
                for e in row["entities"]:
                    clear[(e["name"],e["position"]["x"],e["position"]["y"])]=e
        if len(clear)>128:
            return {"ok":False,"reason":"corridor_clearance_exceeds_128_natural_obstacles"}
        removed=[{"name":e["name"],"position":e["position"],"unit_number":e["unit_number"]}
                 for e in obs["entities"] if identity(e) in old_keys-power_keys
                 and e["name"]!="burner-mining-drill" and identity(e) not in entities]
        digest=hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest()
        self.state={"world_id":obs["world_id"],"old_plan_digest":digest,"new_plan":plan,
                    "remove":removed,"clear":list(clear.values()),"phase":"recover_old_hardware",
                    "before_belts":sum(e["name"]=="transport-belt" for e in old["entities"]),
                    "after_belts":sum(e["name"]=="transport-belt" for e in plan["entities"])}
        _atomic_json(self.path,self.state)
        return {"ok":True,"before_belts":self.state["before_belts"],"after_belts":self.state["after_belts"],
                "route_metrics":route["metrics"],"recover_entities":len(removed),"clear_obstacles":len(clear)}

    def step(self,obs):
        if not self.state:
            prepared=self.prepare(obs)
            if not prepared.get("ok"):
                return {"status":"blocked","reason":prepared.get("reason","layout_preparation_failed")}
        if obs["world_id"]!=self.state["world_id"]:
            return {"status":"blocked","reason":"migration_world_changed"}
        if self.state["phase"]=="recover_old_hardware":
            for target in self.state["remove"]:
                if any(e.get("unit_number")==target["unit_number"] for e in obs["entities"]):
                    return {"type":"mine","name":target["name"],"position":target["position"],"count":1,
                            "expected_entity_unit":target["unit_number"],"expected_entity_world_id":obs["world_id"],
                            "reason":"recover paid obsolete belt hardware before regular trunk construction"}
            self.state["phase"]="clear_corridor";_atomic_json(self.path,self.state)
        if self.state["phase"]=="clear_corridor":
            for target in self.state["clear"]:
                exists=self.game.query('local e=target('+json.dumps(target["position"]).replace('"x":','x=').replace('"y":','y=')+','+json.dumps(target["name"])+');return {exists=e~=nil}')
                if exists.get("exists"):
                    return {"type":"mine","name":target["name"],"position":target["position"],"count":1,
                            "reason":"ordinary mining to clear reserved straight belt corridor"}
            self.builder._sync(obs)
            current=hashlib.sha256(json.dumps(self.builder.state["coal_plan"],sort_keys=True).encode()).hexdigest()
            intended=hashlib.sha256(json.dumps(self.state["new_plan"],sort_keys=True).encode()).hexdigest()
            if current not in {self.state["old_plan_digest"],intended}:
                return {"status":"blocked","reason":"coal_plan_changed_during_migration"}
            if current!=intended:
                self.builder.state["coal_plan"]=self.state["new_plan"];self.builder._save()
            self.state["phase"]="build_validated_trunk";_atomic_json(self.path,self.state)
        result=self.builder.ensure_plan(obs,self.state["new_plan"])
        if result.get("status")=="succeeded":
            return {"status":"succeeded","reason":"regular coal trunk installed with paid hardware recovery",
                    "evidence":{"before_belts":self.state["before_belts"],"after_belts":self.state["after_belts"],
                                "route_metrics":self.state["new_plan"]["route_metrics"],"tick":obs["tick"],
                                "scope":"layout integrity only; power still needs fuel/flow verification"}}
        return result

    def proof(self,obs):
        if not self.state or self.state["phase"]!="build_validated_trunk":
            return None
        actual={identity(e) for e in obs["entities"]}
        if not all(identity(e) in actual for e in self.state["new_plan"]["entities"]):
            return None
        return {"tick":obs["tick"],"route_metrics":self.state["new_plan"]["route_metrics"],
                "before_belts":self.state["before_belts"],"after_belts":self.state["after_belts"],
                "scope":"layout integrity only; not sustained power proof"}
