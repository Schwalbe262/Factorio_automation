"""Migrate an unfinished starter coal feed to a validated PCB-style trunk.

Old paid hardware is recovered through normal, identity-guarded mining. A hot
operating plant requires a future parallel cutover capability and is rejected.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from factorio_ai.deterministic_state import _atomic_json
from .routing import route_trunks


STARTER_MAX_ROUTE_ATTEMPTS=10
STARTER_PLANNING_SECONDS=20.0


def identity(entity):
    return (entity["name"], entity["position"]["x"], entity["position"]["y"], entity.get("direction",0))


def _power_preflight_projection(power,observation):
    """Normal-pipe orientation is cosmetic; retain exact observed unit guards."""
    actual={(e['name'],e['position']['x'],e['position']['y']):e for e in observation.get('entities',[])}
    entities=deepcopy(power['entities']);pins=[]
    for e in entities:
        if e['name']!='pipe':continue
        old=actual.get(('pipe',e['position']['x'],e['position']['y']))
        if old and old.get('direction')==0 and type(old.get('unit_number'))is int and old['unit_number']>0:
            e['direction']=0
            pins.append(dict(name='pipe',position=deepcopy(e['position']),unit_number=old['unit_number']))
    return entities,pins


def _power_pipe_guard(pins):
    payload=json.dumps(json.dumps(pins,separators=(',',':')))
    return '''local starter_power_pipes=helpers.json_to_table('''+payload+''');
for _,q in ipairs(starter_power_pipes)do local old=target(q.position,"pipe")
 if not old or not old.valid or old.name~="pipe"or old.type~="pipe"or old.unit_number~=q.unit_number
  or old.force~=f or old.surface~=s or old.direction~=0 or old.position.x~=q.position.x or old.position.y~=q.position.y then
  return {ok=false,reason="starter_power_pipe_identity_changed",position=q.position}
 end
end;'''


def compact_coal_cell(position):
    x,y = position["x"],position["y"]
    drill = {"name":"burner-mining-drill","position":dict(position),"direction":4}
    entities = [drill]
    for dx,dy,direction in ((1.5,-.5,4),(2.5,-.5,8),(2.5,.5,8),(2.5,1.5,4)):
        entities.append({"name":"transport-belt","position":{"x":x+dx,"y":y+dy},"direction":direction})
    # Direction is the pickup side: east pickup, west drop into the drill.
    entities.append({"name":"burner-inserter","position":{"x":x+1.5,"y":y+.5},"direction":4})
    from .resource_policy import mark_mining_core,MINING_NAMES
    footprint=mark_mining_core([e for e in entities if e['name'] in MINING_NAMES],'coal')
    # Publish a straight off-ore outlet after the corner. A corner source has
    # a perpendicular upstream feed, which the generic crossing compiler must
    # reject when it cannot prove that foreign belt is part of this producer.
    entities.append({'name':'transport-belt','position':{'x':x+3.5,'y':y+1.5},'direction':4})
    return _plan(entities,[{"kind":"item","item":"coal","direction":"output","facing":4,
                          "position":{"x":x+3.5,"y":y+1.5}}],drill=drill,
                 mining_footprint=footprint,template="compact_starter_coal")


class V2FactoryBuilder(FactoryBuilder):
    """Use fixed trunk routing and compact starter cells for future v2 plans."""
    _coal_plan=staticmethod(compact_coal_cell)

    def __init__(self,game,bootstrap,catalog):
        super().__init__(game,bootstrap,catalog)
        bootstrap.cell_site_cleanup=self._recover_resource_cell_obstruction

    def _choose_coal_plan(self,power):
        """Choose a new boundary cell and complete resource-free boiler route.

        Existing checkpoints remain authoritative. Normal ensure_plan checks
        either preserve their exact live assets or explicitly block repairs;
        this selector never deletes a saved plan or moves an operating plant.
        """
        if 'coal_plan' in self.state:
            return deepcopy(self.state['coal_plan'])
        from types import SimpleNamespace
        from .planning import preflight
        from .resource_policy import RESOURCE_CHECK_LUA,mining_reservations,policy_specs
        from .smelting import merge_entities
        from .transport import plan_connection,xy
        destinations=[p for p in power.get('ports',[]) if p.get('item')=='coal' and p.get('direction')=='input']
        if len(destinations)!=1:
            return {'ok':False,'reason':'starter_power_requires_one_typed_coal_input'}
        started=time.monotonic();attempts=0;examined=0;failures=[];indices=[];power_guard=''
        def expired():return time.monotonic()-started>=STARTER_PLANNING_SECONDS
        def failure(reason):
            return {'ok':False,'reason':reason,'failure_category':'layout_planning',
                    'route_attempts':attempts,'route_attempt_budget':STARTER_MAX_ROUTE_ATTEMPTS,
                    'planning_seconds_budget':STARTER_PLANNING_SECONDS,'elapsed_seconds':round(time.monotonic()-started,3),
                    'boundary_candidates':len(indices),'candidates_examined':examined,'failures':failures[-8:]}
        def budget_query(script):
            if expired():return {'ok':False,'reason':'starter_coal_planning_budget_exhausted'}
            result=self.game.query(power_guard+script)
            return {'ok':False,'reason':'starter_coal_planning_budget_exhausted'} if expired() else result
        planning_game=SimpleNamespace(query=budget_query,cfg=getattr(self.game,'cfg',None))
        obs=self.game.observe()
        if expired():return failure('starter_coal_planning_budget_exhausted')
        power_entities,pins=_power_preflight_projection(power,obs);power_guard=_power_pipe_guard(pins)
        cells=[self._coal_plan(position) for position in self.coal_sites()[:256]]
        if expired():return failure('starter_coal_planning_budget_exhausted')
        if not cells:return {'ok':False,'reason':'no_starter_coal_candidates'}
        # The fuel return arm and outgoing port must both be off ore and mining
        # reservations. No inserter exception or prospective on-patch source is
        # required; only the drill and its remaining local collector are scoped.
        boundaries=[]
        for cell in cells:
            arm=next(e for e in cell['entities'] if e['name']=='burner-inserter')
            source=cell['ports'][0]
            output={'name':'transport-belt','position':source['position'],'direction':source['facing']}
            boundaries.append(policy_specs(self,{'world_id':obs['world_id'],'entities':[]},[arm,output]))
        payload=json.dumps(json.dumps({'boundaries':boundaries,'zones':mining_reservations(self.game,obs['world_id'])},separators=(',',':')))
        survey=budget_query(RESOURCE_CHECK_LUA+'''
local args=helpers.json_to_table('''+payload+''');local legal={}
for index,specs in ipairs(args.boundaries) do local clear=true
 for _,spec in ipairs(specs) do if resource_violation(spec,args.zones,{},specs) then clear=false;break end end
 if clear then legal[#legal+1]=index end
end
return {ok=true,legal=legal}
''')
        if not survey.get('ok'):
            return failure('starter_coal_planning_budget_exhausted') if expired() else survey
        indices=survey.get('legal',[])
        if indices=={}:indices=[]
        if (not isinstance(indices,list)
                or any(type(index) is not int or not 1<=index<=len(cells) for index in indices)
                or len(indices)!=len(set(indices))):
            return {'ok':False,'reason':'invalid_starter_boundary_survey'}
        adapter=SimpleNamespace(_occupied_by_plan=self._occupied_by_plan,
                                can_place=lambda entities:preflight(planning_game,self,obs,entities))
        for index in indices:
            if expired() or attempts>=STARTER_MAX_ROUTE_ATTEMPTS:return failure('starter_coal_planning_budget_exhausted')
            examined+=1
            cell=cells[index-1]
            check=preflight(planning_game,self,obs,cell['entities'])
            if not check.get('ok'):
                failures.append({'candidate':index,'stage':'cell','reason':check.get('reason')});continue
            try:
                reserved=merge_entities(power_entities,cell['entities'])
                if expired():return failure('starter_coal_planning_budget_exhausted')
                attempts+=1
                route=plan_connection(planning_game,adapter,obs,cell['ports'][0],destinations[0],reserved)
                if not route.get('ok'):
                    failures.append({'candidate':index,'stage':'route','reason':route.get('reason')});continue
                entities=merge_entities(cell['entities'],route['entities'])
                whole=preflight(planning_game,self,obs,merge_entities(power_entities,entities))
            except ValueError as exc:
                failures.append({'candidate':index,'stage':'geometry','reason':str(exc)});continue
            if not whole.get('ok'):
                failures.append({'candidate':index,'stage':'whole','reason':whole.get('reason')});continue
            if expired():return failure('starter_coal_planning_budget_exhausted')
            obstacles={(e['name'],*xy(e)):e for row in (check,route,whole) for e in row.get('clear_obstacles',[])}
            return _plan(entities,cell['ports'],drill=cell['drill'],template='dedicated_boundary_coal_feed_v2',
                mining_footprint=cell['mining_footprint'],underground_pairs=route.get('underground_pairs',[]),
                route_metrics=route['route_metrics'],clear_obstacles=list(obstacles.values()),
                clearance_world_id=obs['world_id'],layout_policy='pcb-starter-boundary-coal-v2')
        return failure('starter_coal_planning_budget_exhausted' if expired() else
                       'no_boundary_coal_cell_with_complete_resource_avoiding_boiler_route')

    def _recover_resource_cell_obstruction(self,observation,plan,entity):
        recovery=super()._recover_resource_cell_obstruction(observation,plan,entity)
        if recovery is not None:return recovery
        payload=json.dumps(json.dumps(entity,separators=(',',':')))
        survey=self.game.query('''
local spec=helpers.json_to_table('''+payload+''');local box=prototypes.entity[spec.name].collision_box
local function rotate(p)
 local d=spec.direction or 0
 if d==4 then return {x=-p.y,y=p.x} elseif d==8 then return {x=-p.x,y=-p.y} elseif d==12 then return {x=p.y,y=-p.x} end
 return p
end
local p,q=rotate(box.left_top),rotate(box.right_bottom)
local area={{spec.position.x+math.min(p.x,q.x),spec.position.y+math.min(p.y,q.y)},
 {spec.position.x+math.max(p.x,q.x),spec.position.y+math.max(p.y,q.y)}}
local ground={}
for _,e in pairs(s.find_entities_filtered{area=area}) do if e~=a and e.type~="resource" and e.type~="corpse" then
 if e.type~="item-entity" or not e.stack.valid_for_read or e.stack.prototype.type~="item" then return {ok=false} end
 ground[#ground+1]={name=e.name,position=pos(e.position),item=e.stack.name,quality=e.stack.quality.name,count=e.stack.count}
end end
return {ok=true,world_id=d.world_id,ground=ground}
''')
        if not survey.get('ok') or survey.get('world_id')!=observation['world_id'] or not survey.get('ground'):return None
        row=sorted(survey['ground'],key=lambda e:(e['position']['x'],e['position']['y']))[0]
        return dict(row,type='take',count=min(50,row['count']),
                    reason='normally collect spilled construction-site items without discarding materials')

    def ensure_plan(self,observation,plan):
        clearance_only=(plan.get('entities')==[] and isinstance(plan.get('clear_obstacles'),list)
                        and 'clearance_world_id' in plan)
        if clearance_only and plan['clearance_world_id']!=observation['world_id']:
            return {'status':'blocked','reason':'clearance_plan_world_changed'}
        from .resource_policy import ensure_resource_policy
        resource_check=ensure_resource_policy(self.game,self,observation,plan)
        if not resource_check.get('ok'):
            return {'status':'blocked',**resource_check}
        if plan.get('clear_obstacles'):
            if plan.get('clearance_world_id')!=observation['world_id']:
                return {'status':'blocked','reason':'clearance_plan_world_changed'}
            payload=json.dumps(json.dumps(plan['clear_obstacles'],separators=(',',':')))
            result=self.game.query('''
local approved=helpers.json_to_table('''+payload+''')
for _,wanted in ipairs(approved) do
 local e=target(wanted.position,wanted.name)
 if e then
  local rock=e.type=="simple-entity" and (e.name=="big-rock" or e.name=="huge-rock" or e.name=="big-sand-rock")
  if e.force.name~="neutral" or not e.minable or not (e.type=="tree" or rock)
   or (wanted.unit_number and e.unit_number~=wanted.unit_number) then return {ok=false,reason="approved_natural_obstacle_changed"} end
  return {ok=true,obstacle={name=e.name,position=pos(e.position)}}
 end
end
return {ok=true}
''')
            if not result.get('ok'):return {'status':'blocked','reason':result.get('reason','clearance_survey_failed')}
            if result.get('obstacle'):
                return {'type':'mine',**result['obstacle'],'count':1,
                        'reason':'normally mine approved tree or rock on reserved straight corridor'}
        if clearance_only:
            return {'status':'succeeded','reason':'approved clearance-only plan complete'}
        result=super().ensure_plan(observation,plan)
        if result.get('status')!='succeeded':return result
        for wanted in plan['entities']:
            if wanted['name']!='splitter' or not any(k in wanted for k in ('splitter_input_priority','splitter_output_priority')):continue
            priorities={k:wanted.get('splitter_'+k,'none') for k in ('input_priority','output_priority')}
            if any(v not in ('none','left','right') for v in priorities.values()):
                return {'status':'blocked','reason':'invalid_declared_splitter_priority'}
            actual=next((e for e in observation['entities'] if e['name']=='splitter' and e['position']==wanted['position']),None)
            if not actual:return {'status':'waiting','reason':'reobserve paid splitter before configuring priorities'}
            if any(actual.get('splitter_'+k)!=v for k,v in priorities.items()):
                return dict(priorities,type='configure_splitter',name='splitter',position=wanted['position'],
                    expected_splitter_unit=actual['unit_number'],expected_splitter_world_id=observation['world_id'],
                    reason='apply declared main-bus input/output priority through normal native splitter settings')
        return result

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
