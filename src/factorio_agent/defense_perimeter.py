"""Proactive strategic gun posts, normally paid ammunition and bounded upkeep.

The initial perimeter is an actor-supplied bootstrap. It is not an automatic
ammunition factory or evidence of a framework combat victory.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from factorio_ai.deterministic_state import _atomic_json


AMMO_TARGET = 10
REFILL_BELOW = 5
POSTS = (
    ('west_smelter', (-50.5, -26.5), (-44, -26)),
    ('north_smelter', (-32.5, -43.5), (-32, -32)),
    ('coal_south', (-54.5, 31.5), (-56, 17)),
    ('main_bus_middle', (-20.5, -65.5), (-11.5, -65.5)),
    ('iron_north', (14.5, -81.5), (16.5, -71.5)),
    ('steam_south', (43.5, 18.5), (38, 8)),
    ('labs_north', (32.5, -127.5), (32.5, -111.5)),
    ('compact_green_east', (77.5, -68.5), (65.5, -68.5)),
)
FUTURE_AREAS = (
    [[-16.5, -119.5], [-1.5, -10.5]],
    [[39, -82], [71, -54]],
)


def _point(pair):
    return {'x': pair[0], 'y': pair[1]}


def _result(status, reason, **evidence):
    return {'status': status, 'reason': reason, 'evidence': evidence}


class DefensePerimeter:
    def __init__(self, game, bootstrap, catalog):
        self.game, self.bootstrap, self.catalog = game, bootstrap, catalog
        self.path = Path(game.cfg.runtime_dir) / 'defense-perimeter.json'
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def _identity_error(self, obs):
        if not obs.get('ok', True) or not isinstance(obs.get('world_id'), str) or not obs['world_id']:
            return 'defense_requires_valid_world_observation'
        if self.state and (self.state.get('world_id') != obs['world_id']
                           or self.state.get('catalog_fingerprint') != self.catalog.fingerprint):
            return 'defense_perimeter_world_or_catalog_changed'
        return None

    def prepare(self, obs):
        """Read-only all-post native land/range/resource/reservation survey."""
        error = self._identity_error(obs)
        if error:
            return {'ok': False, 'reason': error}
        if not all(obs.get('enabled_recipes', {}).get(item) for item in ('gun-turret', 'firearm-magazine')):
            return {'ok': False, 'reason': 'native_gun_turret_and_normal_magazine_recipes_required'}
        reserved = deepcopy(list(FUTURE_AREAS))
        board_path = self.path.parent / 'pcb-board.json'
        if board_path.exists():
            board = json.loads(board_path.read_text(encoding='utf-8'))
            if board.get('world_id') != obs['world_id'] or board.get('catalog_fingerprint') != self.catalog.fingerprint:
                return {'ok': False, 'reason': 'defense_board_identity_changed'}
            reserved.extend(b['reserved_area'] for b in board.get('blocks', {}).values() if 'reserved_area' in b)
        posts = [{'name': name, 'anchor': _point(anchor), 'focus': _point(focus)} for name, anchor, focus in POSTS]
        payload = json.dumps(json.dumps({'posts': posts, 'reserved': reserved}, separators=(',', ':')))
        result = self.game.query('''
local args=helpers.json_to_table(''' + payload + ''');local proto=prototypes.entity["gun-turret"]
if not proto or not proto.attack_parameters or proto.attack_parameters.range<=2 then return {ok=false,reason="native_turret_range_unavailable"} end
local range=proto.attack_parameters.range;local coverage=range-2;local chosen={}
for _,post in ipairs(args.posts) do
 local best=nil;local best_score=math.huge;local ax=math.floor(post.anchor.x+.5);local ay=math.floor(post.anchor.y+.5)
 for dx=-8,8 do for dy=-8,8 do
  local p={x=ax+dx,y=ay+dy};local legal=(p.x-post.focus.x)^2+(p.y-post.focus.y)^2<=coverage^2
  local area={{p.x-proto.tile_width/2,p.y-proto.tile_height/2},{p.x+proto.tile_width/2,p.y+proto.tile_height/2}}
  if legal then for _,box in ipairs(args.reserved) do
   if area[1][1]<box[2][1] and area[2][1]>box[1][1] and area[1][2]<box[2][2] and area[2][2]>box[1][2] then legal=false;break end
  end end
  if legal then for _,previous in ipairs(chosen) do
   if (p.x-previous.position.x)^2+(p.y-previous.position.y)^2<9 then legal=false;break end
  end end
  local old=legal and target(p,"gun-turret") or nil
  if old and (old.force~=f or old.position.x~=p.x or old.position.y~=p.y) then old=nil end
  if legal and (old or s.can_place_entity{name="gun-turret",position=p,direction=0,force=f})
   and s.count_entities_filtered{area=area,type="resource"}==0 then
   local score=(p.x-post.anchor.x)^2*100+(p.y-post.anchor.y)^2*100+(p.x-post.focus.x)^2+(p.y-post.focus.y)^2
   if old then score=score-100000 end
   if score<best_score then best_score=score;best={name=post.name,anchor=post.anchor,focus=post.focus,position=p,unit_number=old and old.unit_number or nil} end
  end
 end end
 if not best then return {ok=false,reason="no_clear_strategic_turret_site",post=post.name,anchor=post.anchor} end
 chosen[#chosen+1]=best
end
return {ok=true,range=range,coverage_margin=2,posts=chosen}
''')
        if not result.get('ok'):
            return result
        return {**result, 'schema_version': 1, 'world_id': obs['world_id'],
                'catalog_fingerprint': self.catalog.fingerprint, 'prepared_tick': obs.get('tick'),
                'reserved_areas': reserved, 'ammo_target': AMMO_TARGET, 'refill_below': REFILL_BELOW,
                'phase': 'deploy', 'requirements': {'gun-turret': len(result['posts']),
                                                   'firearm-magazine': len(result['posts']) * AMMO_TARGET},
                'scope': 'proactive actor-supplied strategic posts; no automatic ammo or combat acceptance'}

    def inspect(self, obs):
        """Read-only native status, including exact identity and normal ammo."""
        error = self._identity_error(obs)
        if error:
            return {'ok': False, 'reason': error}
        if not self.state:
            return {'ok': True, 'initialized': False, 'posts': [], 'missing': [], 'unarmed': [], 'refill': []}
        payload = json.dumps(json.dumps(self.state['posts'], separators=(',', ':')))
        result = self.game.query('''
local specs=helpers.json_to_table(''' + payload + ''');local posts={};local held=0;local partial=0
local main=a and a.valid and a.get_main_inventory() or nil
if main then for i=1,#main do local stack=main[i]
 if stack.valid_for_read and stack.name=="firearm-magazine" and stack.quality.name=="normal" then
  local used=stack.ammo<stack.prototype.magazine_size and 1 or 0
  held=held+stack.count-used;partial=partial+used
 end
end end
for _,spec in ipairs(specs) do
 local row={name=spec.name,position=spec.position,expected_unit=spec.unit_number}
 local e=target(spec.position,"gun-turret")
 if e and e.force==f and e.position.x==spec.position.x and e.position.y==spec.position.y then
  row.unit_number=e.unit_number;row.identity_changed=spec.unit_number and spec.unit_number~=e.unit_number or false
  row.health=e.health;row.max_health=e.max_health;row.status=e.status;row.ammo=0;row.ammo_rounds=0
  local inv=e.get_inventory(defines.inventory.turret_ammo)
  if inv then for i=1,#inv do local stack=inv[i]
   if stack.valid_for_read and stack.name=="firearm-magazine" and stack.quality.name=="normal" then
    row.ammo=row.ammo+stack.count;row.ammo_rounds=row.ammo_rounds+(stack.count-1)*stack.prototype.magazine_size+stack.ammo
   end
  end end
 else row.missing=true;row.ammo=0;row.ammo_rounds=0 end
 row.nearby_enemies=s.count_entities_filtered{position=spec.position,radius=36,force="enemy",type={"unit","unit-spawner","turret"}}
 posts[#posts+1]=row
end
return {ok=true,tick=game.tick,posts=posts,actor_full_magazines=held,actor_partial_magazines=partial}
''')
        if not result.get('ok'):
            return result
        result.update(initialized=True,
                      missing=[p['name'] for p in result['posts'] if p.get('missing')],
                      unarmed=[p['name'] for p in result['posts'] if not p.get('missing') and p.get('ammo_rounds', 0) <= 0],
                      refill=[p['name'] for p in result['posts'] if not p.get('missing') and p.get('ammo', 0) < REFILL_BELOW],
                      changed=[p['name'] for p in result['posts'] if p.get('identity_changed')],
                      damaged=[p['name'] for p in result['posts'] if p.get('health', 0) < p.get('max_health', 0)])
        return result

    def _save(self):
        _atomic_json(self.path, self.state)

    def _bind(self, report):
        changed = False
        for spec, live in zip(self.state['posts'], report['posts']):
            if live.get('identity_changed'):
                continue
            if live.get('missing'):
                if spec.get('unit_number') is not None:
                    spec['lost_unit_number'] = spec.pop('unit_number')
                    spec['commissioned'] = False
                    changed = True
                continue
            if not spec.get('unit_number'):
                spec['unit_number'] = live['unit_number']
                changed = True
            if live['ammo'] >= AMMO_TARGET and not spec.get('commissioned'):
                spec['commissioned'] = True
                changed = True
        if changed:
            self._save()

    def _refill(self, obs, report, *, initial=False):
        for spec, live in zip(self.state['posts'], report['posts']):
            if live.get('missing') or live.get('identity_changed'):
                continue
            threshold = AMMO_TARGET if initial and not spec.get('commissioned') else REFILL_BELOW
            if live['ammo'] >= threshold:
                continue
            needed = AMMO_TARGET - live['ammo']
            held = report['actor_full_magazines']
            if not held:
                return self.bootstrap.ensure_item(self._ammo_observation(obs, report), 'firearm-magazine', needed)
            return {'type': 'insert', 'name': 'gun-turret', 'position': spec['position'],
                    'item': 'firearm-magazine', 'inventory': 'turret_ammo', 'count': min(held, needed),
                    'expected_entity_unit': live['unit_number'], 'expected_entity_world_id': obs['world_id'],
                    'reason': 'normally load or refill strategic perimeter post ' + spec['name']}
        return None

    @staticmethod
    def _ammo_observation(obs, report):
        # Aggregate inventory counts include partial/high-quality magazines.
        # Ordinary crafting must see only full normal actor stock for this job.
        return {**obs, 'inventory': {**obs.get('inventory', {}),
                                    'firearm-magazine': report['actor_full_magazines']}}

    def upkeep(self, obs):
        """Refill known posts during other jobs; never start a new perimeter."""
        if not self.state:
            return None
        report = self.inspect(obs)
        if not report.get('ok'):
            return _result('blocked', report.get('reason', 'native_defense_status_failed'))
        if report['changed']:
            return _result('blocked', 'defense_post_identity_changed', posts=report['changed'])
        self._bind(report)
        return self._refill(obs, report)

    def step(self, obs):
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get('ok'):
                return _result('blocked', prepared.get('reason', 'defense_preflight_failed'), native=prepared)
            self.state = prepared
            self._save()
        report = self.inspect(obs)
        if not report.get('ok'):
            return _result('blocked', report.get('reason', 'native_defense_status_failed'))
        if report['changed']:
            return _result('blocked', 'defense_post_identity_changed', posts=report['changed'])
        self._bind(report)
        action = self._refill(obs, report, initial=True)
        if action:
            return action
        for spec, live in zip(self.state['posts'], report['posts']):
            if not live.get('missing'):
                continue
            if report['actor_full_magazines'] < AMMO_TARGET:
                return self.bootstrap.ensure_item(self._ammo_observation(obs, report), 'firearm-magazine', AMMO_TARGET)
            if int(obs.get('inventory', {}).get('gun-turret', 0)) < 1:
                return self.bootstrap.ensure_item(obs, 'gun-turret', 1)
            payload = json.dumps(json.dumps(spec['position'], separators=(',', ':')))
            clear = self.game.query('''local p=helpers.json_to_table(''' + payload + ''');return {ok=s.can_place_entity{name="gun-turret",position=p,direction=0,force=f},resources=s.count_entities_filtered{area={{p.x-1,p.y-1},{p.x+1,p.y+1}},type="resource"}}''')
            if not clear.get('ok') or clear.get('resources', 1) > 0:
                return _result('blocked', 'prepared_defense_site_changed', post=spec['name'], position=spec['position'])
            return {'type': 'build', 'name': 'gun-turret', 'position': spec['position'], 'direction': 0,
                    'reason': 'normally build proactive strategic perimeter post ' + spec['name']}
        proof = self._proof(report)
        if not proof:
            return _result('waiting', 'waiting_for_initial_perimeter_magazines', native=report)
        if self.state.get('phase') != 'armed':
            self.state['phase'] = 'armed'
            self._save()
        return _result('succeeded', 'declared strategic posts are normally armed', **proof)

    def _proof(self, report):
        if (not report.get('ok') or not self.state or len(report.get('posts', [])) != len(self.state['posts'])
                or report.get('missing') or report.get('changed') or report.get('unarmed') or report.get('refill')
                or any(not post.get('commissioned') for post in self.state['posts'])):
            return None
        return {'tick': report['tick'], 'posts': report['posts'], 'native_range': self.state['range'],
                'ammo_target': AMMO_TARGET, 'refill_below': REFILL_BELOW,
                'scope': 'strategic armed-post commissioning; actor ammo bootstrap, no automatic-ammo or combat-stress certification'}

    def proof(self, obs):
        return self._proof(self.inspect(obs)) if self.state else None
