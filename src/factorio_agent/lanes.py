"""Explicit lane contracts and ordinary output distribution across both lanes."""
from __future__ import annotations

import json
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .board import entity
from .transport import xy,VECTORS


def inserter_output_lane(pickup_direction,belt_direction,*,mirrored=False,straight=True):
    """Default 2.1 straight-belt rule; corners require native inspection."""
    if not straight:raise ValueError('curved belt drops require native lane evidence')
    if pickup_direction not in VECTORS or belt_direction not in VECTORS:
        raise ValueError('cardinal inserter and belt directions required')
    drop=VECTORS[(pickup_direction+8)%16];flow=VECTORS[belt_direction]
    if drop[0]*flow[0]+drop[1]*flow[1]:return 'left' if mirrored else 'right'
    left=(flow[1],-flow[0])
    return 'left' if drop[0]*left[0]+drop[1]*left[1]>0 else 'right'


def lane_balancer(x,y,item,direction=4):
    """Split the belt, then side-load opposite sides of a straight output.

    A splitter alone preserves lane identity. The two side-load junctions do
    the actual lane distribution. The empty west stub keeps the target straight.
    This accepts either or both input lanes of ONE declared item.
    """
    es=[entity('transport-belt',x,y,4),dict(entity('splitter',x+1,y-.5,4),_width=2,_height=1)]
    es += [entity('transport-belt',x+dx,y-1,4 if dx<5 else 8) for dx in range(2,6)]
    es += [entity('transport-belt',x+2,y,8)]
    es += [entity('transport-belt',x+dx,y+1,4 if dx<5 else 0) for dx in range(2,6)]
    es += [entity('transport-belt',x+dx,y,4) for dx in (4,5,6,7)]
    turns=((direction-4)%16)//4
    if direction not in (0,4,8,12):raise ValueError('cardinal lane-balancer direction required')
    def rotate(dx,dy):
        for _ in range(turns):dx,dy=-dy,dx
        return x+dx,y+dy
    for e in es:
        ex,ey=rotate(e['position']['x']-x,e['position']['y']-y)
        e['position']={'x':ex,'y':ey};e['direction']=(e['direction']+4*turns)%16
    ox,oy=rotate(7,0)
    ports=[{'kind':'item','item':item,'direction':'input','position':{'x':x,'y':y},'facing':direction,
            'lane_policy':'either-or-both lanes, same item'},
           {'kind':'item','item':item,'direction':'output','position':{'x':ox,'y':oy},'facing':direction,
            'lane_policy':'both lanes, same item','lane_items':{'left':item,'right':item}}]
    return _plan(es,ports,template='one_belt_output_lane_distribution',layout_policy='pcb-lane-distribution-v1',
                 scope='output lane distribution; not a claim of equal input-lane draw or saturated throughput')


class SmelterLaneBalance:
    def __init__(self,game,builder,catalog):
        self.game,self.builder,self.catalog=game,builder,catalog
        self.path=Path(game.cfg.runtime_dir)/'smelter-lanes.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def prepare(self,obs):
        root=self.path.parent;smelting=json.loads((root/'smelting-district.json').read_text(encoding='utf-8'))
        board=json.loads((root/'pcb-board.json').read_text(encoding='utf-8'))
        if smelting.get('world_id')!=obs['world_id'] or board.get('world_id')!=obs['world_id'] or board.get('catalog_fingerprint')!=self.catalog.fingerprint:
            return {'ok':False,'reason':'lane_source_world_or_catalog_changed'}
        modules=[]
        for source in smelting['plan']['ports']:
            if source['direction']!='output' or source['facing']!=4:
                return {'ok':False,'reason':'east_boundary_smelter_output_required'}
            source=dict(source,lane_policy='same-side furnace inserters drop on the far lane',
                        lane_items={'left':None,'right':source['item']})
            x,y=source['position']['x']+1,source['position']['y']
            module=lane_balancer(x,y,source['item'])
            module['reserved_area']=[[x+.5,y-2.5],[x+8.5,y+1.5]]
            module['source_port']=source
            area=module['reserved_area']
            if any(area[0][0]<b['reserved_area'][1][0] and area[1][0]>b['reserved_area'][0][0]
                   and area[0][1]<b['reserved_area'][1][1] and area[1][1]>b['reserved_area'][0][1]
                   for b in board['blocks'].values()):
                return {'ok':False,'reason':'lane_module_intrudes_reserved_block'}
            modules.append(module)
        es=[e for module in modules for e in module['entities']]
        boxes=[self.builder._occupied_by_plan([e]) for e in es]
        if sum(map(len,boxes))!=len(set().union(*boxes)):
            return {'ok':False,'reason':'lane_distribution_prospective_overlap'}
        preflight=self.builder.can_place(es)
        if not preflight.get('ok'):return {'ok':False,'reason':'lane_distribution_native_preflight_failed','blocked':preflight.get('blocked')}
        return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'modules':modules,
                'plan':_plan(es,[p for module in modules for p in module['ports']],layout_policy='pcb-smelter-lanes-v1')}

    def step(self,obs):
        if not self.state:
            prepared=self.prepare(obs)
            if not prepared.get('ok'):return {'status':'blocked',**prepared}
            self.state=prepared;_atomic_json(self.path,self.state)
        if self.state['world_id']!=obs['world_id'] or self.state['catalog_fingerprint']!=self.catalog.fingerprint:
            return {'status':'blocked','reason':'lane_distribution_world_or_catalog_changed'}
        board_path=self.path.parent/'pcb-board.json';board=json.loads(board_path.read_text(encoding='utf-8'))
        if board.get('world_id')!=obs['world_id'] or board.get('catalog_fingerprint')!=self.catalog.fingerprint:
            return {'status':'blocked','reason':'lane_distribution_board_changed'}
        changed=False
        for module in self.state['modules']:
            key=module['source_port']['item']+'_lane_distribution'
            if board['blocks'].get(key)!=module:
                if key in board['blocks']:return {'status':'blocked','reason':'lane_distribution_reservation_changed'}
                board['blocks'][key]=module;changed=True
            connection={'item':module['source_port']['item'],'source':module['source_port']['position'],
                        'destination':module['ports'][0]['position'],'owner':key}
            if connection not in board.setdefault('connections',[]):board['connections'].append(connection);changed=True
        if changed:_atomic_json(board_path,board)
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        proof=self.proof(obs)
        if proof:return {'status':'succeeded','reason':'smelter outputs now carry their plate on both lanes','evidence':proof}
        return {'status':'waiting','reason':'waiting for actual plate items in both output lanes'}

    def proof(self,obs):
        if not self.state or self.state['world_id']!=obs['world_id'] or self.state['catalog_fingerprint']!=self.catalog.fingerprint:return None
        by_spec={(e['name'],*xy(e)):e for e in obs['entities']}
        for wanted in self.state['plan']['entities']:
            actual=by_spec.get((wanted['name'],*xy(wanted)))
            if not actual or actual['direction']!=wanted['direction']:return None
        payload=json.dumps(json.dumps(self.state['modules'],separators=(',',':')))
        result=self.game.query('''
local modules=helpers.json_to_table('''+payload+''');local result={}
for _,module in ipairs(modules) do
 local source=target(module.source_port.position,"transport-belt")
 local output=module.ports[2];local belt=target(output.position,"transport-belt")
 if not source or not belt or source.direction~=module.source_port.facing or belt.direction~=output.facing then return {ok=false,reason="lane_port_missing_or_rotated"} end
 local rows={};for _,lane in ipairs({defines.transport_line.left_line,defines.transport_line.right_line}) do
  local items={};for _,item in pairs(belt.get_transport_line(lane).get_contents()) do items[item.name]=(items[item.name] or 0)+item.count end
  rows[#rows+1]=items
 end
 local policies={}
 for _,spec in ipairs(module.entities) do if spec.name=="splitter" then
  local splitter=target(spec.position,spec.name)
  if not splitter or splitter.splitter_input_priority~="none" or splitter.splitter_output_priority~="none" then return {ok=false,reason="lane_splitter_policy_changed"} end
  policies[#policies+1]={unit_number=splitter.unit_number,direction=splitter.direction,
   input_priority=splitter.splitter_input_priority,output_priority=splitter.splitter_output_priority}
 end end
 result[#result+1]={item=output.item,position=output.position,left=rows[1],right=rows[2],splitter_policies=policies}
end
return {ok=true,tick=game.tick,outputs=result}
''')
        if not result.get('ok'):return None
        for output in result['outputs']:
            for lane in ('left','right'):
                items=output[lane]
                if not isinstance(items,dict) or items.get(output['item'],0)<1 or set(items)-{output['item']}:return None
        return {'tick':result['tick'],'outputs':result['outputs'],'entity_directions_verified':len(self.state['plan']['entities']),
                'scope':'native items on both output lanes; splitter plus opposite-side loading; throughput and exact equality require a separate loaded window'}
