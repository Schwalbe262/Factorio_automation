"""Measured time-window rates and whole known-project construction budgets."""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path

from factorio_ai.deterministic_state import RunLock,_atomic_json


def port_lane_snapshot(root,game,obs):
    path=Path(root)/'pcb-board.json'
    if not path.exists():return {'scope':'no reserved board ports yet','ports':[]}
    board=json.loads(path.read_text(encoding='utf-8'))
    if board.get('world_id')!=obs['world_id']:return {'status':'world_changed','ports':[]}
    specs={}
    for block in board.get('blocks',{}).values():
        ports=list(block.get('ports',[]))
        if block.get('source_port'):ports.append(block['source_port'])
        for p in ports:
            key=p['item'],p['position']['x'],p['position']['y']
            specs[key]=p
    by_position={(e['position']['x'],e['position']['y']):e for e in obs['entities'] if e['name']=='transport-belt'}
    selected=[dict(p,expected_unit=by_position[(p['position']['x'],p['position']['y'])]['unit_number'])
              for p in specs.values() if (p['position']['x'],p['position']['y']) in by_position]
    payload=json.dumps(json.dumps(selected,separators=(',',':')))
    return game.query('''
local specs=helpers.json_to_table('''+payload+''');local ports={}
for _,p in ipairs(specs) do
 local e=target(p.position,"transport-belt")
 if e and e.unit_number==p.expected_unit then
  local left={};local right={}
  for _,pair in ipairs({{defines.transport_line.left_line,left},{defines.transport_line.right_line,right}}) do
   for _,item in pairs(e.get_transport_line(pair[1]).get_contents()) do pair[2][item.name]=(pair[2][item.name] or 0)+item.count end
  end
  ports[#ports+1]={item=p.item,position=p.position,direction=e.direction,left=left,right=right,
   declared_lane_items=p.lane_items,declared_lane_policy=p.lane_policy,
   nominal_unstacked_items_per_second_per_lane=e.prototype.belt_speed*60*4}
 end
end
return {ok=true,tick=game.tick,ports=ports,scope="instantaneous lane inventories at typed board ports; left/right relative to belt movement; not measured throughput"}
''')


def production_rates(root,obs):
    path=Path(root)/"rate-samples.json"
    with RunLock(Path(root)/"observer.lock"):
        old=json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        samples=old.get("samples",[]) if old.get("world_id")==obs["world_id"] else []
        reset=None
        if samples and obs["tick"]<samples[-1]["tick"]:
            samples=[];reset="world_rollback"
        if samples and any(obs["production"].get(name,{}).get("produced",0)<row.get("produced",0)
                           for name,row in samples[-1]["production"].items()):
            samples=[];reset="production_counter_reset"
        if not samples or obs["tick"]!=samples[-1]["tick"]:
            samples.append({"tick":obs["tick"],"production":obs["production"]})
        while len(samples)>2 and samples[1]["tick"]<obs["tick"]-18000:samples.pop(0)
        samples=samples[-64:]
        _atomic_json(path,{"world_id":obs["world_id"],"samples":samples})
    if len(samples)<2 or samples[-1]["tick"]-samples[0]["tick"]<300:
        return {"status":"sampling","reset_reason":reset,"rates_per_game_minute":None}
    first,last=samples[0],samples[-1]
    minutes=(last["tick"]-first["tick"])/3600
    rates={}
    for name,row in last["production"].items():
        baseline=first["production"].get(name,{})
        rates[name]={kind:(row.get(kind,0)-baseline.get(kind,0))/minutes for kind in ("produced","consumed")}
    return {"status":"measured","from_tick":first["tick"],"to_tick":last["tick"],
            "window_game_seconds":minutes*60,"rates_per_game_minute":rates,
            "scope":"engine counters over the stated window; includes handcraft where game statistics do"}


def construction_budget(root,obs,catalog):
    plans=[];unplanned=[]
    path=Path(root)/"factory-builder.json"
    if path.exists():
        builder=json.loads(path.read_text(encoding="utf-8"))
        for key in ("power_plan","coal_plan"):
            if builder.get(key):plans.append(builder[key])
            else:unplanned.append(key)
    path=Path(root)/"pcb-board.json"
    if path.exists():plans+=list(json.loads(path.read_text(encoding="utf-8")).get("blocks",{}).values())
    path=Path(root)/"electric-coal.json"
    if path.exists():
        plan=json.loads(path.read_text(encoding="utf-8")).get("power_plan")
        if plan:plans.append(plan)
    path=Path(root)/"mining-outposts.json"
    if path.exists():
        state=json.loads(path.read_text(encoding="utf-8"))
        if state.get("world_id")==obs["world_id"]:
            plans += [bank["plan"] for bank in state.get("banks",[])]
    actual={(e["name"],e["position"]["x"],e["position"]["y"]):e for e in obs["entities"]}
    wanted={}
    for plan in plans:
        for e in plan.get("entities",[]):wanted[(e["name"],e["position"]["x"],e["position"]["y"])]=e
    missing=Counter();configuration=[]
    for key,e in wanted.items():
        if key in actual:
            if e.get("recipe") and actual[key].get("recipe")!=e["recipe"]:configuration.append(e.get("recipe"))
            continue
        missing[e.get("item") or e["name"]]+=1
    missing=Counter({name:max(0,count-obs.get("inventory",{}).get(name,0)) for name,count in missing.items()})
    missing=Counter({name:count for name,count in missing.items() if count})
    base=Counter();unresolved=[]
    leaves={"iron-plate","copper-plate","coal","stone","wood","stone-brick"}
    def expand(item,count,stack=()):
        if item in leaves:base[item]+=count;return
        if item in stack:unresolved.append(item);return
        recipe=catalog.recipe_for_product(item)
        if not recipe or len(recipe["products"])!=1 or any(r.get("type","item")!="item" for r in recipe["ingredients"]):
            unresolved.append(item);return
        product=recipe["products"][0]
        if product.get("probability",1)!=1 or not product.get("amount"):
            unresolved.append(item);return
        cycles=math.ceil(count/product["amount"])
        for row in recipe["ingredients"]:expand(row["name"],row["amount"]*cycles,(*stack,item))
    for item,count in missing.items():expand(item,count)
    outputs=Counter()
    for e in obs["entities"]:
        if e["name"] in {"stone-furnace","steel-furnace","electric-furnace"}:outputs.update(e.get("output_inventory",{}))
    shortfall={name:max(0,count-obs.get("inventory",{}).get(name,0)-outputs.get(name,0)) for name,count in base.items()}
    return {"remaining_placement_items":dict(missing),"base_material_upper_bound":dict(base),
            "furnace_output_stock":dict(outputs),"base_shortfall_after_held_and_furnace_outputs":shortfall,
            "configuration_pending":configuration,"unplanned_power_steps":unplanned,"unresolved_materials":unresolved,
            "scope":"all currently saved power/board/mining plans, not future science/oil/rocket; excludes queued outputs and intermediate stock credit"}
