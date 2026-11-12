"""Fixed-port blue science and a separately owned south-side eight-lab feed."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
import math
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.core.deterministic_state import _atomic_json
from factorio_agent.core.world_catalog import _yield
from .advanced_circuits import AdvancedCircuitProduction, _candidate_failure, _hash, _key, _payload
from .board import entity
from .chemical_production import ChemicalProduction
from .compact_green import _machine_at, _spread, _transport
from .engine_production import EngineProduction, _clear_planned_power_group
from .industrial import PlanningBudget
from .manufacturing import connect_power_groups
from .planning import overlap, preflight, survey_slots
from .science import AutomaticScience
from .smelting import merge_entities, port
from .transport import VECTORS, plan_connection, surface_pipe_proof, underground_proof, unbuilt_external_keepouts, xy


SCOPE_ID = 'chemical-science-production'
PRODUCT = 'chemical-science-pack'


def chemical_science_rates(catalog, count=2):
    if type(count) is not int or not 2 <= count <= 4: raise ValueError('two to four chemical-science machines supported')
    recipe = catalog.recipes[PRODUCT]; machine = catalog.entities['assembling-machine-1']; speed = machine['crafting_speed']
    expected = {'engine-unit', 'advanced-circuit', 'sulfur'}
    if (len(recipe['products']) != 1 or recipe['products'][0]['name'] != PRODUCT or recipe['products'][0].get('type', 'item') != 'item'
            or len(recipe['ingredients']) != 3 or {i['name'] for i in recipe['ingredients']} != expected
            or recipe['category'] not in machine.get('crafting_categories', [])
            or any(i.get('type', 'item') != 'item' or type(i['amount']) not in (int, float) or not math.isfinite(i['amount']) or i['amount'] <= 0 for i in recipe['ingredients'])
            or type(recipe['energy']) not in (int, float) or not math.isfinite(recipe['energy']) or recipe['energy'] <= 0
            or type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0):
        raise ValueError('installed ordinary item-only chemical science recipe and machine required')
    output = _yield(recipe['products'][0]); cycles = 60*speed/recipe['energy']
    needs = {i['name']: i['amount']*cycles for i in recipe['ingredients']}
    if not math.isfinite(output) or output <= 0 or max(needs['engine-unit']+needs['advanced-circuit'], needs['sulfur'], cycles*output) > 30:
        raise ValueError('positive yield and conservative ordinary inserter budgets required')
    return {'science_per_minute': cycles*output*count, 'engine_per_minute': needs['engine-unit']*count,
            'advanced_circuits_per_minute': needs['advanced-circuit']*count, 'sulfur_per_minute': needs['sulfur']*count,
            'assembler_count': count, 'conservative_inserter_design_budget_per_minute': 30,
            'scope': 'catalog nameplate/BOM; shared producer supply and actual delivery determine measured throughput'}


def chemical_science_core(catalog, x, y, count=2):
    rated = chemical_science_rates(catalog, count)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v % 1 != .5 for v in (x, y)):
        raise ValueError('half-tile chemical-science anchor required')
    es = []
    def add(name, dx, dy, d=0, **extra):
        e = dict(entity(name, x+dx, y+dy, d), **extra); es.append(e); return e
    def belt(dx, dy, d=4): return add('transport-belt', dx, dy, d)
    for xx in (-3, -2): belt(xx, 1)
    belt(-1, 1, 8); belt(-1, 2, 8)
    for xx in (-3, -2, -1): belt(xx, 5)
    belt(0, 5, 0); belt(0, 4, 0)
    for xx in range(-1, 22): belt(xx, 3)
    belt(-2, 3)  # Empty inline tail makes the first engine feed a native T.
    for xx in range(2, 26): belt(xx, 2, 12)
    machines = []
    for i in range(count):
        cx = 3+6*i; machines.append(add('assembling-machine-1', cx, 6, recipe=PRODUCT))
        add('inserter', cx, 4); add('long-handed-inserter', cx-1, 4); add('inserter', cx, 8)
        add('small-electric-pole', cx-2, 5); add('small-electric-pole', cx+2, 8)
    for xx in range(-3, 22): belt(xx, 9, 12)
    ports = [port('engine-unit', x-3, y+1, 4), port('advanced-circuit', x-3, y+5, 4),
             port('sulfur', x+25, y+2, 12), port(PRODUCT, x-3, y+9, 12, direction='output')]
    return _plan(es, ports, machines=machines, anchor={'x': x, 'y': y}, rated=rated,
        reserved_area=[[x-4.5, y-.5], [x+26.5, y+12.5]], underground_pairs=[],
        output_access=[entity('transport-belt', x-i, y+9, 12) for i in (4, 5)],
        extension_slots=[entity('assembling-machine-1', x+3+6*i, y+6, recipe=PRODUCT) for i in range(count, 4)],
        mixed_feed_lanes={'left': 'engine-unit', 'right': 'advanced-circuit'}, layout_policy='blue-science-three-item-headers-v1')


def audit_chemical_science_core(plan, catalog):
    es = plan['entities']; errors = []; geometry = FactoryBuilder.__new__(FactoryBuilder)
    boxes = [geometry._occupied_by_plan([e]) for e in es]; occupied = set().union(*boxes); a, b = plan['reserved_area']
    if sum(map(len, boxes)) != len(occupied): errors.append('prospective_footprint_overlap')
    if any(not(a[0] <= px-.5 and px+.5 <= b[0] and a[1] <= py-.5 and py+.5 <= b[1]) for px, py in occupied): errors.append('entity_outside_reservation')
    if any(geometry._occupied_by_plan([e]) & occupied for e in plan['extension_slots']): errors.append('future_blue_slot_occupied')
    belts, graph = _transport(es, []); machines = [e for e in es if e['name'] == 'assembling-machine-1']
    arms = [e for e in es if e['name'] in ('inserter', 'long-handed-inserter')]; poles = [xy(e) for e in es if e['name'] == 'small-electric-pole']
    for e in machines+arms:
        if not any(abs(xy(e)[0]-px) <= 2.5 and abs(xy(e)[1]-py) <= 2.5 for px, py in poles): errors.append('unpowered_consumer:'+str(xy(e)))
    connected = {poles[0]} if poles else set()
    while True:
        more = {p for p in poles if any(math.dist(p, q) <= 7.5 for q in connected)}
        if more <= connected: break
        connected |= more
    if len(connected) != len(poles): errors.append('disconnected_internal_poles')
    lanes = defaultdict(set)
    for p in plan['ports'][:-1]:
        for lane in ('left', 'right'): lanes[xy(p), lane].add(p['item'])
    _spread(lanes, graph); supplied = defaultdict(set); targets = []
    for arm in arms:
        px, py = xy(arm); dx, dy = VECTORS[arm['direction']]; reach = 2 if arm['name'] == 'long-handed-inserter' else 1
        pickup, drop = (px+dx*reach, py+dy*reach), (px-dx*reach, py-dy*reach)
        source, dest = _machine_at(machines, pickup), _machine_at(machines, drop)
        items = {PRODUCT} if source else lanes[pickup, 'left'] | lanes[pickup, 'right']
        if dest: supplied[xy(dest)] |= items
        elif drop in belts:
            vx, vy = VECTORS[belts[drop]['direction']]; side = (px-drop[0])*vy-(py-drop[1])*vx
            lanes[drop, 'right' if side > 0 else 'left'] |= items
        if not items: errors.append('inserter_target_unreachable:'+str(xy(arm)))
        targets.append(dict(arm, pickup_position={'x': pickup[0], 'y': pickup[1]}, drop_position={'x': drop[0], 'y': drop[1]}, allowed_items=sorted(items)))
    _spread(lanes, graph)
    expected = {i['name'] for i in catalog.recipes[PRODUCT]['ingredients']}
    for m in machines:
        if supplied[xy(m)] != expected: errors.append('blue_machine_ingredients_unreachable:'+str(xy(m)))
        point = xy(m)[0], xy(m)[1]-3
        for lane, item in plan['mixed_feed_lanes'].items():
            if lanes[point, lane] != {item}: errors.append('blue_mixed_lane_mismatch:'+lane)
    if lanes[xy(plan['ports'][-1]), 'left'] | lanes[xy(plan['ports'][-1]), 'right'] != {PRODUCT}: errors.append('blue_output_unreachable_or_contaminated')
    return {'ok': not errors, 'errors': errors, 'machine_count': len(machines), 'arm_targets': targets,
            'belt_contracts': [dict(e, allowed_items=sorted(lanes[p, 'left'] | lanes[p, 'right'])) for p, e in belts.items()],
            'rated': chemical_science_rates(catalog, len(machines))}


def blue_lab_extension(labs):
    """One exact west-edge apron and an east header below the existing eight labs."""
    if labs.get('template') != 'belt_fed_lab_row' or labs.get('count') != 8: raise ValueError('existing eight-lab row required')
    machines = sorted((e for e in labs['entities'] if e['name'] == 'lab'), key=lambda e: xy(e)[0])
    if len(machines) != 8: raise ValueError('exactly eight existing labs required')
    x, y = xy(machines[0]); a, b = labs['reserved_area']
    if any(xy(e) != (x+4*i, y) for i, e in enumerate(machines)) or a[0] != x-1.5 or b[0] != x+30.5:
        raise ValueError('unsupported lab row geometry')
    # The current raw copper adapter occupies y+3. Native long arms reach OVER
    # that independent belt to the new y+4 header, without touching its units.
    header = [entity('transport-belt', x+i, y+4, 4) for i in range(-1, 29)]
    arms = [entity('long-handed-inserter', xy(m)[0], y+2, 8) for m in machines]
    outside = port(PRODUCT, x-2, y+4, 4)
    inside = port(PRODUCT, x-1, y+4, 4)
    occupied = FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan(labs['entities'])
    if FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan(header+arms) & occupied: raise ValueError('new blue lab hardware overlaps original lab assets')
    if not a[1] <= y+4-.5 < y+4+.5 <= b[1]: raise ValueError('south lab header outside existing reservation')
    poles = [xy(e) for e in labs['entities'] if e['name'] == 'small-electric-pole']
    if any(not any(abs(xy(e)[0]-px) <= 2.5 and abs(xy(e)[1]-py) <= 2.5 for px, py in poles) for e in arms):
        raise ValueError('existing poles do not cover all eight new south arms')
    for m in machines:
        p = (xy(m)[0], y-2)
        if not any(e['name'] == 'inserter' and xy(e) == p and e['direction'] == 0 for e in labs['entities']): raise ValueError('original north lab inserter changed')
    return _plan(header+arms, [outside], registered_port=inside, outside_port=outside,
        owner='laboratories', original_entities=deepcopy(labs['entities']), original_lab_units_required=8,
        original_geometry_hash=_hash(labs['entities']), labs=machines,
        apron=[{'x': x-2, 'y': y+4}, {'x': x-1, 'y': y+4}],
        layout_policy='owned-eight-lab-south-blue-v1')


class ChemicalScience:
    def __init__(self, game, builder, catalog, perimeter, *, engine=None, advanced=None, chemical=None,
                 science=None, green=None, bus_factory=None):
        self.game, self.builder, self.catalog, self.perimeter = game, builder, catalog, perimeter
        self.science = science if science is not None else AutomaticScience(game, builder, catalog)
        self.chemical = chemical if chemical is not None else ChemicalProduction(game, builder, catalog, perimeter)
        self.engine = engine if engine is not None else EngineProduction(game, builder, catalog, perimeter,
            science=self.science, green=green, bus_factory=bus_factory)
        self.advanced = advanced if advanced is not None else AdvancedCircuitProduction(game, builder, catalog, perimeter,
            science=self.science, green=green, bus_factory=bus_factory, chemical=self.chemical)
        self.path = Path(game.cfg.runtime_dir)/'chemical-science.json'
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        self.last_proof_failure = None

    def _identity(self, obs, value):
        return (obs.get('ok', True) and isinstance(obs.get('world_id'), str) and bool(obs['world_id'])
                and value.get('world_id') == obs['world_id'] and value.get('catalog_fingerprint') == self.catalog.fingerprint)

    def _save(self): _atomic_json(self.path, self.state)

    def _source_plans(self):
        return {'engine-unit': self.engine.state.get('plan'), 'advanced-circuit': self.advanced.state.get('plan'),
                'sulfur': self.chemical.state.get('products', {}).get('sulfur', {}).get('plan')}

    def _sources(self, obs):
        sources = {}; producers = [('engine-unit', self.engine, None), ('advanced-circuit', self.advanced, None),
                                   ('sulfur', self.chemical, {'product': 'sulfur'})]
        for item, component, parameters in producers:
            entry = component.state.get('products', {}).get('sulfur', {}) if parameters else component.state
            if not self._identity(obs, component.state) or entry.get('phase') != 'verified':
                return {'ok': False, 'reason': 'commissioned_automatic_blue_ingredient_producer_required', 'item': item}
            proof = component.proof(obs, parameters) if parameters else component.proof(obs)
            if not proof: return {'ok': False, 'reason': 'fresh_native_blue_ingredient_proof_required', 'item': item,
                                  'evidence': getattr(component, 'last_proof_failure', None)}
            p = proof.get('source' if parameters else 'output', {})
            e = next((e for e in obs['entities'] if e['name'] == 'transport-belt' and e.get('position') == p.get('position')), None)
            if (p.get('kind') != 'item' or p.get('item') != item or p.get('direction') != 'output'
                    or p.get('world_id') != obs['world_id'] or type(p.get('unit_number')) is not int
                    or not e or e.get('unit_number') != p['unit_number'] or e.get('direction') != p.get('facing')):
                return {'ok': False, 'reason': 'exact_fixed_typed_blue_ingredient_outlet_required', 'item': item}
            sources[item] = deepcopy(p)
        return {'ok': True, 'sources': sources, 'plan_revisions': {item: _hash(p) for item, p in self._source_plans().items()}}

    def _lab_context(self, obs):
        if not self._identity(obs, self.science.state): return {'ok': False, 'reason': 'owned_laboratory_science_checkpoint_required'}
        labs = self.science.state.get('labs', {})
        if any(p.get('item') == PRODUCT for p in labs.get('ports', [])):
            return {'ok': False, 'reason': 'blue_lab_port_exists_without_factory_checkpoint'}
        try:
            board = json.loads((self.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
            if not self._identity(obs, board) or board.get('blocks', {}).get('laboratories') != labs:
                return {'ok': False, 'reason': 'blue_laboratory_authorities_disagree'}
            extension = blue_lab_extension(labs)
        except (OSError, KeyError, TypeError, ValueError) as exc: return {'ok': False, 'reason': 'blue_laboratory_interface_incompatible', 'detail': str(exc)}
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}; retained = []
        for spec in labs['entities']:
            e = actual.get((spec['name'], *xy(spec)))
            if not e or type(e.get('unit_number')) is not int or (spec['name'] != 'small-electric-pole' and e.get('direction') != spec.get('direction', 0)):
                return {'ok': False, 'reason': 'original_owned_laboratory_asset_changed', 'entity': spec}
            retained.append(dict(spec, unit_number=e['unit_number']))
        # Native can_place accepts an existing identical owned belt. That alone
        # cannot distinguish blue infrastructure from an unrelated copper feed.
        planned = self.builder._occupied_by_plan(extension['entities']+[entity('transport-belt', *xy(extension['outside_port']), 4)])
        for e in obs['entities']:
            if e['name'] not in ('tree', 'item-on-ground', 'entity-ghost', 'tile-ghost') and self.builder._occupied_by_plan([e]) & planned:
                return {'ok': False, 'reason': 'new_blue_lab_interface_occupied_by_unowned_existing_asset', 'entity': e['name'], 'position': e['position']}
        try: future_hard = unbuilt_external_keepouts(self.builder, obs, board['blocks'])
        except (KeyError, TypeError, ValueError) as exc: return {'ok': False, 'reason': 'blue_unbuilt_external_plans_incompatible', 'detail': str(exc)}
        if planned & future_hard:
            return {'ok': False, 'reason': 'blue_lab_interface_conflicts_with_unbuilt_external_hardware', 'position': sorted(planned & future_hard)[0]}
        return {'ok': True, 'labs': labs, 'extension': extension, 'retained_units': retained, 'board': board}

    def prepare(self, obs):
        if self.state: return deepcopy(self.state) if self._identity(obs, self.state) else {'ok': False, 'reason': 'blue_science_world_or_catalog_changed'}
        required = (PRODUCT, 'assembling-machine-1', 'inserter', 'long-handed-inserter')
        if not obs.get('technologies', {}).get(PRODUCT) or not all(obs.get('enabled_recipes', {}).get(r) for r in required):
            return {'ok': False, 'reason': 'native_chemical_science_technology_and_recipes_required'}
        sources = self._sources(obs)
        if not sources.get('ok'): return sources
        lab = self._lab_context(obs)
        if not lab.get('ok'): return lab
        try:
            rated = chemical_science_rates(self.catalog); board = lab['board']
            red = self.advanced.state['core']['reserved_area']; engine = self.engine.state['core']['reserved_area']
            # Adjacent finite rows around actual producer reservations, not map waypoints.
            x = math.ceil(max(red[1][0], engine[1][0]))+6.5; y = math.floor(min(red[0][1], engine[0][1]))+16.5
            anchors = [(x+dx, y+dy) for dx, dy in ((0, 0), (0, 16), (0, -16), (32, 0), (32, 16), (32, -16))]
            cores = [chemical_science_core(self.catalog, a, b) for a, b in anchors]
            for core in cores:
                audit = audit_chemical_science_core(core, self.catalog)
                if not audit['ok']: return {'ok': False, 'reason': 'blue_core_audit_failed', 'evidence': audit}
            future_hard = unbuilt_external_keepouts(self.builder, obs, board['blocks'])
            cores = [c for c in cores if not any(overlap(c['reserved_area'], b['reserved_area']) for b in board['blocks'].values() if b.get('reserved_area'))
                     and not any(c['reserved_area'][0][0] < px+.49 and px-.49 < c['reserved_area'][1][0]
                                 and c['reserved_area'][0][1] < py+.49 and py-.49 < c['reserved_area'][1][1] for px, py in future_hard)]
            if not cores: return {'ok': False, 'reason': 'no_blue_science_site_clear_of_reserved_and_unbuilt_external_plans'}
        except (KeyError, TypeError, ValueError) as exc: return {'ok': False, 'reason': 'blue_science_planning_context_incompatible', 'detail': str(exc)}
        budget = PlanningBudget(self.game)
        adapter = SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan, can_place=lambda es: preflight(budget.game, self.builder, obs, es))
        vectors = budget.game.query('''for _,name in ipairs({"inserter","long-handed-inserter"})do
 local p=prototypes.entity[name];local reach=name=="inserter"and 1 or 2
 if not p or p.type~="inserter"then return {ok=false,reason="blue_native_inserter_prototype_missing"}end
 local a,b=p.inserter_pickup_position,p.inserter_drop_position
 if math.floor(.5+(a.x or a[1]))~=0 or math.floor(.5+(a.y or a[2]))~=-reach
 or math.floor(.5+(b.x or b[1]))~=0 or math.floor(.5+(b.y or b[2]))~=reach then return {ok=false,reason="blue_native_inserter_vectors_unsupported"}end
end;return {ok=true}''')
        if not vectors.get('ok'): return vectors
        land = survey_slots(budget.game, [[c['reserved_area']] for c in cores])
        if not land.get('ok'): return land
        failures, attempts = [], 0
        for index in land.get('legal', []):
            if budget.expired() or attempts >= 12: return budget.failure(failures=failures[-6:], route_attempts=attempts)
            core = cores[index-1]; future = chemical_science_core(self.catalog, *xy({'position': core['anchor']}), 4)
            groups = []; reserved = merge_entities(core['entities'], future['entities'], lab['extension']['entities'])+_blue_core_keepout(core)
            feasible = True
            pairs = [(sources['sources'][p['item']], p) for p in core['ports'][:-1]]
            pairs.append((core['ports'][-1], lab['extension']['outside_port']))
            for source, destination in pairs:
                if budget.expired() or attempts >= 12: return budget.failure(failures=failures[-6:], route_attempts=attempts)
                route = plan_connection(budget.game, adapter, obs, source, destination, reserved+merge_entities(*(r['entities'] for r in groups))); attempts += 1
                if not route.get('ok'):
                    failures.append(_candidate_failure('route', route, item=source['item'], anchor=core['anchor'])); feasible = False; break
                groups.append(route)
            if not feasible: continue
            es = merge_entities(core['entities'], lab['extension']['entities'], *(r['entities'] for r in groups))
            target = _clear_planned_power_group(budget.game, core)
            if not target.get('ok'): failures.append(_candidate_failure('power_target', target)); continue
            power = connect_power_groups(budget.game, adapter, obs, [target['group']], merge_entities(es, future['entities']))
            if not power.get('ok'): failures.append(_candidate_failure('power', power)); continue
            es = merge_entities(es, power['entities'])
            whole = preflight(budget.game, self.builder, obs, es)
            if not whole.get('ok'): failures.append(_candidate_failure('whole_preflight', whole)); continue
            growth = preflight(budget.game, self.builder, obs, merge_entities(es, future['entities']))
            if not growth.get('ok'): failures.append(_candidate_failure('future_preflight', growth)); continue
            coverage = self.perimeter.prepare_coverage(obs, SCOPE_ID, [core['reserved_area']])
            if not coverage.get('ok'): return {'ok': False, 'reason': 'blue_science_defense_preflight_failed', 'evidence': coverage}
            plan = _plan(es, core['ports'], reserved_area=core['reserved_area'],
                underground_pairs=[p for r in groups for p in r['underground_pairs']], clear_obstacles=whole['clear_obstacles'], clearance_world_id=obs['world_id'])
            actual = {(e['name'], *xy(e)): e for e in obs['entities']}
            return {'ok': True, 'world_id': obs['world_id'], 'catalog_fingerprint': self.catalog.fingerprint, 'phase': 'build',
                'core': core, 'plan': plan, 'audit': audit_chemical_science_core(core, self.catalog), 'rated': rated,
                'sources': sources['sources'], 'source_plan_revisions': sources['plan_revisions'], 'lab_interface': lab['extension'],
                'retained_lab_units': lab['retained_units'], 'old_lab_ports': deepcopy(lab['labs']['ports']),
                'bindings': {_key(e): actual[(e['name'], *xy(e))]['unit_number'] for e in es if (e['name'], *xy(e)) in actual},
                'route_metrics': [dict(item=source['item'], **r['route_metrics']) for (source, _), r in zip(pairs, groups)],
                'route_attempts': attempts, 'defense_preview': coverage, 'prepared_tick': obs.get('tick')}
        return {'ok': False, 'reason': 'no_chemical_science_site_with_complete_owned_lab_connection',
                'evidence': {'failures': failures[-8:], 'legal_sites': len(land.get('legal', [])), 'route_attempts': attempts}}

    def _lab_guard(self, obs):
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        for q in self.state['retained_lab_units']:
            e = actual.get((q['name'], *xy(q)))
            if not e or e.get('unit_number') != q['unit_number'] or (q['name'] != 'small-electric-pole' and e.get('direction') != q['direction']):
                return {'status': 'blocked', 'reason': 'blue_original_lab_asset_missing_or_replaced', 'position': q['position']}
        return None

    def _bind(self, obs):
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}; changed = False
        for q in self.state['plan']['entities']:
            e = actual.get((q['name'], *xy(q))); old = self.state['bindings'].get(_key(q))
            if old and (not e or e.get('unit_number') != old): return {'status': 'blocked', 'reason': 'owned_blue_science_asset_missing_or_replaced', 'entity': q}
            if e:
                if type(e.get('unit_number')) is not int: return {'status': 'blocked', 'reason': 'blue_native_unit_identity_missing'}
                if old is None: self.state['bindings'][_key(q)] = e['unit_number']; changed = True
        if changed: self._save()
        return None

    def _reserve(self, obs):
        path = self.path.parent/'pcb-board.json'
        try: board = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc: return {'status': 'blocked', 'reason': 'blue_board_unavailable', 'detail': str(exc)}
        if not self._identity(obs, board): return {'status': 'blocked', 'reason': 'blue_board_identity_changed'}
        labs = board['blocks'].get('laboratories', {})
        if _hash(labs.get('entities')) != self.state['lab_interface']['original_geometry_hash']:
            return {'status': 'blocked', 'reason': 'blue_original_laboratory_geometry_changed'}
        name = 'chemical_science_production'; old = board['blocks'].get(name); plan = self.state['plan']
        if old and old != plan: return {'status': 'blocked', 'reason': 'blue_registered_plan_changed'}
        if any(k != name and overlap(plan['reserved_area'], b['reserved_area']) for k, b in board['blocks'].items() if b.get('reserved_area')):
            return {'status': 'blocked', 'reason': 'blue_production_reservation_conflict'}
        if not old: board['blocks'][name] = plan; _atomic_json(path, board)
        return None

    def _publish_lab_port(self, obs):
        """Add only port metadata; old red/green plans must not own the blue belt."""
        root = self.path.parent; paths = {'science': root/'automation-science.json', 'board': root/'pcb-board.json'}
        try: docs = {k: json.loads(p.read_text(encoding='utf-8')) for k, p in paths.items()}
        except (OSError, ValueError) as exc: return {'status': 'blocked', 'reason': 'blue_lab_publication_documents_unavailable', 'detail': str(exc)}
        if not all(self._identity(obs, d) for d in docs.values()): return {'status': 'blocked', 'reason': 'blue_lab_publication_identity_changed'}
        old = self.state['old_lab_ports']; new = old+[self.state['lab_interface']['registered_port']]
        for labs in (docs['science']['labs'], docs['board']['blocks']['laboratories']):
            if (_hash(labs['entities']) != self.state['lab_interface']['original_geometry_hash'] or labs['ports'] not in (old, new)):
                return {'status': 'blocked', 'reason': 'blue_lab_publication_original_entities_or_ports_changed'}
            labs['ports'] = deepcopy(new)
        for name, doc in docs.items(): _atomic_json(paths[name], doc)
        self.science.state = docs['science']; self.state['published'] = True; self._save()
        return None

    def step(self, obs):
        if not self.state:
            ready = self.prepare(obs)
            if not ready.get('ok'): return {'status': 'blocked', **ready, 'evidence': ready.get('evidence') or {k: v for k, v in ready.items() if k not in ('ok', 'status')}}
            self.state = ready; self._save()
        if not self._identity(obs, self.state): return {'status': 'blocked', 'reason': 'blue_science_world_or_catalog_changed'}
        if {item: _hash(plan) for item, plan in self._source_plans().items()} != self.state['source_plan_revisions']:
            return {'status': 'blocked', 'reason': 'blue_bound_producer_plan_changed'}
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        for source in self.state['sources'].values():
            e = actual.get(('transport-belt', *xy(source)))
            if not e or e.get('unit_number') != source['unit_number'] or e.get('direction') != source['facing']:
                return {'status': 'blocked', 'reason': 'blue_bound_ingredient_outlet_changed', 'item': source['item']}
        for guard in (self._lab_guard, self._bind, self._reserve):
            result = guard(obs)
            if result: return result
        result = self.perimeter.ensure_coverage(obs, SCOPE_ID, [self.state['core']['reserved_area']])
        if result is not None: return result
        if not self.state.get('construction_authorized'):
            check = preflight(self.game, self.builder, obs, self.state['plan']['entities'])
            if not check.get('ok'): return {'status': 'blocked', 'reason': 'blue_site_changed_after_defense', 'evidence': check}
            self.state['construction_authorized'] = True; self._save()
        pipes = surface_pipe_proof(self.game, obs, self.state['plan'])
        if not pipes.get('ok'): return {'status': 'blocked', 'reason': 'blue_preserved_pipe_network_changed', 'evidence': pipes}
        result = self.builder.ensure_plan(obs, self.state['plan'])
        if result.get('status') != 'succeeded': return result
        proof = self.proof(obs)
        if not proof: return {'status': 'waiting', 'reason': 'waiting_for_real_blue_science_at_all_eight_labs', 'evidence': self.last_proof_failure}
        result = self._publish_lab_port(obs)
        if result: return result
        self.state.update(phase='verified', commissioning_proof=proof); self._save()
        return {'status': 'succeeded', 'reason': 'normally produced blue science reaches all eight preserved labs', 'evidence': proof}

    def proof(self, obs):
        self.last_proof_failure = None; s = self.state
        def fail(reason, **extra): self.last_proof_failure = {'reason': reason, **extra}; return None
        if not s or not self._identity(obs, s): return fail('blue_checkpoint_identity_missing')
        guard = self._lab_guard(obs)
        if guard: return fail(guard['reason'], position=guard['position'])
        if not underground_proof(obs, s['plan']): return fail('blue_native_underground_pairs_unverified')
        pipes = surface_pipe_proof(self.game, obs, s['plan'])
        if not pipes.get('ok'): return fail('blue_preserved_pipe_network_changed', evidence=pipes)
        sources = self._sources(obs)
        if not sources.get('ok') or sources.get('sources') != s['sources'] or sources.get('plan_revisions') != s['source_plan_revisions']:
            return fail('blue_fresh_bound_producer_proof_changed', evidence=sources)
        coverage = self.perimeter.coverage_proof(obs, SCOPE_ID)
        if not coverage: return fail('blue_defense_coverage_missing')
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        for q in s['plan']['entities']:
            e = actual.get((q['name'], *xy(q))); unit = s['bindings'].get(_key(q))
            if not e or type(unit) is not int or e.get('unit_number') != unit or (q['name'] != 'small-electric-pole' and e.get('direction') != q['direction']):
                return fail('blue_asset_identity_or_direction_changed', entity=q)
        def bound(e): return dict(e, unit_number=s['bindings'][_key(e)])
        core, interface = s['core'], s['lab_interface']
        lab_arms = [dict(e, allowed_items=[PRODUCT], pickup_position={'x': xy(e)[0], 'y': xy(e)[1]+2},
            drop_position={'x': xy(e)[0], 'y': xy(e)[1]-2}) for e in interface['entities'] if e['name'] == 'long-handed-inserter']
        belts = s['audit']['belt_contracts']+[dict(e, allowed_items=[PRODUCT]) for e in interface['entities'] if e['name'] == 'transport-belt']
        lab_units = {(e['name'], *xy(e)): e['unit_number'] for e in s['retained_lab_units']}
        args = {'machines': [bound(e) for e in core['machines']], 'arms': [bound(e) for e in s['audit']['arm_targets']+lab_arms],
            'belts': [bound(e) for e in belts], 'sources': [dict(p, name='transport-belt') for p in s['sources'].values()],
            'lanes': [{'x': xy(e)[0], 'y': xy(e)[1]-3} for e in core['machines']],
            'output': bound(dict(core['ports'][-1], name='transport-belt')),
            'labs': [dict(e, unit_number=lab_units[('lab', *xy(e))]) for e in interface['labs']],
            'ingredients': {i['name']: True for i in self.catalog.recipes[PRODUCT]['ingredients']}}
        result = self.game.query(BLUE_SCIENCE_PROOF_LUA.replace('PAYLOAD', _payload(args)))
        if not result.get('ok'): self.last_proof_failure = result; return None
        output = dict(core['ports'][-1], name='transport-belt', unit_number=result['output_unit'], world_id=obs['world_id'], defense_scope_id=SCOPE_ID)
        return {'tick': result['tick'], 'native': result, 'rated': s['rated'], 'defense_coverage': coverage,
            'output': output, 'source': deepcopy(output), 'science_units': [m['unit_number'] for m in result['machines']],
            'lab_units': result['lab_units'], 'lab_science_stock': result['lab_science_stock'],
            'scope': 'native automatic blue delivery to eight original labs; catalog nameplate is not sustained science-consumption certification'}


def _blue_core_keepout(core):
    a, b = core['reserved_area']; clear = set()
    for p in core['ports']:
        px, py = xy(p)
        for xx in (a[0]+.5+i for i in range(int(b[0]-a[0]))):
            if (p['direction'] == 'output' and xx <= px) or (p['direction'] == 'input' and
                    ((p['facing'] == 4 and xx <= px) or (p['facing'] == 12 and xx >= px))): clear.add((xx, py))
    return [entity('pcb-reserved', a[0]+.5+i, a[1]+.5+j) for i in range(int(b[0]-a[0])) for j in range(int(b[1]-a[1]))
            if (a[0]+.5+i, a[1]+.5+j) not in clear]


BLUE_SCIENCE_PROOF_LUA = r'''
local args=helpers.json_to_table(PAYLOAD);local product="chemical-science-pack";local finished={};local stock={}
local function exact(q)local e=target(q.position,q.name or"transport-belt")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return nil end;return e end
local function quality(v)return type(v)=="string"and v or(v and v.name)end
local function same_tile(p,q)return math.floor(p.x)+.5==q.x and math.floor(p.y)+.5==q.y end
for _,q in ipairs(args.machines)do local e=exact(q);local r=e and e.get_recipe()
 if not e or e.direction~=q.direction or not r or r.name~=product or e.products_finished<=0
 or not e.is_connected_to_electric_network()or e.energy<=0 then return {ok=false,reason="blue_machine_recipe_products_or_power_missing",position=q.position}end
 local input,output=e.get_inventory(defines.inventory.crafter_input),e.get_output_inventory()
 if not input or not output then return {ok=false,reason="blue_native_machine_inventory_missing"}end
 for i=1,#input do local t=input[i];if t.valid_for_read and(not args.ingredients[t.name]or t.quality.name~="normal")then
  return {ok=false,reason="blue_machine_input_contaminated",position=q.position}end end
 for i=1,#output do local t=output[i];if t.valid_for_read and(t.name~=product or t.quality.name~="normal")then
  return {ok=false,reason="blue_machine_output_contaminated",position=q.position}end end
 finished[#finished+1]={unit_number=e.unit_number,products_finished=e.products_finished}
end
for _,q in ipairs(args.arms)do local e=exact(q)
 if not e or e.direction~=q.direction or not e.is_connected_to_electric_network()or e.energy<=0
 or not same_tile(e.pickup_position,q.pickup_position)or not same_tile(e.drop_position,q.drop_position)then
  return {ok=false,reason="blue_inserter_handoff_or_power_changed",position=q.position}end
 local t=e.held_stack;if t and t.valid_for_read then local allow={};for _,name in ipairs(q.allowed_items)do allow[name]=true end
  if not allow[t.name]or t.quality.name~="normal"then return {ok=false,reason="blue_inserter_held_item_contaminated",position=q.position}end
 end
end
for _,q in ipairs(args.belts)do local e=exact(q);if not e or e.direction~=q.direction then return {ok=false,reason="blue_belt_identity_or_direction_changed",position=q.position}end
 local allow={};for _,name in ipairs(q.allowed_items)do allow[name]=true end
 for lane=1,e.get_max_transport_line_index()do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if not allow[row.name]or quality(row.quality)~="normal"then return {ok=false,reason="blue_belt_contaminated",position=q.position}end
  stock[row.name]=(stock[row.name]or 0)+row.count
 end end
end
for _,q in ipairs(args.sources)do local e=exact(q)
 if not e or e.direction~=q.facing then return {ok=false,reason="blue_ingredient_outlet_identity_changed",item=q.item}end
 for lane=1,e.get_max_transport_line_index()do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if row.name~=q.item or quality(row.quality)~="normal"then return {ok=false,reason="blue_ingredient_outlet_contaminated",item=q.item}end
 end end
end
for _,p in ipairs(args.lanes)do local e=target(p,"transport-belt")
 if not e or e.force~=f or e.surface~=s or e.direction~=4 then return {ok=false,reason="blue_mixed_feed_missing"}end
 for lane=1,2 do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if row.name~=(lane==1 and"engine-unit"or"advanced-circuit")or quality(row.quality)~="normal"then return {ok=false,reason="blue_mixed_lane_contaminated",position=p}end
 end end
end
for _,item in ipairs({"engine-unit","advanced-circuit","sulfur"})do
 if not stock[item]or stock[item]<=0 then return {ok=false,reason="blue_ingredient_buffers_empty",item=item}end
end
local outlet=exact(args.output);if not outlet then return {ok=false,reason="blue_output_identity_changed"}end
local count=0;for lane=1,2 do for _,row in pairs(outlet.get_transport_line(lane).get_contents())do
 if row.name~=product or quality(row.quality)~="normal"then return {ok=false,reason="blue_output_contaminated"}end;count=count+row.count
end end
if count<=0 then return {ok=false,reason="blue_output_empty"}end
local labs={};local total=0
for _,q in ipairs(args.labs)do local e=exact(q)
 if not e or not e.is_connected_to_electric_network()or e.energy<=0 then return {ok=false,reason="blue_original_lab_identity_or_power_changed",position=q.position}end
 local input=e.get_inventory(defines.inventory.lab_input);if not input then return {ok=false,reason="blue_lab_native_inventory_missing"}end
 local amounts={};for i=1,#input do local t=input[i];if t.valid_for_read then
  if t.quality.name~="normal"then return {ok=false,reason="blue_lab_quality_contaminated",position=q.position}end
  amounts[t.name]=(amounts[t.name]or 0)+t.count
 end end
 for _,name in ipairs({"automation-science-pack","logistic-science-pack",product})do
  if not amounts[name]or amounts[name]<=0 then return {ok=false,reason="blue_red_green_stock_required_at_every_original_lab",item=name,unit_number=e.unit_number}end
 end
 labs[#labs+1]=e.unit_number;total=total+amounts[product]
end
if #labs~=8 then return {ok=false,reason="blue_exactly_eight_original_labs_required"}end
return {ok=true,tick=game.tick,machines=finished,blue_science=count,output_unit=outlet.unit_number,lab_units=labs,lab_science_stock=total,normal_core_items=stock}
'''
