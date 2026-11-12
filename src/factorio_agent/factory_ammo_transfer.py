"""Collect full normal piercing magazines from the exact commissioned factory."""
from __future__ import annotations

import json
from pathlib import Path

from factorio_ai.world_catalog import WorldCatalog
from .ammo_transfer import TAKE_FULL_MAGAZINES_LUA,validate_ammo_action


ACTION='take_factory_magazines'
ITEM='piercing-rounds-magazine'


def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])


def _context(game,action):
    if not isinstance(action,dict):raise ValueError('exact factory ammo transfer envelope required')
    allowed={'type','name','position','item','count','source_slots','expected_entity_unit','expected_entity_world_id',
             'expected_actor_unit','expected_catalog_fingerprint','reason'}
    if(set(action)-allowed or action.get('type')!=ACTION or action.get('item')!=ITEM
            or not isinstance(action.get('expected_catalog_fingerprint'),str)or not action['expected_catalog_fingerprint']):
        raise ValueError('exact typed piercing factory collector required')
    proxy={k:v for k,v in action.items()if k!='expected_catalog_fingerprint'}
    proxy.update(type='take_full_magazines',item='firearm-magazine')
    validate_ammo_action(proxy,kind='take_full_magazines',expected_world=getattr(game,'expected_world',None))
    if getattr(game,'backend',None)not in('assisted','character'):raise ValueError('ordinary factory collector backend required')
    root=Path(game.cfg.runtime_dir);producer=json.loads((root/'piercing-ammo.json').read_text(encoding='utf-8'))
    board=json.loads((root/'pcb-board.json').read_text(encoding='utf-8'))
    catalog=WorldCatalog.from_dict(json.loads((root/'catalog.json').read_text(encoding='utf-8')))
    world,fingerprint=action['expected_entity_world_id'],action['expected_catalog_fingerprint']
    if(getattr(game,'expected_world',None)!=world or catalog.fingerprint!=fingerprint
            or any(q.get('world_id')!=world or q.get('catalog_fingerprint')!=fingerprint for q in(producer,board))
            or producer.get('phase')!='verified'or not isinstance(producer.get('plan'),dict)
            or board.get('blocks',{}).get('piercing_ammo')!=producer.get('plan')):
        raise ValueError('commissioned piercing factory world, catalog or PCB authority changed')
    chest=producer.get('core',{}).get('chest',{})
    if(chest.get('name')!=action['name']or chest.get('position')!=action['position']
            or producer.get('bindings',{}).get(_key(chest))!=action['expected_entity_unit']
            or chest not in producer['plan'].get('entities',[])):
        raise ValueError('factory collector must target its exact bound output chest')
    return action


def factory_magazine_action(game,obs,count,source_slots):
    """Read-only emitter; native operation rechecks the known producer and slots."""
    if not obs.get('ok',True):raise ValueError('fresh successful factory stock observation required')
    state=json.loads((Path(game.cfg.runtime_dir)/'piercing-ammo.json').read_text(encoding='utf-8'))
    chest=state['core']['chest']
    action=dict(type=ACTION,name=chest['name'],position=dict(chest['position']),item=ITEM,count=count,source_slots=list(source_slots),
        expected_entity_unit=state['bindings'].get(_key(chest)),expected_entity_world_id=obs.get('world_id'),
        expected_actor_unit=obs.get('actor_unit_number'),expected_catalog_fingerprint=state.get('catalog_fingerprint'),
        reason='normally collect full piercing magazines from the exact commissioned assembler output')
    return _context(game,action)


# Preserve the proven yellow helper and its public string interface verbatim.
TAKE_FACTORY_MAGAZINES_LUA=TAKE_FULL_MAGAZINES_LUA.replace('"firearm-magazine"','"piercing-rounds-magazine"')
TAKE_FACTORY_MAGAZINES_LUA=TAKE_FACTORY_MAGAZINES_LUA.replace('for i=1,#dest do',r'''
for i=1,#source do local stack=source[i]
 if stack.valid_for_read and not plain(stack)then return reject("factory_ammo_output_contaminated_or_metadata_changed")end
end
for i=1,#dest do''',1)
TAKE_FACTORY_MAGAZINES_LUA=TAKE_FACTORY_MAGAZINES_LUA.replace('actor_unit=a.unit_number,quality="normal",',
    'actor_unit=a.unit_number,quality="normal",item=x.item,catalog_fingerprint=x.expected_catalog_fingerprint,')


def execute_take_factory_magazines(game,action):
    payload=_context(game,action)
    encoded=json.dumps(json.dumps(payload,separators=(',',':')))
    return game.query('local x=helpers.json_to_table('+encoded+');local character_backend='
        +('true'if game.backend=='character'else'false')+';'+TAKE_FACTORY_MAGAZINES_LUA)
