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


def smelting_throughput_bounds(root,obs,catalog):
    from .smelting import bank_capacities
    path=Path(root)/'smelting-district.json'
    if not path.exists():return {'status':'no_registered_smelting'}
    state=json.loads(path.read_text(encoding='utf-8'))
    mining_path=Path(root)/'mining-outposts.json'
    mining=json.loads(mining_path.read_text(encoding='utf-8')) if mining_path.exists() else {}
    if state.get('world_id')!=obs['world_id'] or mining.get('world_id')!=obs['world_id']:
        return {'status':'world_changed'}
    rows=[]
    for bank in state['banks']:
        if not bank.get('producer_rows'):continue
        capacities={lane:bank_capacities(catalog,[dict(bank,entities=producers)])[bank['resource']] if producers else 0
                    for lane,producers in bank['producer_rows'].items()}
        sources=[b for b in mining['banks'] if b.get('ready') and b['plan']['resource']==bank['resource']]
        installed_ore=sum(b['nominal_rate_per_minute'] for b in sources)
        actual={(e['name'],e['position']['x'],e['position']['y']):e for e in obs.get('entities',[]) if e.get('position')}
        remaining=[]
        for source in sources:
            drills=[e for e in source['plan'].get('entities',[]) if e['name']=='electric-mining-drill']
            if not drills:
                remaining.append(None);continue
            live=[actual.get((e['name'],e['position']['x'],e['position']['y'])) for e in drills]
            active=sum(bool(e and e.get('electric_network_connected') and e.get('energy',0)>0
                            and e.get('status_name') not in {'no_minable_resources','no_mineable_resources','no_power'}) for e in live)
            remaining.append(source['nominal_rate_per_minute']*active/len(drills))
        observed_ore=sum(remaining) if all(v is not None for v in remaining) else None
        ore=observed_ore if observed_ore is not None else installed_ore
        native=[p['nominal_unstacked_items_per_second_per_lane']*60
                for p in obs.get('belt_lane_observation',{}).get('ports',[])
                if p['item']==bank['product']]
        belt_per_lane=min(native) if native else None
        per_lane=[min(v,belt_per_lane) if belt_per_lane is not None else v for v in capacities.values()]
        rows.append({'product':bank['product'],'independent_producer_capacity_per_lane':capacities,
            'unstacked_belt_capacity_per_lane':belt_per_lane,'connected_nominal_ore_supply':ore,
            'installed_nominal_ore_supply':installed_ore,'observed_remaining_nominal_ore_supply':observed_ore,
            'ore_basis':'observed remaining nameplate' if observed_ore is not None else 'installed nameplate; live source unknown',
            'partial_upper_bound_per_minute':min(ore,sum(per_lane))})
    return {'banks':rows,'scope':'design upper bounds, not measured flow; fuel, power, inserters and downstream demand can impose further limits'}


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
    for filename in ('automation-science.json','logistic-science.json','steam-expansion.json'):
        path=Path(root)/filename
        if path.exists():
            state=json.loads(path.read_text(encoding='utf-8'))
            if state.get('world_id')==obs['world_id']:plans.append(state['plan'])
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
            "scope":"all currently saved power/board/mining/science plans, not unplanned future stages; excludes queued outputs and intermediate stock credit"}


def factory_constraints(root,obs,catalog):
    """Nameplate design constraints are separate from live flow measurements."""
    from .manufacturing import raw_demand
    def read(filename):
        p=Path(root)/filename
        state=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
        return state if state.get('world_id')==obs['world_id'] else {}
    mining=read('mining-outposts.json');smelting=read('smelting-district.json')
    nominal={};lines=[];demand=Counter()
    for item,ore in (('iron-plate','iron-ore'),('copper-plate','copper-ore')):
        supply=sum(b['nominal_rate_per_minute'] for b in mining.get('banks',[]) if b.get('ready') and b['plan']['resource']==ore)
        producers=sum(b['count']*catalog.entities['stone-furnace']['crafting_speed']*60/catalog.recipes[item]['energy']
                      for b in smelting.get('banks',[]) if b['product']==item)
        nominal[item]=min(supply,producers)
    red=read('automation-science.json')
    if red:
        needs=raw_demand(catalog,'automation-science-pack',red['nominal_machine_rate'])['raw_items_per_minute']
        demand.update(needs);lines.append({'product':'automation-science-pack','nominal_per_minute':red['nominal_machine_rate'],'raw_demand':needs})
    green=read('logistic-science.json')
    if green:
        needs=green['rated_demand']['raw_items_per_minute'];demand.update(needs)
        lines.append({'product':'logistic-science-pack','nominal_per_minute':green['nominal_machine_rate'],
                      'raw_demand':needs,'phase':green['phase']})
    peak=0;generation=0;low_power=Counter();no_fuel=Counter();no_inputs=Counter();full_output=Counter()
    for e in obs['entities']:
        proto=catalog.entities.get(e['name'],{})
        if proto.get('electric'):peak+=proto.get('energy_usage',0)*60/1000
        generation+=proto.get('energy_production',0)*60/1000
        if e.get('status_name') in ('low_power','no_power'):low_power[e['name']]+=1
        if e.get('status_name')=='no_fuel':no_fuel[e['name']]+=1
        if e.get('status_name')=='no_ingredients':no_inputs[e['name']]+=1
        if e.get('status_name')=='full_output':full_output[e['name']]+=1
    return {'registered_design_lines':lines,'nominal_plate_supply_per_minute':nominal,
            'simultaneous_rated_plate_demand_per_minute':dict(demand),
            'rated_plate_deficit_per_minute':{item:max(0,n-nominal.get(item,0)) for item,n in demand.items()},
            'installed_generation_nameplate_kw':generation,'installed_consumer_peak_kw':peak,
            'live_low_or_no_power_entities':dict(low_power),
            'live_no_fuel_entities':dict(no_fuel),'live_waiting_for_ingredients_entities':dict(no_inputs),
            'live_full_output_entities':dict(full_output),
            'scope':'catalog design bounds and instantaneous native status; peak is not average demand; construction/mall/ammo/steel add further demand; no measured throughput claim'}
