"""Proactive strategic gun posts, normally paid ammunition and bounded upkeep.

The initial perimeter is an actor-supplied bootstrap. It is not an automatic
ammunition factory or evidence of a framework combat victory.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re

from factorio_ai.deterministic_state import _atomic_json
from .defense_coverage import COVERAGE_MARGIN, coverage_cells, coverage_report, native_candidate_survey, select_cover


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


def protected_factory_areas(game,world_id,catalog_fingerprint,*,runtime_dir=None):
    """Protect future blocks, external planned hardware and fixed-port exits."""
    from factorio_ai.deterministic_builder import FactoryBuilder
    root=runtime_dir if runtime_dir is not None else getattr(getattr(game,'cfg',None),'runtime_dir',None)
    if root is None:return []
    root=Path(root);path=root/'pcb-board.json'
    if not path.exists():return []
    board=json.loads(path.read_text(encoding='utf-8'))
    if board.get('world_id')!=world_id or board.get('catalog_fingerprint')!=catalog_fingerprint:
        raise ValueError('defense_board_identity_changed')
    blocks=list(board.get('blocks',{}).values());bus=board.get('shared_main_bus',{})
    areas=[deepcopy(b['reserved_area'])for b in blocks if b.get('reserved_area')]
    areas += [deepcopy(r['area'])for r in bus.get('reservations',[])if r.get('area')]
    geometry=FactoryBuilder.__new__(FactoryBuilder)
    cells=geometry._occupied_by_plan([e for b in blocks+[bus]for e in b.get('entities',[])])
    ports=[p for b in blocks+[bus]for p in b.get('ports',[])]
    for filename in('material-sources.json','fluid-sources.json'):
        registry_path=root/filename
        if not registry_path.exists():continue
        registry=json.loads(registry_path.read_text(encoding='utf-8'))
        if registry.get('world_id')!=world_id or registry.get('catalog_fingerprint')!=catalog_fingerprint:
            raise ValueError('defense_source_registry_identity_changed')
        ports += list(registry.get('sources',{}).values())
    vectors={0:(0,-1),4:(1,0),8:(0,1),12:(-1,0)}
    for port in ports:
        if port.get('facing')not in vectors or port.get('direction')not in('input','output'):continue
        p=port['position'];dx,dy=vectors[port['facing']]
        if port['direction']=='input':dx,dy=-dx,-dy
        containing=[a for a in areas if a[0][0]<=p['x']<=a[1][0]and a[0][1]<=p['y']<=a[1][1]]
        length=2
        for step in range(1,5):
            q={'x':p['x']+step*dx,'y':p['y']+step*dy}
            if containing and not any(q['x']+.49>a[0][0]and q['x']-.49<a[1][0]
                    and q['y']+.49>a[0][1]and q['y']-.49<a[1][1]for a in containing):
                length=max(length,step);break
        cells.update((p['x']+i*dx,p['y']+i*dy)for i in range(length+1))
    rows={}
    for x,y in cells:rows.setdefault(y,set()).add(x)
    active={};last=None
    for y,xs in sorted(rows.items()):
        if last is not None and y!=last+1:areas.extend(active.values());active={}
        runs=[]
        for x in sorted(xs):
            if runs and x==runs[-1][1]+1:runs[-1]=(runs[-1][0],x)
            else:runs.append((x,x))
        remaining={}
        for lo,hi in runs:
            run=(lo,hi);rectangle=active.pop(run,None)
            if rectangle:rectangle[1][1]=y+.5
            else:rectangle=[[lo-.5,y-.5],[hi+.5,y+.5]]
            remaining[run]=rectangle
        areas.extend(active.values());active=remaining;last=y
    areas.extend(active.values());return areas


class DefensePerimeter:
    def __init__(self, game, bootstrap, catalog):
        self.game, self.bootstrap, self.catalog = game, bootstrap, catalog
        self.ammo_source = None
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
  row.health=e.health;row.max_health=e.max_health;row.status=e.status;row.ammo=0;row.ammo_rounds=0;row.full_magazines=0
  local inv=e.get_inventory(defines.inventory.turret_ammo)
  if inv then for i=1,#inv do local stack=inv[i]
   if stack.valid_for_read and stack.name=="firearm-magazine" and stack.quality.name=="normal" then
    row.ammo=row.ammo+stack.count;row.ammo_rounds=row.ammo_rounds+(stack.count-1)*stack.prototype.magazine_size+stack.ammo
    row.full_magazines=row.full_magazines+stack.count-(stack.ammo<stack.prototype.magazine_size and 1 or 0)
   end
  end end
 else row.missing=true;row.ammo=0;row.ammo_rounds=0;row.full_magazines=0 end
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
                return self._ensure_magazines(obs, report, needed)
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

    def _ensure_magazines(self, obs, report, count):
        source = getattr(self, 'ammo_source', None)
        if source is not None and source.commissioned:
            action = source.ensure_full_magazines(obs, count)
            return action if action is not None else _result('waiting', 'factory_magazines_available_reobserve_before_loading')
        return self.bootstrap.ensure_item(self._ammo_observation(obs, report), 'firearm-magazine', count)

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
                return self._ensure_magazines(obs, report, AMMO_TARGET)
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

    def _coverage_reservations(self, obs, areas):
        reserved = deepcopy(list(FUTURE_AREAS)) + deepcopy(areas)
        reserved.extend(protected_factory_areas(self.game,obs['world_id'],self.catalog.fingerprint,runtime_dir=self.path.parent))
        for scope in self.state.get('coverage_scopes', {}).values():
            reserved.extend(deepcopy(scope['areas']))
        return reserved

    def prepare_coverage(self, obs, scope_id, areas):
        """Read-only additive planning; every complete cell fits in one disk."""
        error = self._identity_error(obs)
        if error:
            return {'ok': False, 'reason': error}
        if not isinstance(scope_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', scope_id):
            return {'ok': False, 'reason': 'invalid_defense_coverage_scope_id'}
        try:
            cells = coverage_cells(areas)
        except ValueError as exc:
            return {'ok': False, 'reason': 'invalid_defense_coverage_areas', 'detail': str(exc)}
        if not self.state:
            return {'ok': False, 'reason': 'initial_defense_perimeter_required'}
        prior = self.state.get('coverage_scopes', {}).get(scope_id)
        if prior:
            if prior['areas'] != areas:
                return {'ok': False, 'reason': 'defense_coverage_scope_geometry_changed'}
            return {'ok': True, 'scope': deepcopy(prior), 'new_posts': [], 'existing': True}
        if not all(obs.get('enabled_recipes', {}).get(item) for item in ('gun-turret', 'firearm-magazine')):
            return {'ok': False, 'reason': 'native_gun_turret_and_normal_magazine_recipes_required'}
        report = self.inspect(obs)
        if not self._proof(report):
            return {'ok': False, 'reason': 'existing_defense_perimeter_not_healthy', 'native': report}
        try:
            reserved = self._coverage_reservations(obs, areas)
            radius = self.state['range'] - COVERAGE_MARGIN
            if not 0 < radius <= 128:
                raise ValueError('native defense range outside bounded survey budget')
            bounds = [[math.floor(min(a[0][0] for a in areas)-radius-3),
                       math.floor(min(a[0][1] for a in areas)-radius-3)],
                      [math.ceil(max(a[1][0] for a in areas)+radius+3),
                       math.ceil(max(a[1][1] for a in areas)+radius+3)]]
            if (bounds[1][0]-bounds[0][0]+1)*(bounds[1][1]-bounds[0][1]+1) > 65536:
                raise ValueError('local defense walk-grid budget exceeded; split distant scopes')
            candidates = []
            for x in range(bounds[0][0]+2, bounds[1][0]-1):
                for y in range(bounds[0][1]+2, bounds[1][1]-1):
                    position = {'x': x, 'y': y}
                    if any(all(math.hypot(cx-x, cy-y) <= radius for cx, cy in c['corners']) for c in cells):
                        candidates.append(position)
            if len(candidates) > 12000:
                raise ValueError('defense placement candidate budget exceeded')
        except ValueError as exc:
            return {'ok': False, 'reason': 'defense_coverage_survey_budget_or_board_failed', 'detail': str(exc)}
        native = native_candidate_survey(self.game, candidates, areas, reserved, bounds)
        if not native.get('ok'):
            return {'ok': False, 'reason': native.get('reason', 'native_defense_coverage_survey_failed')}
        if (native.get('range') != self.state['range']
                or type(native.get('magazine_size')) not in (int, float) or native['magazine_size'] <= 0):
            return {'ok': False, 'reason': 'native_defense_range_or_magazine_prototype_changed'}
        existing = deepcopy(self.state['posts'])
        chosen = select_cover(cells, existing, native.get('candidates', []), radius)
        if chosen is None:
            return {'ok': False, 'reason': 'no_clear_accessible_complete_defense_coverage',
                    'native_candidates': len(native.get('candidates', [])), 'effective_range': radius}
        new_posts = []
        for i, site in enumerate(chosen):
            new_posts.append({**site, 'name': 'coverage_' + scope_id + '_' + str(i+1),
                              'coverage_scope': scope_id, 'commissioned': False})
        cover = select_cover(cells, [], existing+new_posts, radius,
                             maximum_new_posts=len(existing)+len(new_posts))
        if cover is None:
            return {'ok': False, 'reason': 'defense_coverage_selection_failed'}
        scope = {'areas': deepcopy(areas), 'cells': cells, 'effective_range': radius,
                 'native_range': native['range'], 'coverage_margin': COVERAGE_MARGIN,
                 'magazine_size': native['magazine_size'], 'prepared_tick': obs.get('tick'),
                 'covering_posts': [p['name'] for p in cover], 'new_posts': [p['name'] for p in new_posts],
                 'commissioned': False, 'access_scope': native.get('access_scope'),
                 'survey_bounds': bounds,
                 'geometry': coverage_report(cells, existing+new_posts, radius)}
        return {'ok': True, 'scope': scope, 'new_posts': new_posts,
                'requirements': {'gun-turret': len(new_posts), 'firearm-magazine': AMMO_TARGET*len(new_posts)}}

    def _coverage_evidence(self, report, scope, *, initial=False):
        if not self._proof(report):
            return None
        live = {p['name']: p for p in report['posts']}
        names = set(scope['covering_posts']) | set(scope['new_posts'])
        selected = [live[name] for name in names if name in live]
        if len(selected) != len(names):
            return None
        if initial and any(p.get('full_magazines', -1) < AMMO_TARGET for p in selected):
            return None
        geometry = coverage_report(scope['cells'], selected, scope['effective_range'])
        if not geometry['complete']:
            return None
        return {'tick': report['tick'], 'geometry': geometry, 'posts': selected,
                'initial_full_normal_magazines': AMMO_TARGET, 'ongoing_refill_below': REFILL_BELOW,
                'access_scope': scope.get('access_scope'),
                'scope': 'geometric armed-post coverage at this snapshot; no combat-stress certification'}

    def ensure_coverage(self, obs, scope_id, areas):
        """Ordinary paid deployment gate; None means this scope is ready."""
        error = self._identity_error(obs)
        if error:
            return _result('blocked', error)
        prepared = self.prepare_coverage(obs, scope_id, areas)
        if not self.state and prepared.get('reason') == 'initial_defense_perimeter_required':
            action = self.step(obs)
            if action.get('status') == 'succeeded':
                return _result('waiting', 'initial_perimeter_ready_prepare_additive_coverage_next')
            return action
        if not prepared.get('ok'):
            return _result('blocked', prepared['reason'], native=prepared)
        if not prepared.get('existing'):
            self.state['posts'].extend(deepcopy(prepared['new_posts']))
            self.state.setdefault('coverage_scopes', {})[scope_id] = deepcopy(prepared['scope'])
            self._save()
        scope = self.state['coverage_scopes'][scope_id]
        report = self.inspect(obs)
        if not report.get('ok'):
            return _result('blocked', report.get('reason', 'native_defense_status_failed'))
        if report.get('changed'):
            return _result('blocked', 'defense_post_identity_changed', posts=report['changed'])
        by_name = {p['name']: p for p in report['posts']}
        for spec in self.state['posts']:
            live = by_name.get(spec['name'], {})
            if live.get('missing') and (spec.get('unit_number') or spec.get('lost_unit_number')):
                return _result('blocked', 'existing_defense_post_missing_requires_explicit_repair', post=spec['name'])
        self._bind(report)
        if not scope['commissioned']:
            names = set(scope['covering_posts']) | set(scope['new_posts'])
            for spec in self.state['posts']:
                live = by_name.get(spec['name'], {})
                if spec['name'] not in names or live.get('missing'):
                    continue
                if 'full_magazines' not in live:
                    return _result('blocked', 'native_full_normal_magazine_evidence_required', post=spec['name'])
                needed = AMMO_TARGET-live['full_magazines']
                if needed <= 0:
                    continue
                held = report['actor_full_magazines']
                if not held:
                    return self._ensure_magazines(obs, report, needed)
                return {'type': 'insert', 'name': 'gun-turret', 'position': spec['position'],
                        'item': 'firearm-magazine', 'inventory': 'turret_ammo', 'count': min(held, needed),
                        'expected_entity_unit': live['unit_number'], 'expected_entity_world_id': obs['world_id'],
                        'reason': 'load ten full normal magazines before industrial coverage ' + scope_id}
        evidence = self._coverage_evidence(report, scope, initial=not scope['commissioned'])
        if evidence:
            if not scope['commissioned']:
                scope['commissioned'] = True
                scope['commissioned_tick'] = report['tick']
                self._save()
            return None
        action = self.step(obs)
        if action.get('type') == 'build':
            try:
                reserved = self._coverage_reservations(obs, areas)
            except ValueError as exc:
                return _result('blocked', str(exc))
            native = native_candidate_survey(self.game, [action['position']], areas, reserved, scope['survey_bounds'])
            if (not native.get('ok') or not any(p.get('position') == action['position']
                                               for p in native.get('candidates', []))):
                return _result('blocked', 'prepared_defense_coverage_site_or_access_changed', position=action['position'])
        if action.get('status') == 'succeeded':
            return _result('waiting', 'waiting_for_complete_native_coverage_proof')
        return action

    def coverage_proof(self, obs, scope_id):
        scope = self.state.get('coverage_scopes', {}).get(scope_id)
        if not scope or not scope.get('commissioned'):
            return None
        return self._coverage_evidence(self.inspect(obs), scope)
