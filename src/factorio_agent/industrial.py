"""Semantic, guarded adapters for normal steel supply and paired smelting."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
import time

from factorio_ai.deterministic_state import _atomic_json
from .brick_production import BrickProduction
from .bus_tap import BusTap,reconstruct_bus,signature
from .planning import overlap,preflight
from .steel_production import SteelProduction,steel_rates
from .transport import VECTORS,xy


STEEL_TAP_NAME='steel-iron'
MAX_TAP_CANDIDATES=12
MAX_SITE_CANDIDATES=8
PLANNING_SECONDS=20.0


def free_tap_parameters(bus, *, name, item, demand_per_minute, priority='surplus'):
    """Finite existing surface rows, preserving all station approach spacing."""
    materials,_,_,north,south,_=reconstruct_bus(bus)
    material=next((row for row in materials if row.item==item),None)
    if material is None:raise ValueError('requested material has no active shared bus line')
    stations=[row['splitter']['position']['y'] for row in bus['stations']]
    rows=sorted({e['position']['y'] for e in bus['entities'] if e['name']=='transport-belt'
                 and e.get('direction')==0 and e['position']['x']==material.line_x(0)
                 and north+2<=e['position']['y']<=south-2
                 and all(abs(e['position']['y']-y)>=3 for y in stations)})
    return [dict(name=name,item=item,splitter_y=y,priority=priority,demand_per_minute=demand_per_minute,line=0) for y in rows]


def industrial_site_anchors(bus,board,product):
    """Small eastern land search relative to current production reservations."""
    _,_,_,_,south,east=reconstruct_bus(bus)
    edges=[east]+[block['reserved_area'][1][0] for name,block in board['blocks'].items()
                  if 'reserved_area' in block and 'mining' not in name and name not in ('steel_production','brick_production')]
    x=math.ceil(max(edges))+11;y=math.floor(south)-27
    if product=='steel-plate':offsets=((0,0),(16,0),(0,-16),(16,-16),(32,0),(0,16),(16,16),(0,-32))
    elif product=='stone-brick':
        y+=22;offsets=((0,0),(16,0),(0,-16),(16,-16),(0,-44),(16,-44),(32,0),(0,-60))
    else:raise ValueError('unsupported initial industrial product')
    return tuple((x+dx,y+dy) for dx,dy in offsets)


def _source_identity(source):
    return {key:source.get(key) for key in ('item','position','facing','direction','unit_number','world_id','material_supply_id')}


def _plan_revision(plan):
    return hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class PlanningBudget:
    def __init__(self,game):
        self.started=time.monotonic();self.queries=0;self.original=game
        self.game=SimpleNamespace(cfg=game.cfg,query=self.query)

    def expired(self):return time.monotonic()-self.started>=PLANNING_SECONDS

    def failure(self,reason='industrial_planning_budget_exhausted',**extra):
        return dict(ok=False,reason=reason,queries=self.queries,planning_seconds_budget=PLANNING_SECONDS,
                    elapsed_seconds=round(time.monotonic()-self.started,3),**extra)

    def query(self,script):
        if self.expired():return self.failure()
        self.queries+=1;result=self.original.query(script)
        return self.failure() if self.expired() else result


class IndustrialOperations:
    def __init__(self,game,builder,catalog,defense,*,bus_factory=None,science=None,green=None,tap=None,steel=None,brick=None):
        self.game,self.builder,self.catalog,self.defense=game,builder,catalog,defense
        self.path=Path(game.cfg.runtime_dir)/'industrial-operations.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        self.tap=tap if tap is not None else BusTap(game,builder,catalog,bus_factory=bus_factory,science=science,green=green)
        self.steel=steel if steel is not None else SteelProduction(game,builder,catalog)
        self.brick=brick if brick is not None else BrickProduction(game,builder,catalog)

    def _error(self,obs):
        if self.state and (self.state.get('world_id')!=obs['world_id'] or self.state.get('catalog_fingerprint')!=self.catalog.fingerprint):
            return {'ok':False,'reason':'industrial_world_or_catalog_changed'}
        return None

    def _save(self,obs):
        if not self.state:self.state={'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint}
        _atomic_json(self.path,self.state)

    def _bus(self,obs):
        docs=self.tap._documents()
        if any(doc.get('world_id')!=obs['world_id'] or doc.get('catalog_fingerprint')!=self.catalog.fingerprint for doc in docs.values()):
            raise ValueError('industrial bus authority world/catalog changed')
        bus=docs['factory']['bus'];reconstruct_bus(bus)
        if docs['board']['shared_main_bus']!=bus:raise ValueError('industrial bus authorities disagree')
        keys={signature(e) for e in bus['entities']}
        for plan in (docs['factory']['plan'],docs['factory']['red_plan'],docs['red']['plan'],docs['green']['plan'],docs['board']['blocks']['logistic_science']):
            if not keys<={signature(e) for e in plan['entities']}:raise ValueError('industrial authority lacks complete existing bus')
        return bus,docs['board']

    def _material(self,obs,key,item):
        path=self.path.parent/'material-sources.json'
        if not path.exists():raise ValueError('industrial material sources not commissioned')
        saved=json.loads(path.read_text(encoding='utf-8'))
        if saved.get('world_id')!=obs['world_id'] or saved.get('catalog_fingerprint')!=self.catalog.fingerprint:
            raise ValueError('industrial material registry world/catalog changed')
        source=saved.get('sources',{}).get(key)
        if (not isinstance(source,dict) or source.get('item')!=item or source.get('direction')!='output'
                or source.get('world_id')!=obs['world_id'] or not source.get('material_supply_id')
                or type(source.get('unit_number')) is not int or source['unit_number']<1
                or type(source.get('facing')) is not int or source['facing'] not in VECTORS
                or not isinstance(source.get('position'),dict) or set(source['position'])!={'x','y'}
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in source['position'].values())):
            raise ValueError('industrial exact registered '+key+' output required')
        return deepcopy(source)

    def _steel_coal_key(self):
        key=self.state.get('steel_coal_alias','coal')
        if key not in ('coal','steel-coal'):raise ValueError('unsupported industrial coal alias')
        return key

    def _native_ports(self,sources,splitter=None):
        payload=json.dumps(json.dumps({'sources':sources,'splitter':splitter},separators=(',',':')))
        return self.game.query('''local args=helpers.json_to_table('''+payload+''')
for _,spec in ipairs(args.sources) do local e=target(spec.position,"transport-belt")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=spec.unit_number or e.direction~=spec.facing
  or e.position.x~=spec.position.x or e.position.y~=spec.position.y then return {ok=false,reason="industrial_source_identity_changed"} end
 for lane=1,2 do for _,v in pairs(e.get_transport_line(lane).get_contents()) do
  if v.name~=spec.item then return {ok=false,reason="industrial_source_contaminated"} end
 end end
end
if args.splitter then local spec=args.splitter;local e=target(spec.position,"splitter")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=spec.unit_number or e.direction~=0
  or e.position.x~=spec.position.x or e.position.y~=spec.position.y
  or e.splitter_input_priority~="left" or e.splitter_output_priority~="left" then return {ok=false,reason="industrial_surplus_priority_changed"} end
end
return {ok=true,tick=game.tick}''')

    def prepare_steel_supply(self,obs):
        error=self._error(obs)
        if error:return error
        try:
            coal=self._material(obs,self._steel_coal_key(),'coal');bus,_=self._bus(obs)
            native=self._native_ports([coal])
            if not native.get('ok'):return native
            demand=steel_rates(self.catalog)['iron_per_minute']
            if self.tap.state and self.tap.state.get('parameters',{}).get('name')==STEEL_TAP_NAME:
                if self.tap.state['world_id']!=obs['world_id'] or self.tap.state['catalog_fingerprint']!=self.catalog.fingerprint:
                    return {'ok':False,'reason':'industrial_tap_receipt_identity_changed'}
                return {'ok':True,'prepared':deepcopy(self.tap.state),'parameters':deepcopy(self.tap.state['parameters']),'resuming':True}
            if self.tap.state and self.tap.state.get('phase')!='verified':return {'ok':False,'reason':'another_bus_tap_is_unfinished'}
            if any(p['name']==STEEL_TAP_NAME for p in bus['ports']):return {'ok':False,'reason':'steel_tap_has_no_matching_installation_receipt'}
            choices=free_tap_parameters(bus,name=STEEL_TAP_NAME,item='iron-plate',demand_per_minute=demand)
            budget=PlanningBudget(self.game);adapter=self._planning_builder(budget,obs)
            preview=BusTap(budget.game,adapter,self.catalog);failures=[]
            for index,parameters in enumerate(choices[:MAX_TAP_CANDIDATES],1):
                if budget.expired():return budget.failure(attempts=index-1,failures=failures[-4:])
                ready=preview.prepare(obs,parameters)
                if ready.get('ok'):return {'ok':True,'parameters':parameters,'prepared':ready,'attempts':index}
                failures.append({'parameters':parameters,'reason':ready.get('reason'),'stage':ready.get('stage')})
            return budget.failure('industrial_planning_budget_exhausted' if budget.expired() else 'no_valid_free_steel_bus_station',
                attempts=min(len(choices),MAX_TAP_CANDIDATES),candidate_attempt_budget=MAX_TAP_CANDIDATES,failures=failures[-4:])
        except (OSError,KeyError,ValueError,TypeError) as exc:
            return {'ok':False,'reason':'industrial_steel_supply_unavailable','detail':str(exc)}

    def _planning_builder(self,budget,obs):
        return SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan,
            can_place=lambda entities:preflight(budget.game,self.builder,obs,entities))

    def _supply(self,obs):
        bus,_=self._bus(obs);coal=self._material(obs,self._steel_coal_key(),'coal')
        if self.tap.state and self.tap.state.get('phase')!='verified' and self.tap.state.get('parameters',{}).get('name')!=STEEL_TAP_NAME:
            raise ValueError('another shared bus tap cutover is unfinished')
        cached=self.state.get('steel_sources')
        if cached:
            iron=deepcopy(cached['iron'])
            if _source_identity(coal)!=_source_identity(cached['coal']):raise ValueError('commissioned industrial coal source changed')
        else:
            if not self.tap.state or self.tap.state.get('parameters',{}).get('name')!=STEEL_TAP_NAME or self.tap.state.get('phase')!='verified':
                raise ValueError('commissioned steel bus tap required')
            proof=self.tap.proof(obs)
            if not proof:raise ValueError('fresh steel bus tap proof required')
            iron=dict(proof['port'],world_id=obs['world_id'])
        wanted=next((p for p in bus['ports'] if p['name']==STEEL_TAP_NAME),None)
        if (not wanted or wanted.get('item')!='iron-plate' or wanted.get('priority')!='surplus'
                or wanted.get('demand_per_minute')!=steel_rates(self.catalog)['iron_per_minute']
                or iron.get('priority')!='surplus' or iron.get('item')!='iron-plate'
                or iron.get('position')!=wanted['position'] or iron.get('facing')!=wanted['facing']):
            raise ValueError('steel shared bus output contract changed')
        station=next(row for row in bus['stations'] if row['name']==STEEL_TAP_NAME)
        if iron['priority_splitter']['position']!=station['splitter']['position']:
            raise ValueError('steel shared bus splitter changed')
        native=self._native_ports([iron,coal],iron['priority_splitter'])
        if not native.get('ok'):raise ValueError(native.get('reason','industrial source inspection failed'))
        return {'iron':iron,'coal':coal,'native':native}

    def adopt_shared_coal_source(self,obs,source,proof):
        """Explicit proven handoff migration; an installed steel plant is fixed."""
        error=self._error(obs)
        if error:return {'status':'blocked',**error}
        saved_steel=json.loads(self.steel.path.read_text(encoding='utf-8')) if self.steel.path.exists() else {}
        if self.steel.state or saved_steel:return {'status':'blocked','reason':'industrial_coal_adoption_requires_unbuilt_steel'}
        try:
            registered=self._material(obs,'steel-coal','coal');legacy=self._material(obs,'coal','coal');brick=self._material(obs,'brick-coal','coal')
            native=proof.get('native_shared_outputs',{})
            if (_source_identity(source)!=_source_identity(registered) or _source_identity(proof.get('source',{}))!=_source_identity(source)
                    or source['material_supply_id']!=brick['material_supply_id']
                    or source.get('nominal_shared_rate_per_minute')!=brick.get('nominal_shared_rate_per_minute')
                    or not native.get('ok') or native.get('steel_coal',0)<=0 or native.get('brick_coal',0)<=0
                    or not proof.get('brick_production')):
                return {'status':'blocked','reason':'industrial_shared_coal_commissioning_receipt_invalid'}
            cached=self.state.get('steel_sources',{}).get('coal')
            if cached and _source_identity(cached) not in (_source_identity(legacy),_source_identity(source)):
                return {'status':'blocked','reason':'industrial_previous_coal_handoff_changed'}
            check=self._native_ports([source])
            if not check.get('ok'):return {'status':'blocked',**check}
            splitter=source['shared_header']['splitter'];payload=json.dumps(json.dumps(splitter,separators=(',',':')))
            header=self.game.query('''local spec=helpers.json_to_table('''+payload+''');local e=target(spec.position,"splitter")
if not e or e.force~=f or e.surface~=s or e.unit_number~=spec.unit_number or e.direction~=4
 or e.splitter_input_priority~="right" or e.splitter_output_priority~="none" then return {ok=false,reason="industrial_shared_coal_header_changed"} end
return {ok=true}''')
            if not header.get('ok'):return {'status':'blocked',**header}
            self._save(obs)
            if 'steel_sources' in self.state:self.state['steel_sources']['coal']=deepcopy(source)
            self.state['steel_coal_alias']='steel-coal'
            if self.state.get('coal_adoption',{}).get('to')!=_source_identity(source):
                self.state['coal_adoption']={'from':_source_identity(cached or legacy),'to':_source_identity(source),
                    'native_outputs_tick':native.get('tick'),'brick_proof_tick':proof['brick_production'].get('tick'),
                    'reason':'explicit shared header commissioning; fixed unused original outlet retained'}
            self._save(obs)
            return None
        except (OSError,KeyError,ValueError,TypeError) as exc:
            return {'status':'blocked','reason':'industrial_shared_coal_adoption_unavailable','detail':str(exc)}

    def steel_supply(self,obs):
        error=self._error(obs)
        if error:return {'status':'blocked',**error}
        try:
            if self.state.get('steel_sources'):
                return {'status':'succeeded','reason':'exact typed steel supply remains commissioned','evidence':self._supply(obs)}
            ready=self.prepare_steel_supply(obs)
            if not ready.get('ok'):return {'status':'blocked',**ready}
            if not self.tap.state or self.tap.state.get('parameters')!=ready['parameters']:
                self.tap.state=deepcopy(ready['prepared']);_atomic_json(self.tap.path,self.tap.state)
            self._save(obs);self.state['steel_tap_parameters']=deepcopy(ready['parameters']);self._save(obs)
            result=self.tap.step(obs,ready['parameters'])
            if result.get('status')!='succeeded':return result
            sources=self._supply(obs);self.state['steel_sources']={key:sources[key] for key in ('iron','coal')};self._save(obs)
            return {'status':'succeeded','reason':'native surplus iron tap and registered coal supply commissioned','evidence':sources}
        except (OSError,KeyError,ValueError,TypeError) as exc:
            return {'status':'blocked','reason':'industrial_steel_supply_changed','detail':str(exc)}

    def steel_supply_proof(self,obs):
        if self._error(obs):return None
        try:return self._supply(obs)
        except (OSError,KeyError,ValueError,TypeError):return None

    def _prepare_production(self,obs,kind):
        error=self._error(obs)
        if error:return error
        try:
            controller=self.steel if kind=='steel' else self.brick
            bus,board=self._bus(obs);budget=PlanningBudget(self.game)
            if kind=='steel':
                sources=self._supply(obs)
                kwargs={'iron_source':sources['iron'],'coal_source':sources['coal']};product='steel-plate'
                preview=SteelProduction(budget.game,self._planning_builder(budget,obs),self.catalog,**kwargs)
            else:
                stone=self._material(obs,'stone','stone');coal=self._material(obs,'brick-coal','coal')
                kwargs={'stone_source':stone,'coal_source':coal};product='stone-brick'
                native=self._native_ports([stone,coal])
                if not native.get('ok'):return native
                preview=BrickProduction(budget.game,self._planning_builder(budget,obs),self.catalog,**kwargs)
            if controller.state:
                saved=deepcopy(controller.state)
                if saved.get('world_id')!=obs['world_id'] or saved.get('catalog_fingerprint')!=self.catalog.fingerprint:
                    return {'ok':False,'reason':'industrial_producer_checkpoint_identity_changed'}
                requested=list(kwargs.values())
                if any(_source_identity(a)!=_source_identity(b) for a,b in zip(saved['sources'],requested)):
                    return {'ok':False,'reason':'industrial_producer_source_identity_changed'}
                check=preflight(budget.game,self.builder,obs,saved['plan']['entities'])
                return dict(saved,resuming=True) if check.get('ok') else check
            anchors=industrial_site_anchors(bus,board,product)[:MAX_SITE_CANDIDATES]
            ready=preview.prepare(obs,anchors=anchors,**kwargs)
            if budget.expired():
                result=budget.failure(site_candidate_budget=len(anchors),native_reason=ready.get('reason'))
                result['evidence']={**{key:value for key,value in result.items() if key not in ('ok','reason')},
                                    'native_preparation':ready.get('evidence',{'failures':ready.get('failures',[])})}
                return result
            return ready
        except (OSError,KeyError,ValueError,TypeError) as exc:
            return {'ok':False,'reason':'industrial_'+kind+'_preparation_unavailable','detail':str(exc)}

    def prepare_steel_production(self,obs):return self._prepare_production(obs,'steel')
    def prepare_brick_production(self,obs):return self._prepare_production(obs,'brick')

    def _production(self,obs,kind):
        error=self._error(obs)
        if error:return {'status':'blocked',**error}
        if self.defense is None:return {'status':'blocked','reason':'industrial_defense_coverage_required'}
        if kind=='steel':
            if not obs.get('technologies',{}).get('steel-processing') or not obs.get('enabled_recipes',{}).get('steel-plate'):
                return {'status':'blocked','reason':'native_steel_processing_required'}
            result=self.steel_supply(obs)
            if result.get('status')!='succeeded':return result
        controller=self.steel if kind=='steel' else self.brick
        ready=deepcopy(controller.state) if controller.state else self._prepare_production(obs,kind)
        if not ready.get('ok'):
            evidence=ready.get('evidence') or {key:ready[key] for key in ('failures','detail','queries','elapsed_seconds','planning_seconds_budget') if key in ready}
            return {'status':'blocked',**ready,'evidence':evidence}
        if ready.get('world_id')!=obs['world_id'] or ready.get('catalog_fingerprint')!=self.catalog.fingerprint:
            return {'status':'blocked','reason':'industrial_producer_checkpoint_identity_changed'}
        requested=([self.state['steel_sources']['iron'],self.state['steel_sources']['coal']] if kind=='steel' else
                   [self._material(obs,'stone','stone'),self._material(obs,'brick-coal','coal')])
        if any(_source_identity(a)!=_source_identity(b) for a,b in zip(ready['sources'],requested)):
            return {'status':'blocked','reason':'industrial_producer_source_identity_changed'}
        if not controller.state:
            controller.state=deepcopy(ready);_atomic_json(controller.path,controller.state)
        self._save(obs)
        scope='industrial:'+kind
        board_path=self.path.parent/'pcb-board.json';board=json.loads(board_path.read_text(encoding='utf-8'))
        key=kind+'_production';plan=controller.state['plan']
        if board.get('world_id')!=obs['world_id'] or board.get('catalog_fingerprint')!=self.catalog.fingerprint:
            return {'status':'blocked','reason':'industrial_board_identity_changed'}
        if board['blocks'].get(key)!=plan:
            if key in board['blocks'] or any(overlap(plan['reserved_area'],b['reserved_area']) for b in board['blocks'].values() if 'reserved_area' in b):
                return {'status':'blocked','reason':'industrial_core_reservation_changed'}
            board['blocks'][key]=plan;_atomic_json(board_path,board)
        action=self.defense.ensure_coverage(obs,scope,[controller.state['core']['reserved_area']])
        if action is not None:return action
        revision=_plan_revision(plan);authorized=self.state.get('producer_authorizations',{}).get(kind)
        if authorized and authorized['plan_revision']!=revision:
            return {'status':'blocked','reason':'industrial_authorized_plan_changed'}
        if not authorized:
            if not self.defense.coverage_proof(obs,scope):return {'status':'blocked','reason':'industrial_native_coverage_proof_required'}
            # New coverage posts can affect an unbuilt adapter. Prove complete
            # coexistence once before producer construction; the normal builder
            # retains its resource cache and per-action collision checks later.
            check=preflight(self.game,self.builder,obs,plan['entities'])
            if not check.get('ok'):return {'status':'blocked',**check,'stage':'industrial_plan_after_defense'}
            self.state.setdefault('producer_authorizations',{})[kind]={'plan_revision':revision,'coverage_scope':scope,'tick':obs['tick']}
            self._save(obs)
        result=controller.step(obs)
        if result.get('status')!='succeeded':return result
        proof=self.steel_production_proof(obs) if kind=='steel' else self.brick_production_proof(obs)
        return {'status':'succeeded','reason':'normally supplied defended '+kind+' production commissioned','evidence':proof} if proof else {
            'status':'waiting','reason':'waiting_for_fresh_defended_'+kind+'_production_proof'}

    def steel_production(self,obs):
        try:return self._production(obs,'steel')
        except (OSError,KeyError,ValueError,TypeError) as exc:
            return {'status':'blocked','reason':'industrial_steel_controller_unavailable','detail':str(exc)}

    def brick_production(self,obs):
        try:return self._production(obs,'brick')
        except (OSError,KeyError,ValueError,TypeError) as exc:
            return {'status':'blocked','reason':'industrial_brick_controller_unavailable','detail':str(exc)}

    def steel_production_proof(self,obs):
        supply=self.steel_supply_proof(obs)
        coverage=self.defense.coverage_proof(obs,'industrial:steel') if self.defense is not None else None
        production=self.steel.proof(obs) if not self._error(obs) else None
        return {'supply':supply,'coverage':coverage,'production':production} if supply and coverage and production else None

    def brick_production_proof(self,obs):
        if self._error(obs):return None
        try:
            sources=[self._material(obs,'stone','stone'),self._material(obs,'brick-coal','coal')]
            if not self.brick.state or any(_source_identity(a)!=_source_identity(b) for a,b in zip(self.brick.state['sources'],sources)):
                return None
        except (OSError,KeyError,ValueError,TypeError):return None
        coverage=self.defense.coverage_proof(obs,'industrial:brick') if self.defense is not None else None
        production=self.brick.proof(obs)
        return {'coverage':coverage,'production':production} if coverage and production else None
