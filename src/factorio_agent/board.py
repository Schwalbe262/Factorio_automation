"""PCB-style block reservation and bounded, whole-layout native preflight."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json


def entity(name,x,y,direction=0,recipe=None):
    result={"name":name,"position":{"x":x,"y":y},"direction":direction}
    if recipe:
        result["recipe"]=recipe
    return result


def startup_science_cell(x,y):
    """A fixed-port, normally powered gear -> red-science machine cell.

    Three chest ports are finite bootstrap buffers, not automated raw supply.
    All transfers inside the cell use real powered inserters.
    """
    entities=[entity("assembling-machine-1",x,y-4,recipe="iron-gear-wheel"),
              entity("assembling-machine-1",x,y,recipe="automation-science-pack"),
              entity("wooden-chest",x-3,y-4),entity("wooden-chest",x-3,y),
              entity("wooden-chest",x,y+3),
              entity("inserter",x-2,y-4,12),entity("inserter",x-2,y,12),
              entity("inserter",x,y-2,0),entity("inserter",x,y+2,0),
              entity("small-electric-pole",x-2,y-2),entity("small-electric-pole",x+2,y+1)]
    ports=[{"item":"iron-plate","kind":"item","direction":"input","position":{"x":x-3,"y":y-4}},
           {"item":"copper-plate","kind":"item","direction":"input","position":{"x":x-3,"y":y}},
           {"item":"automation-science-pack","kind":"item","direction":"output","position":{"x":x,"y":y+3}}]
    return _plan(entities,ports,template="startup_science_cell",layout_policy="pcb-block-v1")


def pole_chain(source,destination,*,horizontal_first=True):
    """Straight wire hops of at most five tiles; wire itself may cross machines."""
    x,y=source["x"],source["y"]
    result=[]
    for axis in ((0,1) if horizontal_first else (1,0)):
        target=destination["x"] if axis==0 else destination["y"]
        current=x if axis==0 else y
        while abs(target-current)>1e-6:
            current+=max(-5,min(5,target-current))
            if axis==0:x=current
            else:y=current
            result.append(entity("small-electric-pole",x,y))
    return result


class Board:
    def __init__(self,game,builder,catalog):
        self.game,self.builder,self.catalog=game,builder,catalog
        self.path=Path(game.cfg.runtime_dir)/"pcb-board.json"
        self.state=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def reserve_startup_science(self,obs):
        if self.state and self.state.get("world_id")!=obs["world_id"]:
            return {"ok":False,"reason":"board_world_changed"}
        if not self.state:
            self.state={"schema_version":1,"world_id":obs["world_id"],"catalog_fingerprint":self.catalog.fingerprint,"blocks":{}}
        if self.state["catalog_fingerprint"]!=self.catalog.fingerprint:
            return {"ok":False,"reason":"board_catalog_changed_requires_revalidation"}
        if "startup_science" in self.state["blocks"]:
            return deepcopy(self.state["blocks"]["startup_science"])
        networks={e.get("electric_network_id") for e in obs["entities"] if e["name"]=="steam-engine" and e.get("energy",0)>0}
        poles=[e for e in obs["entities"] if e["name"]=="small-electric-pole" and e.get("electric_network_id") in networks]
        if not poles:
            return {"ok":False,"reason":"no_energized_source_network"}
        reference=poles[0]["position"]
        bx=math.floor((reference["x"]-8)/8)*8+.5
        by=math.floor(reference["y"]/8)*8+.5
        offsets=[(0,0),(-16,0),(0,16),(-16,16),(0,-16),(-16,-16),(-32,0),(-32,16),(16,0),(16,16)]
        candidates=[]
        for dx,dy in offsets:
            x,y=bx+dx,by+dy
            base=startup_science_cell(x,y)
            target={"x":x+2,"y":y+1}
            source=min(poles,key=lambda e:abs(e["position"]["x"]-target["x"])+abs(e["position"]["y"]-target["y"]))
            for horizontal_first in (True,False):
                plan=deepcopy(base)
                plan["entities"]+=pole_chain(source["position"],target,horizontal_first=horizontal_first)
                unique={ (e["name"],e["position"]["x"],e["position"]["y"]):e for e in plan["entities"] }
                plan=_plan(list(unique.values()),plan["ports"],template="startup_science_cell",layout_policy="pcb-block-v1",
                           reserved_area=[[x-8.5,y-8.5],[x+7.5,y+7.5]])
                # Whole prospective footprint must not overlap itself.
                footprints=[self.builder._occupied_by_plan([e]) for e in plan["entities"]]
                if sum(map(len,footprints))!=len(set().union(*footprints)):
                    continue
                candidates.append(plan)
        payload=json.dumps(json.dumps(candidates,separators=(",",":")))
        surveyed=self.game.query('''
local plans=helpers.json_to_table('''+payload+''');local out={}
for i,p in ipairs(plans) do
 local legal=true
 if s.count_entities_filtered{area=p.reserved_area,type="resource"}>0 then legal=false end
 if legal then
  for _,e in pairs(s.find_entities_filtered{area=p.reserved_area,force=f}) do
   local q=e.position;local lo=p.reserved_area[1];local hi=p.reserved_area[2]
   if e.type~="character" and e.type~="electric-pole" and q.x>lo[1]+.5 and q.x<hi[1]-.5 and q.y>lo[2]+.5 and q.y<hi[2]-.5 then legal=false;break end
  end
 end
 if legal then for _,e in ipairs(p.entities) do
  local old=target(e.position,e.name)
  if not (old and old.force==f and old.type=="electric-pole")
   and not s.can_place_entity{name=e.name,position=e.position,direction=e.direction,force=f} then legal=false;break end
 end end
 if legal then out[#out+1]=i end
end
return {ok=true,legal=out}
''')
        if not surveyed.get("ok"):
            return {"ok":False,"reason":surveyed.get("reason","block_survey_failed")}
        indices=surveyed.get("legal",[])
        if not indices:
            return {"ok":False,"reason":"no_clear_reserved_science_slot"}
        plan=candidates[indices[0]-1]
        self.state["blocks"]["startup_science"]=plan
        _atomic_json(self.path,self.state)
        return deepcopy(plan)
