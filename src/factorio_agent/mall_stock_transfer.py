"""Exact-slot normal construction goods from a commissioned capped mall."""
import json
import math
from pathlib import Path

from factorio_ai.world_catalog import WorldCatalog

ACTION='take_mall_stock'
PRODUCTS=('transport-belt','gun-turret')


def _key(q):return q['name']+':'+str(q['position']['x'])+':'+str(q['position']['y'])


def _context(game,action):
    allowed={'type','name','position','item','count','source_slots','expected_entity_unit','expected_entity_world_id',
             'expected_actor_unit','expected_catalog_fingerprint','reason'}
    if(not isinstance(action,dict)or set(action)-allowed or action.get('type')!=ACTION or action.get('item')not in PRODUCTS
            or action.get('name')!='iron-chest' or type(action.get('count'))is not int or not 1<=action['count']<=50
            or any(type(action.get(k))is not int or action[k]<1 for k in('expected_entity_unit','expected_actor_unit'))
            or not isinstance(action.get('source_slots'),list)or not 1<=len(action['source_slots'])<=16
            or any(type(i)is not int or not 1<=i<=48 for i in action['source_slots'])
            or len(set(action['source_slots']))!=len(action['source_slots'])
            or not isinstance(action.get('position'),dict)or set(action['position'])!={'x','y'}
            or any(type(v)not in(int,float)or not math.isfinite(v)for v in action['position'].values())
            or not isinstance(action.get('expected_entity_world_id'),str)or not action['expected_entity_world_id']
            or not isinstance(action.get('expected_catalog_fingerprint'),str)or not action['expected_catalog_fingerprint']
            or getattr(game,'backend',None)not in('assisted','character')):
        raise ValueError('bounded exact owned mall stock envelope required')
    root=Path(game.cfg.runtime_dir);state=json.loads((root/'construction-mall.json').read_text(encoding='utf-8'))
    board=json.loads((root/'pcb-board.json').read_text(encoding='utf-8'))
    catalog=WorldCatalog.from_dict(json.loads((root/'catalog.json').read_text(encoding='utf-8')))
    world=action['expected_entity_world_id'];fp=action['expected_catalog_fingerprint'];item=action['item']
    entry=state.get('products',{}).get(item,{})
    if(getattr(game,'expected_world',None)!=world or catalog.fingerprint!=fp
            or any(q.get('world_id')!=world or q.get('catalog_fingerprint')!=fp for q in(state,entry,board))
            or entry.get('phase')!='verified' or board.get('blocks',{}).get('construction_mall_'+item)!=entry.get('plan')):
        raise ValueError('commissioned mall world/catalog/producer/PCB authority changed')
    chest=entry.get('core',{}).get('chest',{})
    if(chest.get('name')!=action['name']or chest.get('position')!=action['position']
            or entry.get('bindings',{}).get(_key(chest))!=action['expected_entity_unit']or chest not in entry['plan']['entities']):
        raise ValueError('mall collector must use its exact paid output chest')
    return action


def mall_stock_action(game,obs,item,count,slots):
    state=json.loads((Path(game.cfg.runtime_dir)/'construction-mall.json').read_text(encoding='utf-8'))
    entry=state['products'][item];chest=entry['core']['chest']
    action=dict(type=ACTION,name=chest['name'],position=chest['position'],item=item,count=count,source_slots=list(slots),
                expected_entity_unit=entry['bindings'][_key(chest)],expected_entity_world_id=obs['world_id'],
                expected_actor_unit=obs['actor_unit_number'],expected_catalog_fingerprint=state['catalog_fingerprint'],
                reason='collect normally produced construction stock from its exact capped owned output')
    return _context(game,action)


TAKE_MALL_STOCK_LUA=r'''
local function reject(reason)return {ok=false,status="blocked",reason=reason,moved=0}end
if not d or d.world_id~=x.expected_entity_world_id then return reject("mall_stock_world_changed")end
if not a or not a.valid or a.unit_number~=x.expected_actor_unit or a.force~=f or a.surface~=s then return reject("mall_stock_actor_changed")end
local e=target(x.position,x.name)
if not e or not e.valid or e.unit_number~=x.expected_entity_unit or e.force~=f or e.surface~=s
 or e.position.x~=x.position.x or e.position.y~=x.position.y or e.quality.name~="normal"then return reject("mall_stock_chest_changed")end
if character_backend and not a.can_reach_entity(e)then return reject("mall_stock_out_of_reach")end
local source=e.get_inventory(defines.inventory.chest);local dest=a.get_main_inventory()
if not source or not dest then return reject("mall_stock_inventory_missing")end
local function plain(st)return st.valid_for_read and st.name==x.item and st.prototype.type=="item"
 and st.quality.name=="normal"and st.health==1 and st.spoil_tick==0
 and not st.is_item_with_tags and not st.is_item_with_inventory and not st.is_item_with_entity_data end
for i=1,#source do if source[i].valid_for_read and not plain(source[i])then return reject("mall_stock_source_contaminated")end end
for i=1,#dest do if dest[i].valid_for_read and dest[i].name==x.item and not plain(dest[i])then return reject("mall_stock_destination_metadata_conflict")end end
for _,i in ipairs(x.source_slots)do if i>#source or not plain(source[i])then return reject("mall_stock_slot_changed")end end
local function total(inv)local n=0;for i=1,#inv do if plain(inv[i])then n=n+inv[i].count end end;return n end
local before=total(source)+total(dest);local moved=0
for _,i in ipairs(x.source_slots)do
 if moved>=x.count then break end
 local st=source[i];local wanted=math.min(st.count,x.count-moved)
 local inserted=dest.insert{name=x.item,count=wanted,quality="normal"}
 if type(inserted)~="number"or inserted<0 or inserted>wanted or inserted%1~=0 then error("unexpected_native_insert_receipt")end
 if inserted>0 then st.count=st.count-inserted;moved=moved+inserted end
 if inserted<wanted then break end
end
if before~=total(source)+total(dest)then error("mall_stock_conservation_failed")end
return {ok=true,status=moved>0 and"succeeded"or"waiting",tick=game.tick,moved=moved,requested=x.count,item=x.item,
 unit_number=e.unit_number,actor_unit=a.unit_number,world_id=d.world_id,catalog_fingerprint=x.expected_catalog_fingerprint,quality="normal"}
'''


def execute_take_mall_stock(game,action):
    payload=_context(game,action)
    encoded=json.dumps(json.dumps(payload,separators=(',',':')))
    try:
        result=game.query('local x=helpers.json_to_table('+encoded+');local character_backend='
                          +('true'if game.backend=='character'else'false')+';'+TAKE_MALL_STOCK_LUA)
    except Exception:return {'ok':False,'status':'blocked','reason':'mall_stock_transfer_outcome_unknown'}
    if not isinstance(result,dict)or type(result.get('ok'))is not bool:
        return {'ok':False,'status':'blocked','reason':'mall_stock_transfer_outcome_unknown'}
    if not result['ok'] and(result.get('moved')!=0 or result.get('status')!='blocked' or result.get('reason')not in{
            'mall_stock_world_changed','mall_stock_actor_changed','mall_stock_chest_changed','mall_stock_out_of_reach',
            'mall_stock_inventory_missing','mall_stock_source_contaminated','mall_stock_destination_metadata_conflict','mall_stock_slot_changed'}):
        return {'ok':False,'status':'blocked','reason':'mall_stock_transfer_outcome_unknown'}
    if result['ok'] and(type(result.get('moved'))is not int or not 0<=result['moved']<=action['count']
            or result.get('requested')!=action['count']or result.get('item')!=action['item']
            or result.get('unit_number')!=action['expected_entity_unit']or result.get('world_id')!=action['expected_entity_world_id']
            or result.get('actor_unit')!=action['expected_actor_unit']or result.get('catalog_fingerprint')!=action['expected_catalog_fingerprint']
            or result.get('quality')!='normal'or result.get('status')!=('succeeded'if result['moved']else'waiting')):
        return {'ok':False,'status':'blocked','reason':'mall_stock_transfer_outcome_unknown'}
    return result
