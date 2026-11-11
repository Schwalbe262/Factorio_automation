"""One paid priority tap on an existing bus, preserving every other bus asset."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .board import entity
from .main_bus import BusMaterial, ConsumerTap, SourceMerge, PRIORITY_OUTPUT, plan_main_bus
from .planning import preflight
from .routing import route_trunks
from .transport import VECTORS, compile_crossings, underground_proof, xy


def signature(e):
    return (e['name'], *xy(e), e.get('direction', 0), e.get('belt_to_ground_type'),
            e.get('_width'), e.get('_height'), e.get('splitter_input_priority'), e.get('splitter_output_priority'))


def _pair_key(pair):
    return tuple(pair['input']), tuple(pair['output']), pair['direction'], pair['span']


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_parameters(parameters):
    required = {'name', 'item', 'splitter_y', 'priority', 'demand_per_minute'}
    if (not isinstance(parameters, dict) or not required.issubset(parameters) or set(parameters) - required - {'line'}
            or not isinstance(parameters['name'], str) or not re.fullmatch('[a-z][a-z0-9_-]{0,79}', parameters['name'])
            or not isinstance(parameters['item'], str) or not parameters['item']
            or parameters['priority'] not in PRIORITY_OUTPUT
            or type(parameters.get('line', 0)) is not int or parameters.get('line', 0) < 0
            or type(parameters['splitter_y']) not in (int, float) or not math.isfinite(parameters['splitter_y'])
            or parameters['splitter_y'] % 1 != .5
            or type(parameters['demand_per_minute']) not in (int, float)
            or not math.isfinite(parameters['demand_per_minute']) or parameters['demand_per_minute'] < 0):
        raise ValueError('tap requires name, typed item, half-tile splitter_y, native priority, finite demand and active line')


def reconstruct_bus(bus):
    """Recover contracts from saved reservations, capacity and typed stations."""
    if bus.get('template') != 'shared_north_main_bus' or bus.get('flow_direction') != 0:
        raise ValueError('unsupported existing main bus')
    materials, taps, merges = [], [], []
    for reservation in bus['reservations']:
        item, columns = reservation['item'], reservation['columns']
        capacity = bus['capacity']['materials'][item]
        lines = sorted(capacity['lines'], key=lambda row: row['line'])
        if not columns or [row['line'] for row in lines] != list(range(len(lines))):
            raise ValueError('invalid saved parallel-line contract')
        pitch = columns[0] - columns[1] if len(columns) > 1 else 4
        if type(pitch) not in (int, float) or pitch % 1 or pitch < 4:
            raise ValueError('invalid saved expansion pitch')
        material = BusMaterial(item, columns[0], tuple(row['declared_source_per_minute'] for row in lines), len(columns), int(pitch))
        if columns != [material.line_x(i) for i in range(len(columns))]:
            raise ValueError('saved future columns moved')
        if any(row['x'] != material.line_x(row['line']) for row in lines):
            raise ValueError('saved active source columns moved')
        materials.append(material)
    ports = {p['name']: p for p in bus['ports']}
    for station in bus['stations']:
        if station['kind'] == 'consumer':
            p = ports[station['name']]
            if p['item'] != station['item'] or p.get('priority') != station['priority']:
                raise ValueError('saved typed tap contract changed')
            taps.append(ConsumerTap(station['name'], station['item'], station['splitter']['position']['y'],
                                    station['priority'], station['line'], p['demand_per_minute']))
        elif station['kind'] == 'source_merge':
            merges.append(SourceMerge(station['name'], station['item'], station['splitter']['position']['y'],
                                      station['splitter']['splitter_input_priority'], station['line']))
        else:
            raise ValueError('unsupported saved bus station')
    north = {r['north_y'] for r in bus['trunks']}; south = {r['south_y'] for r in bus['trunks']}
    east = {p['position']['x'] for p in bus['ports'] if p.get('priority') in PRIORITY_OUTPUT and p['facing'] == 4}
    if len(north) != 1 or len(south) != 1 or len(east) != 1:
        raise ValueError('incompatible saved bus bounds or fixed outlets')
    return materials, taps, merges, north.pop(), south.pop(), east.pop()


def extend_bus(bus, parameters, obs, *, maximum_span=5):
    """Recompile contracts and one complete branch, freezing old tunnel layers."""
    validate_parameters(parameters)
    materials, taps, merges, north, south, east = reconstruct_bus(bus)
    if any(p['name'] == parameters['name'] for p in bus['ports']):
        return {'ok': False, 'reason': 'tap_name_already_registered'}
    tap = ConsumerTap(parameters['name'], parameters['item'], parameters['splitter_y'], parameters['priority'],
                      parameters.get('line', 0), parameters['demand_per_minute'])
    material = next((m for m in materials if m.item == tap.item), None)
    if material is None or tap.line >= len(material.source_rates):
        return {'ok': False, 'reason': 'tap_requires_existing_typed_active_line'}
    x = material.line_x(tap.line)
    old_tile = next((e for e in bus['entities'] if e['name'] == 'transport-belt' and xy(e) == (x, tap.y) and e['direction'] == 0), None)
    if not old_tile:
        return {'ok': False, 'reason': 'tap_station_requires_owned_straight_surface_trunk'}
    virtual = []
    active_columns = {row['x'] for row in bus['trunks']}
    for pair in bus['underground_pairs']:
        dx, dy = VECTORS[pair['direction']]
        for step in range(1, pair['span']):
            vx, vy, direction = pair['input'][0] + step * dx, pair['input'][1] + step * dy, (pair['direction'] + 4) % 16
            if direction % 8 == 0 and vx in active_columns:
                continue  # The declared active trunk already supplies this crossing layer.
            virtual.append(entity('transport-belt', vx, vy, direction))
    compiled = plan_main_bus(materials, taps + [tap], north_y=north, south_y=south, port_x=east,
                             merges=merges, foreign_entities=virtual, maximum_span=maximum_span)
    if not compiled.get('ok'):
        return {**compiled, 'stage': 'recompile_existing_bus'}
    old_keys = {signature(e) for e in bus['entities']}; target_key = signature(old_tile)
    new_keys = {signature(e) for e in compiled['entities']}
    if (not old_keys - {target_key} <= new_keys or any(p not in compiled['ports'] for p in bus['ports'])
            or compiled['reservations'] != bus['reservations'] or compiled['trunks'] != bus['trunks']
            or not {_pair_key(p) for p in bus['underground_pairs']} <= {_pair_key(p) for p in compiled['underground_pairs']}):
        return {'ok': False, 'reason': 'tap_recompile_would_change_existing_bus_contract'}
    # Existing layers are already physically installed. Only the new branch is
    # compiled against today's foreign factory, including all old bus mouths.
    route = route_trunks((x + 1, tap.y - 1), (east, tap.y - 1), start_direction=4, end_direction=4)
    future = [entity('transport-belt', column, tap.y - 1, 0) for r in bus['reservations']
              for column in r['future_columns'] if x + 1 < column < east]
    branch = compile_crossings(route, obs['entities'] + bus['entities'] + future, maximum_span=maximum_span)
    if not branch.get('ok'):
        return {**branch, 'stage': 'new_tap_branch'}
    station = next(s for s in compiled['stations'] if s['name'] == tap.name)
    entities = [e for e in bus['entities'] if signature(e) != target_key] + [station['splitter']] + branch['entities']
    extra = {k: v for k, v in compiled.items() if k not in ('ok', 'reason', 'entities', 'ports', 'required_items', 'underground_pairs')}
    pairs = list({_pair_key(p): p for p in bus['underground_pairs'] + branch['underground_pairs']}.values())
    result = _plan(entities, compiled['ports'], underground_pairs=pairs, **extra)
    result['branches'][-1]['route_metrics'] = branch['route_metrics']
    return {'ok': True, 'bus': result, 'remove_spec': old_tile,
            'added_entities': [e for e in result['entities'] if signature(e) not in old_keys],
            'branch_path': route['path'],
            'tap_port': next(p for p in result['ports'] if p['name'] == tap.name)}


def replace_bus(plan, old, new):
    """Replace only bus entities/pairs, keeping production ports and machines."""
    keys = {signature(e) for e in plan['entities']}; old_keys = {signature(e) for e in old['entities']}
    new_keys = {signature(e) for e in new['entities']}
    if new_keys <= keys and not old_keys - new_keys & keys:
        return plan
    if not old_keys <= keys:
        raise ValueError('authoritative plan no longer contains the complete old bus')
    entities = [e for e in plan['entities'] if signature(e) not in old_keys] + new['entities']
    old_pairs = {_pair_key(p) for p in old['underground_pairs']}
    pairs = [p for p in plan.get('underground_pairs', []) if _pair_key(p) not in old_pairs] + new['underground_pairs']
    pairs = list({_pair_key(p): p for p in pairs}.values())
    extra = {k: v for k, v in plan.items() if k not in ('ok', 'reason', 'entities', 'ports', 'required_items', 'underground_pairs')}
    return _plan(entities, plan['ports'], underground_pairs=pairs, **extra)


class BusTap:
    def __init__(self, game, builder, catalog, *, bus_factory=None, science=None, green=None):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.components = {'factory': bus_factory, 'red': science, 'green': green}
        root = Path(game.cfg.runtime_dir); self.path = root / 'bus-tap.json'
        self.paths = {'factory': root / 'bus-factory.json', 'red': root / 'automation-science.json',
                      'green': root / 'logistic-science.json', 'board': root / 'pcb-board.json'}
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def _documents(self):
        return {name: json.loads(path.read_text(encoding='utf-8')) for name, path in self.paths.items()}

    def prepare(self, obs, parameters):
        validate_parameters(parameters)
        parameters = {**parameters, 'line': parameters.get('line', 0)}
        try:
            docs = self._documents(); factory = docs['factory']; old = factory.get('bus', {})
            reconstruct_bus(old)
        except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
            return {'ok': False, 'reason': 'tap_saved_bus_missing_or_incompatible', 'detail': str(exc)}
        if any(d.get('world_id') != obs['world_id'] or d.get('catalog_fingerprint') != self.catalog.fingerprint for d in docs.values()):
            return {'ok': False, 'reason': 'tap_authoritative_world_or_catalog_changed'}
        if factory.get('phase') != 'verified' or factory.get('remove') or factory.get('cleanup'):
            return {'ok': False, 'reason': 'tap_requires_commissioned_retired_main_bus'}
        if docs['board'].get('shared_main_bus') != old:
            return {'ok': False, 'reason': 'tap_authoritative_bus_copies_disagree'}
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        retained = []
        for spec in old.get('entities', []):
            live = actual.get((spec['name'], *xy(spec)))
            if (not live or live['direction'] != spec['direction'] or type(live.get('unit_number')) is not int
                    or (spec['name'] == 'splitter' and any(live.get(k) != spec[k] for k in ('splitter_input_priority', 'splitter_output_priority')))):
                return {'ok': False, 'reason': 'tap_existing_native_bus_changed', 'position': spec['position']}
            retained.append({'name': live['name'], 'position': live['position'], 'unit_number': live['unit_number']})
        if not underground_proof(obs, old):
            return {'ok': False, 'reason': 'tap_existing_underground_pairs_unverified'}
        span = self.game.query('return {ok=true,max_span=prototypes.entity["underground-belt"].max_underground_distance}')
        if not span.get('ok'): return span
        try:
            extension = extend_bus(old, parameters, obs, maximum_span=span['max_span'])
            if not extension.get('ok'): return extension
            for plan in (factory['plan'], factory['red_plan'], docs['red']['plan'], docs['green']['plan'], docs['board']['blocks']['logistic_science']):
                replace_bus(plan, old, extension['bus'])
        except (KeyError, TypeError, ValueError) as exc:
            return {'ok': False, 'reason': 'tap_saved_bus_incompatible', 'detail': str(exc)}
        spec = extension['remove_spec']; remove = actual[(spec['name'], *xy(spec))]
        resource_tiles = {(p['x'], p['y']) for p in extension['branch_path']}
        resource_tiles.update(self.builder._occupied_by_plan(extension['added_entities']))
        payload = json.dumps(json.dumps([{'x': x, 'y': y} for x, y in sorted(resource_tiles)], separators=(',', ':')))
        resources = self.game.query('''local tiles=helpers.json_to_table(''' + payload + ''')
for _,p in ipairs(tiles) do
 if s.count_entities_filtered{area={{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}},type="resource"}>0 then
  return {ok=false,reason="tap_new_corridor_crosses_resource_patch",position=p}
 end
end
return {ok=true}''')
        if not resources.get('ok'): return resources
        # Existing assets are grandfathered only by unchanged exact native
        # identities. All new/changed footprints use the current resource policy.
        native = preflight(self.game, self.builder, obs, extension['added_entities'], ignored_units=(remove['unit_number'],))
        if not native.get('ok'): return native
        return {'ok': True, 'schema_version': 1, 'world_id': obs['world_id'], 'catalog_fingerprint': self.catalog.fingerprint,
                'parameters': deepcopy(parameters), 'phase': 'stock', 'old_bus': old, 'new_bus': extension['bus'],
                'old_hash': _hash(old), 'new_hash': _hash(extension['bus']), 'remove': remove,
                'retained_units': [e for e in retained if e['unit_number'] != remove['unit_number']],
                'added_entities': extension['added_entities'], 'required_stock': dict(Counter(e['name'] for e in extension['added_entities'])),
                'resource_checked_path': extension['branch_path'],
                'preflight_scope': 'every new/changed footprint and branch path; exact preserved native units only grandfathered',
                'clearance': _plan([], [], clear_obstacles=native['clear_obstacles'], clearance_world_id=obs['world_id'])}

    def _save(self):
        _atomic_json(self.path, self.state)

    def _retained_problem(self, obs):
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        wanted = {(e['name'], *xy(e)): e for e in self.state['old_bus']['entities']}
        for spec in self.state['retained_units']:
            key = spec['name'], *xy(spec); live = actual.get(key); original = wanted[key]
            if (not live or live['unit_number'] != spec['unit_number'] or live['direction'] != original['direction']
                    or live.get('belt_to_ground_type') != original.get('belt_to_ground_type')
                    or (spec['name'] == 'splitter' and any(live.get(k) != original[k]
                        for k in ('splitter_input_priority', 'splitter_output_priority')))):
                return spec['position']
        return None

    def _publish(self):
        s = self.state; old, new = s['old_bus'], s['new_bus']
        try: docs = self._documents()
        except (OSError, ValueError) as exc:
            return {'status': 'blocked', 'reason': 'tap_publish_documents_unavailable', 'detail': str(exc)}
        if any(d.get('world_id') != s['world_id'] or d.get('catalog_fingerprint') != s['catalog_fingerprint'] for d in docs.values()):
            return {'status': 'blocked', 'reason': 'tap_publish_identity_changed'}
        if _hash(docs['factory']['bus']) not in (s['old_hash'], s['new_hash']) or _hash(docs['board']['shared_main_bus']) not in (s['old_hash'], s['new_hash']):
            return {'status': 'blocked', 'reason': 'tap_publish_bus_revision_changed'}
        try:
            for key in ('plan', 'red_plan'): docs['factory'][key] = replace_bus(docs['factory'][key], old, new)
            for key in ('red', 'green'): docs[key]['plan'] = replace_bus(docs[key]['plan'], old, new)
            docs['board']['blocks']['logistic_science'] = replace_bus(docs['board']['blocks']['logistic_science'], old, new)
        except ValueError as exc:
            return {'status': 'blocked', 'reason': 'tap_publish_plan_changed', 'detail': str(exc)}
        docs['factory']['bus'] = new; docs['board']['shared_main_bus'] = new
        for name, doc in docs.items(): _atomic_json(self.paths[name], doc)
        for name, component in self.components.items():
            if component is not None: component.state = docs[name]
        s['published'] = True; self._save()
        return None

    def step(self, obs, parameters):
        validate_parameters(parameters)
        parameters = {**parameters, 'line': parameters.get('line', 0)}
        if self.state and self.state['parameters'] != parameters:
            if self.state['phase'] != 'verified': return {'status': 'blocked', 'reason': 'another_bus_tap_is_unfinished'}
            self.state = {}
        if not self.state:
            ready = self.prepare(obs, parameters)
            if not ready.get('ok'): return {'status': 'blocked', **ready}
            self.state = ready; self._save()
        s = self.state
        if s['world_id'] != obs['world_id'] or s['catalog_fingerprint'] != self.catalog.fingerprint:
            return {'status': 'blocked', 'reason': 'tap_world_or_catalog_changed'}
        if s['phase'] == 'stock':
            for item, count in s['required_stock'].items():
                if obs.get('inventory', {}).get(item, 0) < count:
                    return self.builder.bootstrap.ensure_item(obs, item, count)
            clear = self.builder.ensure_plan(obs, s['clearance'])
            if clear.get('status') != 'succeeded': return clear
            s['phase'] = 'recover'; self._save()
        if s['phase'] == 'recover':
            wanted = s['remove']; live = next((e for e in obs['entities'] if e['name'] == wanted['name'] and xy(e) == xy(wanted)), None)
            if live:
                if live['unit_number'] != wanted['unit_number'] or live['direction'] != 0:
                    return {'status': 'blocked', 'reason': 'tap_recovery_identity_changed'}
                for item, count in s['required_stock'].items():
                    if obs.get('inventory', {}).get(item, 0) < count:
                        return self.builder.bootstrap.ensure_item(obs, item, count)
                if self._retained_problem(obs) is not None:
                    return {'status': 'blocked', 'reason': 'tap_retained_native_unit_changed_before_cutover'}
                check = preflight(self.game, self.builder, obs, s['added_entities'], ignored_units=(wanted['unit_number'],))
                if not check.get('ok'): return {'status': 'blocked', **check}
                return {'type': 'mine', 'name': wanted['name'], 'position': wanted['position'], 'count': 1,
                        'expected_entity_unit': wanted['unit_number'], 'expected_entity_world_id': obs['world_id'],
                        'reason': 'normally recover the one exact owned trunk belt for a fully stocked priority tap'}
            s['remove'] = None; s['phase'] = 'build'; self._save()
        if s['phase'] == 'build':
            if self._retained_problem(obs) is not None:
                return {'status': 'blocked', 'reason': 'tap_retained_native_unit_changed_during_build'}
            result = self.builder.ensure_plan(obs, _plan(s['new_bus']['entities'], s['new_bus']['ports'],
                                                       underground_pairs=s['new_bus']['underground_pairs']))
            if result.get('status') != 'succeeded': return result
            result = self._publish()
            if result: return result
            s['phase'] = 'commission'; self._save()
        proof = self.proof(obs)
        if not proof: return {'status': 'waiting', 'reason': 'waiting_for_native_priority_and_typed_tap_output'}
        s['phase'] = 'verified'; self._save()
        return {'status': 'succeeded', 'reason': 'one paid native priority tap installed with existing bus contracts preserved', 'evidence': proof}

    def proof(self, obs):
        s = self.state
        if (not s or s['world_id'] != obs['world_id'] or s['catalog_fingerprint'] != self.catalog.fingerprint
                or not underground_proof(obs, s['new_bus'])): return None
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        for spec in s['new_bus']['entities']:
            live = actual.get((spec['name'], *xy(spec)))
            if not live or live['direction'] != spec['direction']: return None
            if spec['name'] == 'splitter' and any(live.get(k) != spec[k] for k in ('splitter_input_priority', 'splitter_output_priority')): return None
        for spec in s['retained_units']:
            live = actual.get((spec['name'], *xy(spec)))
            if not live or live['unit_number'] != spec['unit_number']: return None
        port = next(p for p in s['new_bus']['ports'] if p['name'] == s['parameters']['name'])
        live = actual.get(('transport-belt', *xy(port)))
        content = live.get('belt_inventory', {}) if live else {}
        if set(content) - {port['item']} or content.get(port['item'], 0) <= 0: return None
        station = next(row for row in s['new_bus']['stations'] if row['name'] == port['name'])
        splitter = actual[('splitter', *xy(station['splitter']))]
        output = dict(port, unit_number=live['unit_number'], priority_splitter={
            'name': 'splitter', 'position': splitter['position'], 'unit_number': splitter['unit_number'], 'direction': 0})
        return {'tick': obs['tick'], 'port': output, 'native_contents': content,
                'preserved_entities': len(s['retained_units']), 'required_stock': s['required_stock'],
                'scope': 'one native typed priority tap; no producer increase or guaranteed surplus rate'}
