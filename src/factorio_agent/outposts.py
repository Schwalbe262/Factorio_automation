"""Additive electric mining banks with aligned collectors and typed output ports."""
from __future__ import annotations

import json
import math
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .board import entity
from .power_grid import plan_connection


RESOURCES={"iron-ore","copper-ore","coal","stone"}


def validate_parameters(parameters):
    if not isinstance(parameters,dict) or set(parameters)!={"resource","rate_per_minute"}:
        raise ValueError("mining_outpost requires resource and rate_per_minute")
    if not isinstance(parameters["resource"],str) or parameters["resource"] not in RESOURCES:
        raise ValueError("unsupported solid resource")
    rate=parameters["rate_per_minute"]
    if type(rate) not in (int,float) or not math.isfinite(rate) or not 0<rate<=240:
        raise ValueError("mining rate must be finite in (0,240]")


def mining_bank(x,y,count,resource):
    es=[entity("electric-mining-drill",x+4*i,y,8) for i in range(count)]
    es += [entity("transport-belt",x+dx,y+2,4) for dx in range(-1,4*(count-1)+3)]
    es += [entity("small-electric-pole",x+4*i+2,y) for i in range(count)]
    port={"kind":"item","item":resource,"direction":"output","facing":4,
          "position":{"x":x+4*(count-1)+2,"y":y+2}}
    return _plan(es,[port],template="electric_mining_bank",resource_cell=True,
                 resource=resource,drill_count=count,layout_policy="pcb-mining-row-v1")


class MiningOutposts:
    def __init__(self,game,bootstrap,builder):
        self.game,self.bootstrap,self.builder=game,bootstrap,builder
        self.path=Path(game.cfg.runtime_dir)/"mining-outposts.json"
        self.state=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @staticmethod
    def find(obs,e):
        return next((a for a in obs["entities"] if a["name"]==e["name"] and a["position"]==e["position"]),None)

    def prepare(self,obs,resource,count):
        areas=[];board=self.path.parent/"pcb-board.json"
        if board.exists():
            areas=[p["reserved_area"] for p in json.loads(board.read_text(encoding="utf-8")).get("blocks",{}).values()]
        args=json.dumps(json.dumps({"resource":resource,"count":count,"reserved":areas},separators=(",",":")))
        candidates=self.game.query('''
local args=helpers.json_to_table('''+args+''');local resource=args.resource;local count=args.count
local proto=prototypes.entity["electric-mining-drill"];local radius=proto.mining_drill_radius
local best={};local seen={}
for _,ore in pairs(s.find_entities_filtered{position={0,0},radius=384,name=resource,limit=2000}) do
 local x=math.floor(ore.position.x)+.5;local y=math.floor(ore.position.y)+.5;local key=x..":"..y
 if not seen[key] then
  seen[key]=true;local legal=true;local amount=0;local coverage=0
  for i=0,count-1 do
   local p={x=x+4*i,y=y}
   for _,area in ipairs(args.reserved) do
    if p.x+2>area[1][1] and p.x-2<area[2][1] and p.y+2>area[1][2] and p.y-2<area[2][2] then legal=false;break end
   end
   if not s.can_place_entity{name="electric-mining-drill",position=p,direction=8,force=f} then legal=false;break end
   local covered=0;local available=0
   for _,r in pairs(s.find_entities_filtered{area={{p.x-radius,p.y-radius},{p.x+radius,p.y+radius}},type="resource"}) do
    if r.name~=resource then legal=false;break end;covered=covered+1;available=available+r.amount
   end
   if covered<4 or available<1800 then legal=false;break end
   amount=amount+available;coverage=coverage+covered
   if not s.can_place_entity{name="small-electric-pole",position={x=p.x+2,y=y},force=f} then legal=false;break end
  end
  if legal then for dx=-1,4*(count-1)+2 do
   if not s.can_place_entity{name="transport-belt",position={x=x+dx,y=y+2},direction=4,force=f} then legal=false;break end
  end end
  if legal then
   best[#best+1]={x=x,y=y,amount=amount,score=coverage*100000+math.min(amount,10000)-x*x-y*y}
   table.sort(best,function(a,b) return a.score>b.score end)
   if #best>8 then table.remove(best) end
  end
 end
end
local mined=prototypes.entity[resource].mineable_properties
return {ok=true,candidates=best,nominal_drill_rate=proto.mining_speed*60/mined.mining_time}
''')
        if not candidates.get("ok"):return candidates
        for site in candidates.get("candidates",[]):
            base=mining_bank(site["x"],site["y"],count,resource)
            pole=next(e for e in base["entities"] if e["name"]=="small-electric-pole")
            reserved=[e for e in base["entities"] if e["name"]!="small-electric-pole"]
            power=plan_connection(self.game,self.builder,obs,pole["position"],reserved)
            if not power.get("ok"):continue
            all_entities={(e["name"],e["position"]["x"],e["position"]["y"]):e
                          for e in power["entities"]+base["entities"]}
            full=_plan(list(all_entities.values()),base["ports"],resource_cell=True,
                       resource=resource,drill_count=count,layout_policy="pcb-mining-row-v1")
            footprints=[self.builder._occupied_by_plan([e]) for e in full["entities"]]
            if sum(map(len,footprints))!=len(set().union(*footprints)):continue
            return {"ok":True,"plan":full,"nominal_rate_per_minute":count*candidates["nominal_drill_rate"],
                    "remaining_resource_survey":site["amount"],"wire_hops":power["wire_hops"],"ready":False}
        return {"ok":False,"reason":"no_clear_electric_mining_row_with_power"}

    def bank_proof(self,obs,bank):
        drills=[self.find(obs,e) for e in bank["plan"]["entities"] if e["name"]=="electric-mining-drill"]
        if any(not e or not e.get("electric_network_connected") or e.get("energy",0)<=0
               or e.get("status_name") in {"no_minable_resources","no_mineable_resources","no_power"} for e in drills):return None
        unit_ids=sorted(e["unit_number"] for e in drills)
        counts=0
        for wanted in bank["plan"]["entities"]:
            if wanted["name"]=="transport-belt":
                actual=self.find(obs,wanted)
                counts+=(actual or {}).get("belt_inventory",{}).get(bank["plan"]["resource"],0)
        if counts:
            bank["output_evidence"]={"tick":obs["tick"],"count":counts,"drill_units":unit_ids}
        evidence=bank.get("output_evidence")
        if not evidence or evidence["drill_units"]!=unit_ids or not 0<=obs["tick"]-evidence["tick"]<=600:return None
        return {"nominal_rate_per_minute":bank["nominal_rate_per_minute"],"drill_units":unit_ids,
                "native_output_evidence":evidence,"output_port":bank["plan"]["ports"][0]}

    def step(self,obs,parameters):
        validate_parameters(parameters)
        if not obs.get("technologies",{}).get("electric-mining-drill"):
            return {"status":"blocked","reason":"electric_mining_research_required"}
        if not self.state:self.state={"world_id":obs["world_id"],"banks":[]}
        if self.state["world_id"]!=obs["world_id"]:return {"status":"blocked","reason":"outpost_world_changed"}
        resource,target=parameters["resource"],parameters["rate_per_minute"]
        banks=[b for b in self.state["banks"] if b["plan"]["resource"]==resource]
        for bank in banks:
            if bank.get("ready") and any(self.find(obs,e) is None for e in bank["plan"]["entities"]):
                return {"status":"blocked","reason":"existing_outpost_damaged_requires_repair_before_expansion"}
            if bank.get("ready") and self.bank_proof(obs,bank) is None:
                return {"status":"waiting","reason":"restore existing outpost power/output before adding capacity"}
        active=[b for b in banks if b.get("ready") and self.bank_proof(obs,b)]
        rate=sum(b["nominal_rate_per_minute"] for b in active)
        if rate>=target:
            _atomic_json(self.path,self.state)
            return {"status":"succeeded","reason":"powered electric mining banks have native output","evidence":self.proof(obs,parameters)}
        pending=next((b for b in banks if not b.get("ready")),None)
        if pending is None:
            metadata=self.game.query('return {rate=prototypes.entity["electric-mining-drill"].mining_speed*60/prototypes.entity['+json.dumps(resource)+'].mineable_properties.mining_time}')
            if not isinstance(metadata.get("rate"),(int,float)) or metadata["rate"]<=0:
                return {"status":"blocked","reason":"mining_rate_query_failed"}
            count=math.ceil((target-rate)/metadata["rate"])
            if count>8:return {"status":"blocked","reason":"one_new_bank_exceeds_eight_drills"}
            pending=self.prepare(obs,resource,count)
            if not pending.get("ok"):return {"status":"blocked","reason":pending.get("reason","mining_bank_preflight_failed")}
            self.state["banks"].append(pending);_atomic_json(self.path,self.state)
        result=self.builder.ensure_plan(obs,pending["plan"])
        if result.get("status")!="succeeded":return result
        proof=self.bank_proof(obs,pending)
        if proof:
            pending["ready"]=True;_atomic_json(self.path,self.state)
            return {"status":"waiting","reason":"new electric bank output observed; remeasure whole target"}
        return {"status":"waiting","reason":"waiting for real electric drill output onto aligned collector"}

    def proof(self,obs,parameters):
        proofs=[self.bank_proof(obs,b) for b in self.state.get("banks",[]) if b.get("ready") and b["plan"]["resource"]==parameters["resource"]]
        proofs=[p for p in proofs if p]
        total=sum(p["nominal_rate_per_minute"] for p in proofs)
        if total<parameters["rate_per_minute"]:return None
        _atomic_json(self.path,self.state)
        return {"tick":obs["tick"],"resource":parameters["resource"],"nominal_rate_per_minute":total,
                "banks":proofs,"scope":"powered automatic extraction with native collector output; rate is catalog capacity, not a sustained throughput measurement"}
