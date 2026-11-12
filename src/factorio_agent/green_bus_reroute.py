"""Move green bus handoffs off ore while retaining its existing production core."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .bus_factory import identity, trace_corridor
from .bus_tap import reconstruct_bus, replace_bus, signature
from .main_bus import plan_main_bus
from .planning import preflight
from .resource_reroute import partition_routes, _pair_geometry
from .routing import route_trunks
from .smelting import merge_entities
from .transport import VECTORS, compile_crossings, plan_connection, underground_proof, xy


GREEN_NAMES = {'green-iron', 'green-copper'}
DEFAULT_STATIONS = ((-107.5, -83.5), (-111.5, -83.5), (-107.5, -91.5))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _pair_key(pair):
    return tuple(pair['input']), tuple(pair['output']), pair['direction']


def _pairs_within(plan, specs):
    positions = {xy(e) for e in specs if e['name'] == 'underground-belt'}
    return [p for p in plan.get('underground_pairs', []) if tuple(p['input']) in positions and tuple(p['output']) in positions]


def _bound(spec, actual):
    live = actual.get((spec['name'], *xy(spec)))
    if (not live or live.get('direction', 0) != spec.get('direction', 0)
            or live.get('belt_to_ground_type') != spec.get('belt_to_ground_type')
            or type(live.get('unit_number')) is not int
            or (spec['name'] == 'splitter' and any(live.get(k) != spec[k] for k in ('splitter_input_priority', 'splitter_output_priority') if k in spec))):
        raise ValueError('owned green transport is missing or changed: ' + str((spec['name'], xy(spec))))
    return {k: live[k] for k in ('name', 'position', 'direction', 'unit_number')} | (
        {'belt_to_ground_type': live['belt_to_ground_type']} if 'belt_to_ground_type' in live else {})


def old_green_branches(bus):
    ports = {p['name']: p for p in bus['ports']}; result = []
    for station in bus['stations']:
        if station['name'] not in GREEN_NAMES: continue
        sx, sy = xy(station['splitter'])
        source = {'position': {'x': sx + .5, 'y': sy - 1}}
        trace = trace_corridor(bus, source, ports[station['name']])
        if not trace.get('ok'): raise ValueError('cannot trace saved ' + station['name'] + ' branch')
        result.extend([station['splitter']] + trace['entities'])
    if {s['name'] for s in bus['stations'] if s['name'] in GREEN_NAMES} != GREEN_NAMES:
        raise ValueError('both registered green bus stations required')
    return merge_entities(result)


def green_branch_items(bus):
    ports={p['name']:p for p in bus['ports']};items={}
    for station in bus['stations']:
        if station['name'] not in GREEN_NAMES:continue
        x,y=xy(station['splitter'])
        trace=trace_corridor(bus,{'position':{'x':x+.5,'y':y-1}},ports[station['name']])
        if not trace.get('ok'):raise ValueError('green branch item mapping failed')
        items.update({identity(e):station['item'] for e in trace['entities']})
    return items


def move_green_taps(bus, copper_y, iron_y, obs, *, maximum_span=5):
    """Recompile only two named taps, freezing all compatible existing layers."""
    materials, taps, merges, north, south, east = reconstruct_bus(bus)
    moved = old_green_branches(bus); moved_ids = {identity(e) for e in moved}
    kept_pairs = [p for p in bus['underground_pairs'] if p not in _pairs_within(bus, moved)]
    rows = {'green-copper': copper_y, 'green-iron': iron_y}
    taps = [replace(t, y=rows[t.name]) if t.name in rows else t for t in taps]
    active = {r['x'] for r in bus['trunks']}; virtual = []
    for pair in kept_pairs:
        dx, dy = VECTORS[pair['direction']]
        for step in range(1, pair['span']):
            x, y, direction = pair['input'][0] + step * dx, pair['input'][1] + step * dy, (pair['direction'] + 4) % 16
            if direction % 8 == 0 and x in active: continue
            virtual.append(entity('transport-belt', x, y, direction))
    compiled = plan_main_bus(materials, taps, north_y=north, south_y=south, port_x=east,
                             merges=merges, foreign_entities=virtual, maximum_span=maximum_span)
    if not compiled.get('ok'): return {**compiled, 'stage': 'move_green_bus_contracts'}
    targets = []
    for station in compiled['stations']:
        if station['name'] not in GREEN_NAMES: continue
        x, y = xy(station['splitter']); target = next((e for e in bus['entities']
            if e['name'] == 'transport-belt' and xy(e) == (x - .5, y) and e['direction'] == 0), None)
        if target is None: return {'ok': False, 'reason': 'new_green_station_requires_owned_straight_trunk'}
        targets.append(target)
    permitted = moved_ids | {identity(e) for e in targets}
    protected = [e for e in bus['entities'] if identity(e) not in permitted]
    if (not {signature(e) for e in protected} <= {signature(e) for e in compiled['entities']}
            or compiled['trunks'] != bus['trunks'] or compiled['reservations'] != bus['reservations']
            or any(p not in compiled['ports'] for p in bus['ports'] if p['name'] not in GREEN_NAMES)
            or not {_pair_key(p) for p in kept_pairs} <= {_pair_key(p) for p in compiled['underground_pairs']}):
        return {'ok': False, 'reason': 'green_move_would_change_protected_bus_contract'}
    generated = []
    for station in compiled['stations']:
        if station['name'] not in GREEN_NAMES: continue
        sx, sy = xy(station['splitter']); p = next(p for p in compiled['ports'] if p['name'] == station['name'])
        trace = trace_corridor(compiled, {'position': {'x': sx + .5, 'y': sy - 1}}, p)
        if not trace.get('ok'): return {'ok': False, 'reason': 'new_green_branch_trace_failed'}
        generated.extend(trace['entities'])
    generated_ids = {identity(e) for e in generated}
    es = [e for e in compiled['entities'] if identity(e) not in generated_ids]
    pairs = [p for p in compiled['underground_pairs'] if p not in _pairs_within(compiled, generated)]
    for station in compiled['stations']:
        if station['name'] not in GREEN_NAMES: continue
        sx, sy = xy(station['splitter']); route = route_trunks((sx + .5, sy - 1), (east, sy - 1), start_direction=4, end_direction=4)
        future = [entity('transport-belt', column, sy - 1, 0) for r in bus['reservations']
                  for column in r['future_columns'] if sx + .5 < column < east]
        branch = compile_crossings(route, obs['entities'] + es + future, maximum_span=maximum_span)
        if not branch.get('ok'): return {**branch, 'stage': station['name'] + '_branch'}
        es = merge_entities(es, branch['entities']); pairs.extend(branch['underground_pairs'])
    extra = {k: v for k, v in compiled.items() if k not in ('ok', 'reason', 'entities', 'ports', 'required_items', 'underground_pairs')}
    new_bus = _plan(es, compiled['ports'], underground_pairs=list({_pair_key(p): p for p in pairs}.values()), **extra)
    return {'ok': True, 'bus': new_bus, 'old_changed_bus': moved, 'station_targets': targets,
            'protected_bus': protected}


def partition_green_network(old, new, builder, protected=(), *, old_items=None, new_items=None):
    """Defer old/new surface joins and whole pairs before any cutover build."""
    parts = partition_routes(old, new, builder)
    old_by_id = {identity(e): e for e in old['entities']}; new_by_id = {identity(e): e for e in new['entities']}
    retained = {identity(e) for e in parts['retained']}; conflicts = {identity(e) for e in parts['conflicts']}
    wrong_item = {key for key in retained if old_items and new_items and key in old_items and key in new_items
                  and old_items[key] != new_items[key]}
    retained -= wrong_item; conflicts |= wrong_item
    old_tiles = {}; new_tiles = {}
    for e in old['entities']:
        for p in builder._occupied_by_plan([e]): old_tiles[p] = identity(e)
    for e in new['entities']:
        for p in builder._occupied_by_plan([e]): new_tiles[p] = identity(e)
    for key, e in old_by_id.items():
        if key in retained: continue
        if e.get('belt_to_ground_type') != 'input':
            dx, dy = VECTORS[e['direction']]
            if any((p[0] + dx, p[1] + dy) in new_tiles for p in builder._occupied_by_plan([e])): conflicts.add(key)
    for e in new['entities']:
        if e.get('belt_to_ground_type') == 'input': continue
        dx, dy = VECTORS[e['direction']]
        for p in builder._occupied_by_plan([e]):
            recipient = old_tiles.get((p[0] + dx, p[1] + dy))
            if recipient is not None and recipient not in retained: conflicts.add(recipient)
    for pair in old.get('underground_pairs', []):
        members = {identity(e) for e in old['entities'] if e['name'] == 'underground-belt' and xy(e) in (tuple(pair['input']), tuple(pair['output']))}
        if members & conflicts: conflicts.update(members); retained -= members
    if conflicts & {identity(e) for e in protected}:
        return {'ok': False, 'reason': 'green_route_requires_recovery_of_protected_bus_asset'}
    deferred = {identity(e) for e in parts['deferred']} | wrong_item
    conflict_boxes = [builder._occupied_by_plan([old_by_id[k]]) for k in conflicts]
    old_feeds=set();old_nonretained_tiles=set()
    for key,e in old_by_id.items():
        if key in retained:continue
        box=builder._occupied_by_plan([e]);old_nonretained_tiles.update(box)
        if e.get('belt_to_ground_type')!='input':
            dx,dy=VECTORS[e['direction']];old_feeds.update((x+dx,y+dy) for x,y in box)
    for key, e in new_by_id.items():
        if key in retained: continue
        box=builder._occupied_by_plan([e])
        if box & old_feeds or any(box & conflict for conflict in conflict_boxes):deferred.add(key)
        if e.get('belt_to_ground_type')!='input':
            dx,dy=VECTORS[e['direction']]
            if any((x+dx,y+dy) in old_nonretained_tiles for x,y in box):deferred.add(key)
        if e['name'] == 'underground-belt':
            for old_key in conflicts:
                other = old_by_id[old_key]
                if (other['name'] == 'underground-belt' and other['direction'] == e['direction']
                        and ((xy(other)[0] == xy(e)[0]) if e['direction'] % 8 == 0 else (xy(other)[1] == xy(e)[1]))
                        and abs(xy(other)[0] - xy(e)[0]) + abs(xy(other)[1] - xy(e)[1]) <= 5): deferred.add(key)
    added = [e for k, e in new_by_id.items() if k not in retained]
    return {'ok': True, 'retained': [old_by_id[k] for k in retained],
            'conflicts': [e for e in old['entities'] if identity(e) in conflicts],
            'obsolete': [e for e in old['entities'] if identity(e) not in retained | conflicts],
            'prebuild': [e for e in added if identity(e) not in deferred],
            'deferred': [e for e in added if identity(e) in deferred], 'added': added}


def replace_network(plan, old, new):
    old_ids = {identity(e) for e in old['entities']}; new_ids = {identity(e) for e in new['entities']}
    current = {identity(e) for e in plan['entities']}
    if new_ids <= current and not (old_ids - new_ids) & current: return plan
    if not old_ids <= current: raise ValueError('saved green network changed during publication')
    es = merge_entities([e for e in plan['entities'] if identity(e) not in old_ids], new['entities'])
    pairs = [p for p in plan.get('underground_pairs', []) if _pair_key(p) not in {_pair_key(p) for p in old.get('underground_pairs', [])}]
    pairs = list({_pair_key(p): p for p in pairs + new.get('underground_pairs', [])}.values())
    extra = {k: v for k, v in plan.items() if k not in ('ok', 'reason', 'entities', 'ports', 'required_items', 'underground_pairs')}
    before = {(e['name'], *xy(e), e.get('recipe')) for e in plan['entities'] if e['name'] not in ('transport-belt', 'underground-belt', 'splitter')}
    after = {(e['name'], *xy(e), e.get('recipe')) for e in es if e['name'] not in ('transport-belt', 'underground-belt', 'splitter')}
    if before != after: raise ValueError('green reroute cannot change production machines or poles')
    return _plan(es, plan['ports'], underground_pairs=pairs, **extra)


class GreenBusReroute:
    def __init__(self, game, builder, catalog, bus_factory, science, green, smelting):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.bus_factory, self.science, self.green, self.smelting = bus_factory, science, green, smelting
        root = Path(game.cfg.runtime_dir); self.path = root / 'green-bus-reroute.json'
        self.paths = {'factory': root / 'bus-factory.json', 'red': root / 'automation-science.json',
                      'green': root / 'logistic-science.json', 'board': root / 'pcb-board.json'}
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def _documents(self):
        return {name: json.loads(path.read_text(encoding='utf-8')) for name, path in self.paths.items()}

    def _save(self):
        _atomic_json(self.path, self.state)

    def prepare(self, obs, parameters=None, *, candidates=DEFAULT_STATIONS):
        if parameters not in (None, {}): return {'ok': False, 'reason': 'green_bus_reroute_takes_no_parameters'}
        try:
            docs = self._documents(); factory = docs['factory']; old_bus = factory['bus']
            if any(d.get('world_id') != obs['world_id'] or d.get('catalog_fingerprint') != self.catalog.fingerprint for d in docs.values()):
                return {'ok': False, 'reason': 'green_reroute_world_or_catalog_changed'}
            if factory.get('phase') != 'verified' or factory.get('cleanup') or factory.get('remove') or docs['board']['shared_main_bus'] != old_bus:
                return {'ok': False, 'reason': 'green_reroute_requires_consistent_commissioned_bus'}
            core_ports = deepcopy(factory['core']['ports'])
            if docs['green']['core']['ports'] != core_ports: raise ValueError('green core fixed ports disagree')
            inputs = {p['item']: p for p in core_ports if p['direction'] == 'input'}
            old_ports = {p['name']: p for p in old_bus['ports']}
            old_adapters = []; adapter_specs = []
            for name in sorted(GREEN_NAMES):
                source = old_ports[name]; target = inputs[source['item']]
                trace = trace_corridor(factory['plan'], source, target)
                if not trace.get('ok'): return dict(trace, stage='trace_' + name + '_adapter')
                specs = trace['entities'][:-1]; adapter_specs.extend(specs)
                old_adapters.append({'name': name, 'entities': specs, 'underground_pairs': _pairs_within(factory['plan'], specs)})
            old_network = _plan(merge_entities(old_bus['entities'], adapter_specs), [],
                underground_pairs=list({_pair_key(p): p for p in old_bus['underground_pairs'] +
                                       [p for r in old_adapters for p in r['underground_pairs']]}.values()))
            actual = {(e['name'], *xy(e)): e for e in obs['entities']}
            bindings = {identity(e): _bound(e, actual) for e in old_network['entities']}
            if not underground_proof(obs, old_network): return {'ok': False, 'reason': 'green_old_native_pairs_unverified'}
            affected = merge_entities(old_green_branches(old_bus), adapter_specs)
            failures = []
            span = self.game.query('return {ok=true,max_span=prototypes.entity["underground-belt"].max_underground_distance}')
            if not span.get('ok'): return span
            for copper_y, iron_y in candidates:
                ignored_specs = affected[:]
                for name, row_y in (('green-copper', copper_y), ('green-iron', iron_y)):
                    station = next(s for s in old_bus['stations'] if s['name'] == name)
                    x = station['splitter']['position']['x'] - .5
                    target = next((e for e in old_bus['entities'] if e['name'] == 'transport-belt' and xy(e) == (x, row_y) and e['direction'] == 0), None)
                    if target: ignored_specs.append(target)
                ignored = {bindings[identity(e)]['unit_number'] for e in ignored_specs}
                virtual = dict(obs, entities=[e for e in obs['entities'] if e.get('unit_number') not in ignored])
                try: moved = move_green_taps(old_bus, copper_y, iron_y, virtual, maximum_span=span['max_span'])
                except (ValueError, KeyError) as exc:
                    failures.append({'stage': 'bus_contract', 'reason': str(exc)}); continue
                if not moved.get('ok'): failures.append(moved); continue
                new_bus = moved['bus']; ports = {p['name']: p for p in new_bus['ports']}
                fixed = [e for e in factory['plan']['entities'] if identity(e) not in {identity(e) for e in old_network['entities']}]
                es = merge_entities(fixed, new_bus['entities']); routes = []
                adapter = SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan,
                    can_place=lambda specs: preflight(self.game, self.builder, obs, specs, ignored_units=ignored))
                for name in ('green-copper', 'green-iron'):
                    route = plan_connection(self.game, adapter, virtual, ports[name], inputs[ports[name]['item']], es, ignored_units=ignored)
                    if not route.get('ok'): failures.append(dict(route, connection=name)); break
                    routes.append(dict(route, name=name)); es = merge_entities(es, route['entities'])
                if len(routes) != 2: continue
                input_ids = {identity(e) for e in factory['core']['entities'] if any(xy(e) == xy(p) for p in inputs.values())}
                new_adapters = [e for r in routes for e in r['entities'] if identity(e) not in input_ids]
                new_network = _plan(merge_entities(new_bus['entities'], new_adapters), [],
                    underground_pairs=list({_pair_key(p): p for p in new_bus['underground_pairs'] +
                                           [p for r in routes for p in r['underground_pairs']]}.values()))
                if not _pair_geometry(new_network): failures.append({'reason': 'green_new_pairs_can_steal_neighbours'}); continue
                old_items = green_branch_items(old_bus)
                old_items.update({identity(e): old_ports[r['name']]['item'] for r in old_adapters for e in r['entities']})
                new_items = green_branch_items(new_bus)
                new_items.update({identity(e): r['ports'][0]['item'] for r in routes for e in r['entities']})
                parts = partition_green_network(old_network, new_network, self.builder, moved['protected_bus'],
                                                old_items=old_items, new_items=new_items)
                if not parts.get('ok'): failures.append(parts); continue
                removed = parts['conflicts'] + parts['obsolete']; removed_units = [bindings[identity(e)]['unit_number'] for e in removed]
                full = replace_network(factory['plan'], old_network, new_network)
                check = preflight(self.game, self.builder, obs, full['entities'], ignored_units=removed_units)
                if not check.get('ok'): failures.append(dict(check, stage='whole_green_alternate')); continue
                cutover_units=[bindings[identity(e)]['unit_number'] for e in parts['conflicts']]
                cutover_check=preflight(self.game,self.builder,obs,new_network['entities'],ignored_units=cutover_units)
                if not cutover_check.get('ok'):failures.append(dict(cutover_check,stage='green_cutover_coexistence'));continue
                prebuild = preflight(self.game, self.builder, obs, parts['prebuild'])
                if not prebuild.get('ok'): failures.append(dict(prebuild, stage='green_prebuild')); continue
                for plan in (factory['red_plan'], docs['red']['plan']): replace_bus(plan, old_bus, new_bus)
                replace_network(docs['green']['plan'], old_network, new_network)
                replace_network(docs['board']['blocks']['logistic_science'], old_network, new_network)
                obstacles = {(e['name'], *xy(e)): e for r in routes + [check,cutover_check,prebuild] for e in r['clear_obstacles']}
                conflicts = [bindings[identity(e)] for e in parts['conflicts']]
                conflicts.sort(key=lambda e: (e['name'] != 'splitter', xy(e)))
                metrics = {r['name']: r['route_metrics'] for r in routes}
                return {'ok': True, 'world_id': obs['world_id'], 'catalog_fingerprint': self.catalog.fingerprint,
                        'phase': 'stock', 'old_bus': old_bus, 'new_bus': new_bus, 'old_network': old_network,
                        'new_network': new_network, 'old_hash': _hash(old_bus), 'new_hash': _hash(new_bus),
                        'core_ports': core_ports, 'core_entities': factory['core']['entities'], 'inputs': list(inputs.values()),
                        'pending_cutover': conflicts, 'pending_retirement': [bindings[identity(e)] for e in parts['obsolete']],
                        'retained': [bindings[identity(e)] for e in parts['retained']],
                        'prebuild': _plan(parts['prebuild'], [], underground_pairs=_pairs_within(new_network, parts['prebuild'])),
                        'required_stock': dict(Counter(e['name'] for e in parts['added'])),
                        'deferred_stock': dict(Counter(e['name'] for e in parts['deferred'])),
                        'clearance': _plan([], [], clear_obstacles=list(obstacles.values()), clearance_world_id=obs['world_id']),
                        'route_metrics': metrics, 'station_rows': {'green-copper': copper_y, 'green-iron': iron_y},
                        'prepared_tick': obs['tick']}
            return {'ok': False, 'reason': 'no_resource_free_green_bus_move_with_bounded_adapters', 'failures': failures}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {'ok': False, 'reason': 'green_reroute_saved_network_incompatible', 'detail': str(exc)}

    def _stock(self, obs, counts):
        for item, count in counts.items():
            if obs.get('inventory', {}).get(item, 0) < count: return self.builder.bootstrap.ensure_item(obs, item, count)
        return None

    def _recover(self, obs, key):
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}; remaining = []; actions = []
        for wanted in self.state[key]:
            live = actual.get((wanted['name'], *xy(wanted)))
            if not live: continue
            if live['unit_number'] != wanted['unit_number'] or identity(live) != identity(wanted):
                return {'status': 'blocked', 'reason': 'green_reroute_recovery_identity_changed', 'position': wanted['position']}
            remaining.append(wanted)
            if len(actions) < 8:
                actions.append(dict(type='mine', name=wanted['name'], position=wanted['position'], count=1,
                    expected_entity_unit=wanted['unit_number'], expected_entity_world_id=obs['world_id']))
        if remaining != self.state[key]: self.state[key] = remaining; self._save()
        if actions: return {'type': 'mine_many', 'actions': actions, 'reason': 'normally recover only exact obsolete green plate transport'}
        return None

    def _retained_healthy(self, obs):
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        return all(actual.get((e['name'], *xy(e)), {}).get('unit_number') == e['unit_number']
                   and identity(actual[(e['name'], *xy(e))]) == identity(e) for e in self.state['retained'])

    def _publish(self):
        s = self.state; docs = self._documents()
        if any(d.get('world_id') != s['world_id'] or d.get('catalog_fingerprint') != s['catalog_fingerprint'] for d in docs.values()):
            return {'status': 'blocked', 'reason': 'green_reroute_publish_identity_changed'}
        if any(_hash(b) not in (s['old_hash'], s['new_hash']) for b in (docs['factory']['bus'], docs['board']['shared_main_bus'])):
            return {'status': 'blocked', 'reason': 'green_reroute_publish_bus_revision_changed'}
        if docs['factory']['core']['ports'] != s['core_ports'] or docs['green']['core']['ports'] != s['core_ports']:
            return {'status': 'blocked', 'reason': 'green_fixed_core_ports_changed'}
        try:
            docs['factory']['plan'] = replace_network(docs['factory']['plan'], s['old_network'], s['new_network'])
            docs['factory']['red_plan'] = replace_bus(docs['factory']['red_plan'], s['old_bus'], s['new_bus'])
            docs['red']['plan'] = replace_bus(docs['red']['plan'], s['old_bus'], s['new_bus'])
            docs['green']['plan'] = replace_network(docs['green']['plan'], s['old_network'], s['new_network'])
            docs['board']['blocks']['logistic_science'] = replace_network(docs['board']['blocks']['logistic_science'], s['old_network'], s['new_network'])
        except ValueError as exc: return {'status': 'blocked', 'reason': 'green_reroute_publish_network_changed', 'detail': str(exc)}
        for key in ('factory', 'green'):
            updated=[]
            for row in docs[key]['route_metrics']:
                name=row.get('connection')
                if name in ('green-iron-plate','green-copper-plate'):name=name[:-6]
                updated.append(dict(row,**s['route_metrics'][name]) if name in s['route_metrics'] else row)
            docs[key]['route_metrics']=updated
        docs['factory']['bus'] = s['new_bus']; docs['board']['shared_main_bus'] = s['new_bus']
        for name, doc in docs.items(): _atomic_json(self.paths[name], doc)
        self.bus_factory.state = docs['factory']; self.science.state = docs['red']; self.green.state = docs['green']
        s['published'] = True; self._save()
        return None

    def step(self, obs, parameters=None):
        if not self.state:
            prepared = self.prepare(obs, parameters)
            if not prepared.get('ok'): return {'status': 'blocked', **prepared}
            self.state = prepared; self._save()
        s = self.state
        if s['world_id'] != obs['world_id'] or s['catalog_fingerprint'] != self.catalog.fingerprint:
            return {'status': 'blocked', 'reason': 'green_reroute_world_or_catalog_changed'}
        if not self._retained_healthy(obs): return {'status': 'blocked', 'reason': 'green_reroute_retained_native_asset_changed'}
        if s['phase'] == 'stock':
            action = self._stock(obs, s['required_stock'])
            if action: return action
            result = self.builder.ensure_plan(obs, s['clearance'])
            if result.get('status') != 'succeeded': return result
            s['phase'] = 'prebuild'; self._save()
        if s['phase'] == 'prebuild':
            result = self.builder.ensure_plan(obs, s['prebuild'])
            if result.get('status') != 'succeeded': return result
            s['phase'] = 'cutover'; self._save()
        if s['phase'] == 'cutover':
            action = self._stock(obs, s['deferred_stock'])
            if action: return action
            check = preflight(self.game, self.builder, obs, s['new_network']['entities'],
                              ignored_units=[e['unit_number'] for e in s['pending_cutover']])
            if not check.get('ok'): return {'status': 'blocked', **check}
            action = self._recover(obs, 'pending_cutover')
            if action: return action
            s['phase'] = 'build'; self._save()
        if s['phase'] == 'build':
            result = self.builder.ensure_plan(obs, s['new_network'])
            if result.get('status') != 'succeeded': return result
            s['phase'] = 'publish'; self._save()
        if s['phase'] == 'publish':
            result = self._publish()
            if result: return result
            s['phase'] = 'commission'; self._save()
        if s['phase'] in ('commission', 'retire', 'verified'):
            evidence = self.proof(obs)
            if not evidence: return {'status': 'waiting', 'reason': 'waiting_for_new_raw_feeds_all_compact_machines_labs_and_fresh_source_proof'}
            s['commissioned'] = evidence; s['phase'] = 'retire'; self._save()
            action = self._recover(obs, 'pending_retirement')
            if action: return action
            s['phase'] = 'verified'; self._save()
            return {'status': 'succeeded', 'reason': 'green plate taps and adapters moved off ore; obsolete owned routes recovered', 'evidence': evidence}
        return {'status': 'blocked', 'reason': 'unknown_green_reroute_phase'}

    def proof(self, obs):
        s = self.state
        if (not s or not s.get('published') or s['world_id'] != obs['world_id']
                or not self._retained_healthy(obs) or not underground_proof(obs, s['new_network'])): return None
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}; feeds = []
        for p in s['inputs']:
            live = actual.get(('transport-belt', *xy(p))); content = live.get('belt_inventory', {}) if live else {}
            if not live or live['direction'] != p['facing'] or set(content) - {p['item']} or content.get(p['item'], 0) <= 0: return None
            feeds.append({'item': p['item'], 'position': p['position'], 'unit_number': live['unit_number'], 'native_contents': content})
        for spec in s['new_network']['entities']:
            live = actual.get((spec['name'], *xy(spec)))
            if not live or live['direction'] != spec['direction']: return None
            if spec['name'] == 'splitter' and any(live.get(k) != spec[k] for k in ('splitter_input_priority', 'splitter_output_priority')): return None
        factory = self.bus_factory.proof(obs)
        if not factory or not factory.get('source_smelting') or len(factory.get('compact_machine_units', [])) != 12: return None
        return {'tick': obs['tick'], 'station_rows': s['station_rows'], 'fixed_core_ports': s['core_ports'],
                'native_raw_feeds': feeds, 'factory': factory, 'route_metrics': s['route_metrics'],
                'scope': 'resource-free green transport migration and fresh production/lab commissioning; no sustained-rate certificate'}
