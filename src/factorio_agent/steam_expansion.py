"""An additive normally fueled boiler/engine pair; preserve the running plant."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .planning import preflight
from .smelting import port,merge_entities
from .paired_mining import prepare_new_bank,lane_evidence
from .transport import plan_connection,xy,underground_proof


def steam_pair():
    es=[entity('boiler',37.5,1,8),entity('steam-engine',37.5,4.5,8),entity('steam-engine',37.5,9.5,8),
        entity('burner-inserter',37.5,-.5,0),entity('transport-belt',37.5,-1.5,8)]
    es += [entity('small-electric-pole',35.5,y) for y in (1.5,5.5,9.5)]
    es += [entity('small-electric-pole',40.5,5.5)]
    es += [entity('pipe',39.5,.5),entity('pipe-to-ground',40.5,.5,12),entity('pipe-to-ground',44.5,.5,4)]
    return _plan(es,[port('coal',37.5,-1.5,8)],reserved_area=[[35,-4],[45,13]],
                 template='additional_shared_water_steam_pair')


class SteamExpansion:
    def __init__(self,game,builder,catalog,outposts):
        self.game,self.builder,self.catalog,self.outposts=game,builder,catalog,outposts
        self.path=Path(game.cfg.runtime_dir)/'steam-expansion.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def owns_burner(self,e,obs):
        return self.state.get('world_id')==obs['world_id'] and e['name'] in ('boiler','burner-inserter') and any(
            w['name']==e['name'] and w['position']==e['position'] for w in self.state.get('core',{}).get('entities',[]))

    def prepare(self,obs):
        core=steam_pair();check=preflight(self.game,self.builder,obs,core['entities'])
        if not check.get('ok'):return check
        board=json.loads((self.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
        if board.get('world_id')!=obs['world_id']:return {'ok':False,'reason':'steam_board_world_changed'}
        startup=board['blocks'].get('startup_science')
        if startup:
            for e in startup['entities']:
                if e['name']=='small-electric-pole':continue
                if any(p[0]>=35 for p in self.builder._occupied_by_plan([e])):
                    return {'ok':False,'reason':'old_startup_cell_cannot_release_east_reservation'}
        reservations=[b['reserved_area'] for b in board['blocks'].values()]+[core['reserved_area']]
        bank=prepare_new_bank(self.game,self.builder,obs,'coal',2,reservations)
        if not bank.get('ok'):return bank
        es=merge_entities(core['entities'],bank['plan']['entities'])
        route=plan_connection(self.game,self.builder,obs,bank['plan']['ports'][0],core['ports'][0],es)
        if not route.get('ok'):return dict(route,source=bank['plan']['ports'][0],destination=core['ports'][0])
        es=merge_entities(es,route['entities']);check=preflight(self.game,self.builder,obs,es)
        if not check.get('ok'):return check
        pairs=route['underground_pairs']
        plan=_plan(es,core['ports'],resource_cell=True,underground_pairs=pairs,
            clear_obstacles=check['clear_obstacles'],clearance_world_id=obs['world_id'],layout_policy='pcb-additive-steam-v1')
        bank['plan']=_plan(merge_entities(bank['plan']['entities'],route['entities']),bank['plan']['ports'],
            **{k:v for k,v in bank['plan'].items() if k not in ('ok','reason','entities','ports','required_items','clear_obstacles','clearance_world_id','underground_pairs')},
            underground_pairs=pairs,clear_obstacles=check['clear_obstacles'],clearance_world_id=obs['world_id'])
        engines=[e for e in obs['entities'] if e['name']=='steam-engine']
        if not engines or not any(e.get('energy',0)>0 for e in engines):return {'ok':False,'reason':'running_source_plant_required'}
        return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,
                'core':core,'bank':bank,'plan':plan,'samples':[],
                'added_nominal_kw':2*self.catalog.entities['steam-engine']['energy_production']*60/1000,
                'preserved_engine_units':[e['unit_number'] for e in engines]}

    def step(self,obs):
        if not self.state:
            prepared=self.prepare(obs)
            if not prepared.get('ok'):return {'status':'blocked',**prepared}
            self.state=prepared;_atomic_json(self.path,self.state)
        if self.state['world_id']!=obs['world_id'] or self.state['catalog_fingerprint']!=self.catalog.fingerprint:
            return {'status':'blocked','reason':'steam_expansion_world_or_catalog_changed'}
        path=self.path.parent/'pcb-board.json';board=json.loads(path.read_text(encoding='utf-8'))
        if board.get('world_id')!=obs['world_id']:return {'status':'blocked','reason':'steam_expansion_board_world_changed'}
        if 'steam_expansion' not in board['blocks']:
            if 'startup_science' in board['blocks']:board['blocks']['startup_science']['reserved_area'][1][0]=35
            board['blocks']['steam_expansion']=dict(self.state['plan'],reserved_area=self.state['core']['reserved_area'])
            _atomic_json(path,board)
        bank=self.state['bank']
        if bank.get('layout_contract'):
            # Publish the mining-only growth area separately from its clear exit.
            saved=json.loads(self.outposts.path.read_text(encoding='utf-8')) if self.outposts.path.exists() else deepcopy(self.outposts.state)
            if(saved.get('world_id')!=obs['world_id']or saved.get('catalog_fingerprint',self.catalog.fingerprint)!=self.catalog.fingerprint):
                return {'status':'blocked','reason':'steam_coal_registry_identity_changed'}
            matches=[b for b in saved.get('banks',[])if b['plan']['ports']==bank['plan']['ports']]
            if len(matches)>1 or(matches and matches[0]['plan']!=bank['plan']):
                return {'status':'blocked','reason':'steam_fixed_coal_bank_plan_changed'}
            if not matches:
                saved.setdefault('banks',[]).append(dict(deepcopy(bank),ready=False,power_only=True))
                _atomic_json(self.outposts.path,saved)
            self.outposts.state=saved
            reservation=self.outposts.publish_reservations(obs)
            if reservation:return reservation
        from .mining_layout_policy import validate_retained_units
        retained=validate_retained_units(obs,bank)
        if not retained.get('ok'):return dict(retained,status='blocked')
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        sample=self.sample(obs)
        if not sample or not sample.get('ok'):
            self.state['samples']=[];_atomic_json(self.path,self.state)
            temporary={'boiler_waiting_for_automatic_coal_or_water','engine_waiting_for_steam_or_load'}
            if sample and sample.get('reason') and sample['reason'] not in temporary:
                return {'status':'blocked','reason':'steam_native_probe_failed','detail':sample}
            return {'status':'waiting','reason':'waiting for automatic coal, water and native generation from both new engines'}
        history=self.state['samples']
        if history and not 0<sample['tick']-history[-1]['tick']<=600:history.clear()
        if history and [e['unit_number'] for e in sample['engines']]!=[e['unit_number'] for e in history[-1]['engines']]:history.clear()
        history.append(sample)
        while len(history)>2 and history[1]['tick']<=sample['tick']-1800:history.pop(0)
        _atomic_json(self.path,self.state)
        proof=self.proof(obs)
        if not proof:return {'status':'waiting','reason':'sampling native generation from the new engine pair for thirty game seconds'}
        bank=self.state['bank'];bank['ready']=True;bank['power_only']=True
        matches=[b for b in self.outposts.state['banks']if b['plan']['ports']==bank['plan']['ports']]
        if matches and bank.get('layout_contract'):
            matches[0].update(deepcopy(bank));_atomic_json(self.outposts.path,self.outposts.state)
        elif not matches:
            self.outposts.state['banks'].append(bank);_atomic_json(self.outposts.path,self.outposts.state)
        return {'status':'succeeded','reason':'paid additive steam generation has automatic dedicated coal and shared normal water','evidence':proof}

    def sample(self,obs):
        if not underground_proof(obs,self.state['plan']):return None
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        for wanted in self.state['plan']['entities']:
            e=actual.get((wanted['name'],*xy(wanted)))
            if not e:return None
            if wanted['name']=='steam-engine':
                if e['direction']%8!=wanted.get('direction',0)%8:return None
            elif wanted['name'] not in ('pipe','small-electric-pole') and e['direction']!=wanted.get('direction',0):return None
        bank=lane_evidence(self.game,obs,self.state['bank'])
        if not bank.get('ok'):return None
        payload=json.dumps(json.dumps(self.state['core'],separators=(',',':')))
        return self.game.query('''
local core=helpers.json_to_table('''+payload+''');local result={ok=true,tick=game.tick,engines={}}
local function fluid(e,name) local contents=e.get_fluid_contents();return contents[name] or 0 end
for _,spec in ipairs(core.entities) do
 local e=target(spec.position,spec.name)
 if not e then return {ok=false} end
 if e.name=="boiler" then
  local fuel=e.get_fuel_inventory();result.boiler_coal=fuel and fuel.get_item_count("coal") or 0
  result.water=fluid(e,"water");if result.boiler_coal<=0 or result.water<=0 then return {ok=false,reason="boiler_waiting_for_automatic_coal_or_water"} end
 elseif e.name=="steam-engine" then
  local generated=e.energy_generated_last_tick;local steam=fluid(e,"steam")
  if generated<=0 or steam<=0 or not e.is_connected_to_electric_network() then return {ok=false,reason="engine_waiting_for_steam_or_load"} end
  result.engines[#result.engines+1]={unit_number=e.unit_number,kw=generated*60/1000,steam=steam,network=e.electric_network_id}
 end
end
return result
''')

    def proof(self,obs):
        if not self.state or self.state['world_id']!=obs['world_id']:return None
        history=self.state['samples']
        if len(history)<2 or history[-1]['tick']-history[0]['tick']<1800:return None
        if not all(any(e.get('unit_number')==unit and e['name']=='steam-engine' for e in obs['entities']) for unit in self.state['preserved_engine_units']):return None
        fresh=self.sample(obs)
        if not fresh or not fresh.get('ok') or [e['unit_number'] for e in fresh['engines']]!=[e['unit_number'] for e in history[-1]['engines']]:return None
        if not 0<=fresh['tick']-history[-1]['tick']<=600:return None
        return {'tick':fresh['tick'],'from_tick':history[0]['tick'],'to_tick':history[-1]['tick'],
            'added_nominal_kw':self.state['added_nominal_kw'],'latest_native_sample':fresh,
            'dedicated_coal_nominal_per_minute':self.state['bank']['nominal_rate_per_minute'],
            'scope':'native generation from both new engines over thirty-second sampled window; nominal added capacity is not a full-load stress test'}
