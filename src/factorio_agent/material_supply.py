"""Independent, paid coal/stone drill pairs and identity-bound material ports."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .board import entity
from .paired_mining import prepare_new_bank
from .planning import overlap, preflight
from .smelting import port, merge_entities
from .transport import xy
from .resource_policy import annotate_mining_exit, mining_exit_candidates, MINING_EXIT_LUA
from .power_grid import plan_connection as power_connection


def validate_material(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {'resource'} or parameters['resource'] not in ('coal', 'stone'):
        raise ValueError('dedicated_materials requires resource coal or stone')


def _same_plan(a, b):
    return a['plan'] == b['plan'] and a.get('reserved_area') == b.get('reserved_area')


class DedicatedMaterials:
    def __init__(self, game, builder, catalog, outposts, *, perimeter=None):
        self.game, self.builder, self.catalog, self.outposts = game, builder, catalog, outposts
        self.perimeter = perimeter
        self.path = Path(game.cfg.runtime_dir)/'material-supply.json'
        self.sources_path = self.path.parent/'material-sources.json'
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def _save(self):
        _atomic_json(self.path, self.state)

    def _outposts(self):
        path = Path(self.outposts.path)
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else deepcopy(self.outposts.state)

    def _valid(self, state, world):
        return state.get('world_id') == world and state.get('catalog_fingerprint', self.catalog.fingerprint) == self.catalog.fingerprint

    def _coal_hub(self, source, reservations, planned=(), mining_area=None):
        zones = [{'area': area, 'resource': '__other_reserved_area__'} for area in reservations]
        if mining_area:
            zones.append({'area': mining_area, 'resource': 'coal'})
        payload = json.dumps(json.dumps({'source': source, 'areas': reservations, 'zones': zones,
            'planned': list(planned), 'exit_candidates': mining_exit_candidates(source['position'], extra_clear_steps=4)}, separators=(',', ':')))
        return self.game.query('''
local args=helpers.json_to_table('''+payload+''');
'''+MINING_EXIT_LUA+'''
local function hub_clear(p)
 local x=p.x;local y=p.y;local area={{x+.5,y-1.5},{x+4.5,y+2.5}};local reserved=false
 for _,r in ipairs(args.areas) do
  if area[1][1]<r[2][1] and area[2][1]>r[1][1] and area[1][2]<r[2][2] and area[2][2]>r[1][2] then reserved=true;break end
 end
 for _,spec in ipairs(args.planned)do
  local proto=prototypes.entity[spec.name];local w=spec._width or proto.tile_width;local h=spec._height or proto.tile_height
  if spec.direction==4 or spec.direction==12 then w,h=h,w end
  local q=spec.position
  if q.x-w/2<area[2][1]and q.x+w/2>area[1][1]and q.y-h/2<area[2][2]and q.y+h/2>area[1][2]then return false end
 end
 for _,offset in ipairs{{3,0},{4,0},{4,-1},{3,1},{4,1},{4,2}}do
  if not s.can_place_entity{name="transport-belt",position={x=x+offset[1],y=y+offset[2]},direction=4,force=f}then return false end
 end
 return not reserved and s.count_entities_filtered{area=area,type="resource"}==0
  and s.can_place_entity{name="splitter",position={x=x+1,y=y+.5},direction=4,force=f}
  and s.can_place_entity{name="transport-belt",position={x=x+2,y=y},direction=4,force=f}
  and s.can_place_entity{name="transport-belt",position={x=x+2,y=y+1},direction=4,force=f}
  and s.can_place_entity{name="transport-belt",position={x=x+3,y=y},direction=4,force=f}
  and s.can_place_entity{name="transport-belt",position={x=x+3,y=y+1},direction=4,force=f}
end
return select_mining_exit(args.source,args.zones,{},args.planned,args.exit_candidates,hub_clear,false,4)
''')

    @staticmethod
    def _hub_geometry(source, hub):
        path = hub.get('exit_prefix')
        if path is None:
            x, y = xy(source)
            path = [{'x': x+i, 'y': y} for i in range(hub['offset']+1)]
        else:
            path = path + (hub.get('hub_connector') or [])
        last = path[-1]
        return path, last['x'], last['y']

    def _distribution_area(self, bank, hub):
        path, x, y = self._hub_geometry(bank['plan']['ports'][0], hub)
        area = deepcopy(bank['reserved_area'])
        area[0][0] = min(area[0][0], min(p['x']-.5 for p in path))
        area[0][1] = min(area[0][1], y-1.5, min(p['y']-.5 for p in path))
        area[1][0] = max(area[1][0], x+4.5, max(p['x']+.5 for p in path))
        area[1][1] = max(area[1][1], y+2.5, max(p['y']+.5 for p in path))
        return area

    @staticmethod
    def _output_access(x,y):
        return {'coal':[{'x':x+2,'y':y},{'x':x+3,'y':y},{'x':x+4,'y':y},{'x':x+4,'y':y-1}],
                'brick-coal':[{'x':x+2,'y':y+1},{'x':x+3,'y':y+1},{'x':x+4,'y':y+1},{'x':x+4,'y':y+2}]}

    @staticmethod
    def _published_power_keepouts(banks):
        points=set()
        for bank in banks:
            distribution=bank.get('material_distribution',{})
            paths=distribution.get('output_access')
            if paths:
                points.update((p['x'],p['y'])for path in paths.values()for p in path[1:])
            else:
                ports=list(distribution.get('ports',{}).values())or bank['plan']['ports']
                for source in ports:
                    if source.get('direction')=='output'and source.get('facing')==4:
                        x,y=xy(source);points.update(((x+1,y),(x+2,y)))
        return [entity('transport-belt',x,y,4)for x,y in sorted(points)]

    def prepare(self, obs, parameters):
        validate_material(parameters)
        resource = parameters['resource']
        if not obs.get('technologies', {}).get('electric-mining-drill'):
            return {'ok': False, 'reason': 'native_electric_mining_required'}
        saved = self._outposts()
        if not self._valid(saved, obs['world_id']):
            return {'ok': False, 'reason': 'material_outposts_world_or_catalog_changed'}
        board_path = self.path.parent/'pcb-board.json'
        board = json.loads(board_path.read_text(encoding='utf-8'))
        if not self._valid(board, obs['world_id']):
            return {'ok': False, 'reason': 'material_board_world_or_catalog_changed'}
        supply_id = 'dedicated-material:'+obs['world_id']+':'+resource
        existing = [b for b in saved.get('banks', []) if b.get('material_supply_id') == supply_id]
        if len(existing) > 1:
            return {'ok': False, 'reason': 'duplicate_dedicated_material_bank_identity'}
        if existing:
            bank = deepcopy(existing[0])
            if bank['plan'].get('resource') != resource or not bank['plan'].get('paired'):
                return {'ok': False, 'reason': 'dedicated_material_bank_configuration_changed'}
        else:
            reservations = [b['reserved_area'] for b in board.get('blocks', {}).values() if b.get('reserved_area')]
            reservations += [r['area'] for r in board.get('shared_main_bus', {}).get('reservations', [])]
            reservations += [b.get('reserved_area', b['plan'].get('reserved_area')) for b in saved.get('banks', [])
                             if b.get('reserved_area', b['plan'].get('reserved_area'))]
            planning_reservations = reservations
            if resource == 'coal':
                planning_reservations = [[[r[0][0]-2, r[0][1]], list(r[1])] for r in reservations]
            hub = None
            failures = []
            for attempt in range(6 if resource == 'coal' else 1):
                bank = prepare_new_bank(self.game, self.builder, obs, resource, 2, planning_reservations)
                if not bank.get('ok'):
                    return dict(bank, candidate_failures=failures, attempted_banks=attempt+1)
                if resource != 'coal':
                    break
                hub = self._coal_hub(bank['plan']['ports'][0], reservations, bank['plan']['entities'], bank['reserved_area'])
                if hub.get('ok'):
                    grown = self._distribution_area(bank, hub)
                    if not any(overlap(grown, r) for r in reservations):
                        break
                    hub = {'ok': False, 'reason': 'coal_distribution_reservation_conflict'}
                failures.append(hub.get('reason', 'coal_hub_unavailable'))
                planning_reservations = planning_reservations+[deepcopy(bank['reserved_area'])]
            else:
                return {'ok': False, 'reason': 'no_independent_coal_bank_with_ore_free_hub',
                        'candidate_failures': failures, 'attempted_banks': 6}
            bank = deepcopy(bank)
            bank.update(material_supply_id=supply_id, ready=False, consumer_connection=None)
            drills = [e for row in bank['plan']['producer_rows'].values() for e in row]
            drill_positions = {xy(e) for e in drills}
            pole_positions = {(x+2, y) for x, y in drill_positions}
            output_x, output_y = xy(bank['plan']['ports'][0])
            left_x = min(x for x, _ in drill_positions)-1
            core = [e for e in bank['plan']['entities'] if
                    (e['name'] == 'electric-mining-drill' and xy(e) in drill_positions)
                    or (e['name'] == 'small-electric-pole' and xy(e) in pole_positions)
                    or (e['name'] == 'transport-belt' and xy(e)[1] == output_y and left_x <= xy(e)[0] <= output_x)]
            occupied = self.builder._occupied_by_plan(core)
            core_area = [[min(x for x, _ in occupied)-.5, min(y for _, y in occupied)-.5],
                         [max(x for x, _ in occupied)+.5, max(y for _, y in occupied)+.5]]
            bank['plan']['mining_footprint'] = core_area
            core_keys = {(e['name'], *xy(e)) for e in core}
            for spec in bank['plan']['entities']:
                if (spec['name'], *xy(spec)) in core_keys:
                    spec['mining_resource'] = resource
                    spec['mining_footprint'] = core_area
                    spec['resource_role'] = 'mining_core'
                else:
                    spec.pop('mining_resource', None); spec.pop('mining_footprint', None)
            if resource == 'coal':
                source = bank['plan']['ports'][0]
                bank['plan']['mining_reserved_area'] = deepcopy(bank['reserved_area'])
                path, hub_x, hub_y = self._hub_geometry(source, hub)
                mining_path = hub.get('exit_prefix', path)
                if len(path)>1:
                    vectors = {(0, -1): 0, (1, 0): 4, (0, 1): 8}
                    directions = [vectors[(b['x']-a['x'], b['y']-a['y'])] for a,b in zip(path,path[1:])] + [4]
                else:
                    directions = [4]
                splitter = dict(entity('splitter', hub_x+1, hub_y+.5, 4), _width=2, _height=1,
                                splitter_input_priority='none', splitter_output_priority='none')
                additions = [entity('transport-belt', p['x'], p['y'], directions[i]) for i,p in enumerate(path) if i]
                additions += [splitter, entity('transport-belt', hub_x+2, hub_y, 4), entity('transport-belt', hub_x+2, hub_y+1, 4)]
                area = self._distribution_area(bank, hub)
                if any(overlap(area, r) for r in reservations):
                    return {'ok': False, 'reason': 'coal_distribution_reservation_conflict'}
                es = merge_entities(core, additions)
                access=self._output_access(hub_x,hub_y)
                empty={(p['x'],p['y'])for path in access.values()for p in path[1:]}
                old_bridge=[e for e in bank['plan']['entities']if(e['name'],*xy(e))not in core_keys]
                if empty & self.builder._occupied_by_plan(es+old_bridge):
                    return {'ok':False,'reason':'coal_output_access_obstructed_by_prospective_entity'}
                keepouts=self._published_power_keepouts(saved.get('banks',[]))
                keepouts += [entity('transport-belt',x,y,4)for x,y in sorted(empty)]
                pole=next(e for e in core if e['name']=='small-electric-pole')
                power=power_connection(self.game,self.builder,obs,pole['position'],
                    [e for e in es if e['name']!='small-electric-pole']+keepouts)
                if not power.get('ok'):
                    return {'ok':False,'reason':'coal_power_bridge_cannot_preserve_output_access','power':power}
                es=merge_entities(power['entities'],es)
                if empty & self.builder._occupied_by_plan(es):
                    return {'ok':False,'reason':'coal_output_access_obstructed_by_prospective_entity'}
                bank['wire_hops']=power['wire_hops']
                last_resource = hub.get('exit_length', hub.get('last_resource_step', 0))
                if last_resource >= 0 and len(mining_path)>1:
                    annotate_mining_exit(es, resource, source['position'], last_resource, path=mining_path)
                    approved = [e for e in es if e.get('mining_exit')]
                    for spec in approved:
                        spec['resource_role'] = 'mining_escape'
                    bank['plan']['resource_escape_area'] = [[min(p['x'] for p in mining_path[:-1])-.5, min(p['y'] for p in mining_path[:-1])-.5],
                                                           [max(p['x'] for p in mining_path[:-1])+.5, max(p['y'] for p in mining_path[:-1])+.5]]
                check = preflight(self.game, self.builder, obs, es)
                if not check.get('ok'):
                    return {'ok': False, 'reason': 'whole_coal_distribution_preflight_failed', 'preflight': check}
                old = bank['plan']
                extra = {k: v for k, v in old.items() if k not in ('ok', 'reason', 'entities', 'ports', 'required_items', 'clear_obstacles')}
                obstacles = merge_entities(old.get('clear_obstacles', []), check.get('clear_obstacles', []))
                bank['plan'] = _plan(es, old['ports'], **extra, clear_obstacles=obstacles,output_access=access)
                bank['reserved_area'] = area
                bank['material_distribution'] = {'splitter': splitter,
                    'ports': {'coal': port('coal', hub_x+2, hub_y, 4, 'output'), 'brick-coal': port('coal', hub_x+2, hub_y+1, 4, 'output')},
                    'output_access':access,'output_access_policy':'reserve two clear east cells then independent outward turns; no power or production entities'}
        return {'ok': True, 'world_id': obs['world_id'], 'catalog_fingerprint': self.catalog.fingerprint,
                'resource': resource, 'material_supply_id': supply_id, 'bank': bank,
                'entity_units': deepcopy(bank.get('material_entity_units', [])), 'phase': 'build'}

    def _register(self, obs, entry, *, ready=False):
        saved = self._outposts()
        if not self._valid(saved, obs['world_id']):
            return {'status': 'blocked', 'reason': 'material_outposts_world_or_catalog_changed'}
        matches = [b for b in saved.get('banks', []) if b.get('material_supply_id') == entry['material_supply_id']]
        if len(matches) > 1 or (matches and not _same_plan(matches[0], entry['bank'])):
            return {'status': 'blocked', 'reason': 'registered_material_bank_changed'}
        bank = deepcopy(entry['bank'])
        bank['ready'] = ready
        bank['material_entity_units'] = deepcopy(entry['entity_units'])
        if matches:
            saved['banks'][saved['banks'].index(matches[0])] = bank
        else:
            saved.setdefault('banks', []).append(bank)
        saved.setdefault('catalog_fingerprint', self.catalog.fingerprint)
        self.outposts.state = saved
        _atomic_json(self.outposts.path, saved)
        result = self.outposts.publish_reservations(obs)
        if result:
            return result
        return None

    def _bind(self, obs, entry):
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        bound = {(e['name'], *xy(e)): e for e in entry['entity_units']}
        for spec in entry['bank']['plan']['entities']:
            if spec['name'] == 'small-electric-pole':
                continue
            key = spec['name'], *xy(spec)
            live, previous = actual.get(key), bound.get(key)
            if previous and (not live or live.get('unit_number') != previous['unit_number']):
                return {'status': 'blocked', 'reason': 'dedicated_material_entity_identity_changed', 'position': spec['position']}
            if live:
                if live.get('direction', 0) != spec.get('direction', 0) or type(live.get('unit_number')) is not int:
                    return {'status': 'blocked', 'reason': 'dedicated_material_entity_configuration_changed'}
                if not previous:
                    entry['entity_units'].append({k: live[k] for k in ('name', 'position', 'unit_number')})
        return None

    def step(self, obs, parameters):
        validate_material(parameters)
        if self.state and not self._valid(self.state, obs['world_id']):
            return {'status': 'blocked', 'reason': 'material_supply_world_or_catalog_changed'}
        if not self.state:
            self.state = {'world_id': obs['world_id'], 'catalog_fingerprint': self.catalog.fingerprint, 'supplies': {}}
        resource = parameters['resource']
        if resource not in self.state['supplies']:
            prepared = self.prepare(obs, parameters)
            if not prepared.get('ok'):
                return {'status': 'blocked', **prepared}
            self.state['supplies'][resource] = prepared
            self._save()
        entry = self.state['supplies'][resource]
        result = self._bind(obs, entry)
        if result:
            return result
        self._save()
        result = self._register(obs, entry, ready=entry['phase'] == 'verified')
        if result:
            return result
        if self.perimeter is not None:
            result = self.perimeter.ensure_coverage(obs, entry['material_supply_id'], [entry['bank']['reserved_area']])
            if result is not None:
                return result
        result = self.builder.ensure_plan(obs, entry['bank']['plan'])
        if result.get('status') != 'succeeded':
            return result
        proof = self.proof(obs, parameters)
        if not proof:
            return {'status': 'waiting', 'reason': 'waiting for independent powered material drill rows and both native collector lanes'}
        result = self._register(obs, entry, ready=True)
        if result:
            return result
        saved = json.loads(self.sources_path.read_text(encoding='utf-8')) if self.sources_path.exists() else {
            'world_id': obs['world_id'], 'catalog_fingerprint': self.catalog.fingerprint, 'sources': {}}
        if not self._valid(saved, obs['world_id']):
            return {'status': 'blocked', 'reason': 'material_source_registry_identity_changed'}
        for key, value in proof['sources'].items():
            old = saved['sources'].get(key)
            if old and (old.get('material_supply_id') != entry['material_supply_id'] or old.get('unit_number') != value['unit_number']):
                return {'status': 'blocked', 'reason': 'published_material_output_identity_changed'}
            saved['sources'][key] = value
        _atomic_json(self.sources_path, saved)
        entry['phase'] = 'verified'; entry['proof'] = proof; self._save()
        return {'status': 'succeeded', 'reason': 'independent normally built material bank publishes proven typed outputs', 'evidence': proof}

    def proof(self, obs, parameters):
        validate_material(parameters)
        if not self.state or not self._valid(self.state, obs['world_id']):
            return None
        entry = self.state.get('supplies', {}).get(parameters['resource'])
        if not entry or self._bind(obs, entry):
            return None
        defense = None
        if self.perimeter is not None:
            defense = self.perimeter.coverage_proof(obs, entry['material_supply_id'])
            if defense is None:
                return None
        bank = entry['bank']
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        if any((e['name'], *xy(e)) not in actual for e in bank['plan']['entities']):
            return None
        native = self.outposts.bank_proof(obs, bank)
        lanes = (native or {}).get('native_producer_lanes') or {}
        item = parameters['resource']
        if not lanes.get('ok') or any(set(lanes.get(side, {}))-{item} for side in ('left', 'right')):
            return None
        drill_units = native['drill_units']
        if all(lanes.get(side, {}).get(item, 0)>0 for side in ('left', 'right')):
            entry['lane_witness'] = {'tick': obs['tick'], 'drill_units': drill_units}
        witness = entry.get('lane_witness')
        if not witness or witness['drill_units'] != drill_units or not 0 <= obs['tick']-witness['tick'] <= 600:
            return None
        ports = {'stone': bank['plan']['ports'][0]} if item == 'stone' else bank['material_distribution']['ports']
        if item == 'coal':
            spec = bank['material_distribution']['splitter']
            splitter = actual.get(('splitter', *xy(spec)))
            if not splitter or any(splitter.get(k) != 'none' for k in ('splitter_input_priority', 'splitter_output_priority')):
                return None
        sources = {}
        for key, spec in ports.items():
            live = actual.get(('transport-belt', *xy(spec)))
            if not live or live.get('direction') != spec['facing']:
                return None
            sources[key] = dict(spec, name='transport-belt', unit_number=live['unit_number'],
                world_id=obs['world_id'], material_supply_id=entry['material_supply_id'],
                nominal_shared_rate_per_minute=bank['nominal_rate_per_minute'],
                capacity_scope='shared bank capacity; separate outlets do not multiply production')
        return {'tick': obs['tick'], 'resource': item, 'material_supply_id': entry['material_supply_id'],
                'sources': sources, 'native_bank': native, 'both_lane_witness': deepcopy(witness),
                'defense_coverage': defense,
                'scope': 'normal independent extraction and typed output commissioning; nominal rates are not measured throughput'}
