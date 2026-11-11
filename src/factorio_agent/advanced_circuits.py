"""Normally supplied advanced circuits, with dedicated green and wire subcells."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from factorio_ai.deterministic_state import _atomic_json
from factorio_ai.world_catalog import _yield
from .board import entity
from .bus_tap import BusTap, extend_bus, reconstruct_bus, signature
from .chemical_production import ChemicalProduction
from .compact_green import _machine_at, _spread, _transport
from .industrial import PlanningBudget, free_tap_parameters
from .manufacturing import connect_power_groups
from .planning import overlap, preflight, survey_slots
from .smelting import merge_entities, port
from .transport import VECTORS, plan_connection, surface_pipe_proof, underground_proof, xy


SCOPE_ID = 'advanced-circuit-production'
TAP_NAMES = {'iron-plate': 'advanced-iron', 'copper-plate': 'advanced-copper'}
ARM_BUDGETS = {'inserter': 30, 'long-handed-inserter': 30, 'fast-inserter': 120}


def _key(e):
    return e['name'] + ':' + str(e['position']['x']) + ':' + str(e['position']['y'])


def _payload(value):
    return json.dumps(json.dumps(value, separators=(',', ':')))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _candidate_failure(stage, result, **context):
    """Keep public blocker evidence useful without serializing route/plan payloads."""
    keys = ('ok', 'reason', 'stage', 'visited', 'rejected_skeletons', 'max_nodes', 'length', 'turns',
            'detour_ratio', 'item', 'position', 'queries', 'elapsed_seconds', 'planning_seconds_budget')
    detail = {k: deepcopy(result[k]) for k in keys if k in result}
    if isinstance(result.get('detail'), str): detail['detail'] = result['detail'][:600]
    return {'stage': stage, **context, 'detail': detail}


def advanced_circuit_rates(catalog, count=2):
    """Catalog nameplate and recursive BOM; arm budgets are conservative design limits."""
    if type(count) is not int or not 2 <= count <= 4:
        raise ValueError('two to four advanced-circuit assemblers supported')
    expected = {'advanced-circuit': {'plastic-bar', 'electronic-circuit', 'copper-cable'},
                'electronic-circuit': {'iron-plate', 'copper-cable'}, 'copper-cable': {'copper-plate'}}
    machine = catalog.entities['assembling-machine-1']
    speed = machine['crafting_speed']
    if type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0:
        raise ValueError('finite positive native assembling speed required')
    recipes = {}
    for name, items in expected.items():
        r = catalog.recipes[name]
        if (len(r['products']) != 1 or r['products'][0]['name'] != name
                or r['products'][0].get('type', 'item') != 'item'
                or len(r['ingredients']) != len(items) or {i['name'] for i in r['ingredients']} != items
                or any(i.get('type', 'item') != 'item' or type(i['amount']) not in (int, float)
                       or not math.isfinite(i['amount']) or i['amount'] <= 0 for i in r['ingredients'])
                or type(r['energy']) not in (int, float) or not math.isfinite(r['energy']) or r['energy'] <= 0
                or r.get('category') not in machine.get('crafting_categories', [])):
            raise ValueError('installed ordinary advanced/green/cable recipes required')
        output = _yield(r['products'][0])
        if not math.isfinite(output) or output <= 0:
            raise ValueError('positive expected native recipe yield required')
        recipes[name] = (r, {i['name']: i['amount'] for i in r['ingredients']}, output)
    red, red_raw, red_yield = recipes['advanced-circuit']
    green, green_raw, green_yield = recipes['electronic-circuit']
    wire, wire_raw, wire_yield = recipes['copper-cable']
    red_cycles = 60 * speed / red['energy']
    green_rate = count * red_cycles * red_raw['electronic-circuit']
    green_cycles = green_rate / green_yield
    green_wire = green_cycles * green_raw['copper-cable']
    direct_wire = count * red_cycles * red_raw['copper-cable']
    total_wire = green_wire + direct_wire
    wires = 1 if count == 2 else 2
    wire_nominal = 60 * speed / wire['energy'] * wire_yield
    green_nominal = 60 * speed / green['energy'] * green_yield
    iron = green_cycles * green_raw['iron-plate']
    copper = total_wire / wire_yield * wire_raw['copper-plate']
    if (green_rate > green_nominal or total_wire > wire_nominal * wires
            or max(iron, green_rate, green_wire / wires, direct_wire / wires, copper / wires) > ARM_BUDGETS['fast-inserter']
            or red_cycles * (red_raw['electronic-circuit'] + red_raw['copper-cable']) > ARM_BUDGETS['inserter']
            or max(red_cycles * red_raw['plastic-bar'], red_cycles * red_yield) > ARM_BUDGETS['long-handed-inserter']):
        raise ValueError('dedicated subcell or conservative inserter capacity exceeded')
    return {'advanced_circuits_per_minute': count * red_cycles * red_yield,
            'iron_per_minute': iron, 'copper_per_minute': copper,
            'plastic_per_minute': count * red_cycles * red_raw['plastic-bar'],
            'green_per_minute': green_rate, 'green_wire_per_minute': green_wire,
            'direct_wire_per_minute': direct_wire, 'total_wire_per_minute': total_wire,
            'advanced_assembler_count': count, 'green_assembler_count': 1, 'wire_assembler_count': wires,
            'wire_nominal_per_minute': wire_nominal * wires, 'green_nominal_per_minute': green_nominal,
            'component_nominal_iron_per_minute': 60 * speed / green['energy'] * green_raw['iron-plate'],
            'component_nominal_copper_per_minute': 60 * speed / wire['energy'] * wire_raw['copper-plate'] * wires,
            'inserter_design_budgets_per_minute': dict(ARM_BUDGETS),
            'capacity_scope': 'catalog nameplate and steady BOM; finite buffers create higher transient demand; shared surplus and arm budgets are not measured throughput'}


def advanced_circuit_core(catalog, x, y, count=2):
    """31x22 fixed headers; four red slots and a second wire slot remain reserved."""
    rated = advanced_circuit_rates(catalog, count)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v % 1 != .5 for v in (x, y)):
        raise ValueError('half-tile advanced-circuit anchors required')
    es, pairs = [], []
    def add(name, dx, dy, direction=0, **extra):
        e = dict(entity(name, x+dx, y+dy, direction), **extra); es.append(e); return e
    def belt(dx, dy, direction=4):
        return add('transport-belt', dx, dy, direction)
    def arm(dx, dy, direction=0, name='fast-inserter', role=None):
        return add(name, dx, dy, direction, arm_role=role)
    def pair(ax, bx, yy, d):
        add('underground-belt', ax, yy, d, belt_to_ground_type='input')
        add('underground-belt', bx, yy, d, belt_to_ground_type='output')
        pairs.append({'input': [x+ax, y+yy], 'output': [x+bx, y+yy], 'direction': d, 'span': abs(bx-ax)})
    # Two perpendicular entries keep copper LEFT and iron RIGHT on one east header.
    belt(-3, -1); belt(-2, -1); belt(-1, -1, 8)
    for xx in range(-3, 0): belt(xx, 2)
    belt(0, 2, 0); belt(0, 1, 0)
    for xx in range(-1, 12): belt(xx, 0)
    wires = [add('assembling-machine-1', 3, 3, recipe='copper-cable')]
    green = add('assembling-machine-1', 7, 3, recipe='electronic-circuit')
    arm(3, 1, role='copper-input'); arm(7, 1, role='iron-input')
    arm(5, 3, 12, role='green-wire'); arm(3, 5, role='direct-wire'); arm(7, 5, role='green-output')
    if count > 2:
        wires.append(add('assembling-machine-1', 11, 3, recipe='copper-cable'))
        arm(11, 1, role='copper-input'); arm(9, 3, 4, role='green-wire'); arm(11, 5, role='direct-wire')
    for xx in range(-1, 12):
        if xx not in (6, 7, 8): belt(xx, 6, 8 if xx == -1 else 12)
    pair(8, 6, 6, 12); belt(-1, 7, 8); belt(-1, 8, 8)
    for yy in range(6, 10): belt(7, yy, 8)
    for xx in (7, 6, 5): belt(xx, 10, 12)
    pair(4, 1, 10, 12); belt(0, 10, 12); belt(-1, 10, 0)
    for xx in range(-1, 22):
        if xx not in (6, 7, 8): belt(xx, 9)
    pair(6, 8, 9, 4)
    for xx in range(2, 26):
        if xx not in (6, 7, 8): belt(xx, 8, 12)
    pair(8, 6, 8, 12)
    reds = []
    for i in range(count):
        xx = 3+6*i; reds.append(add('assembling-machine-1', xx, 12, recipe='advanced-circuit'))
        arm(xx, 10, name='inserter', role='red-components')
        arm(xx-1, 10, name='long-handed-inserter', role='red-plastic')
        arm(xx+2, 12, 12, name='inserter', role='red-output')
        for yy in range(12, 18): belt(xx+3, yy, 8)
        add('small-electric-pole', xx-2, 11); add('small-electric-pole', xx+2, 11)
    for xx in range(6, 27): belt(xx, 18)
    for xx, yy in ((5, 1), (5, 5)) + (((9, 1), (9, 5)) if count > 2 else ()): add('small-electric-pole', xx, yy)
    ports = [port('copper-plate', x-3, y-1, 4), port('iron-plate', x-3, y+2, 4),
             port('plastic-bar', x+25, y+8, 12), port('advanced-circuit', x+26, y+18, 4, direction='output')]
    extensions = [entity('assembling-machine-1', x+3+6*i, y+12, recipe='advanced-circuit') for i in range(count, 4)]
    if count == 2: extensions.append(entity('assembling-machine-1', x+11, y+3, recipe='copper-cable'))
    return _plan(es, ports, machines=wires+[green]+reds, red_machines=reds, wire_machines=wires, green_machine=green,
                 underground_pairs=pairs, anchor={'x': x, 'y': y}, rated=rated,
                 reserved_area=[[x-4.5, y-1.5], [x+26.5, y+20.5]], extension_slots=extensions,
                 output_access=[entity('transport-belt', x+dx, y+18, 4) for dx in (27, 28)],
                 raw_feed_lanes={'left': 'copper-plate', 'right': 'iron-plate'},
                 component_feed_lanes={'left': 'copper-cable', 'right': 'electronic-circuit'},
                 layout_policy='dedicated-advanced-circuit-subcells-v1')


def audit_advanced_circuit_core(plan, catalog):
    """Exact prospective footprints, lane reachability, power and native arm budgets."""
    errors = []; es = plan['entities']; geometry = FactoryBuilder.__new__(FactoryBuilder)
    boxes = [geometry._occupied_by_plan([e]) for e in es]; occupied = set().union(*boxes)
    if sum(map(len, boxes)) != len(occupied): errors.append('prospective_footprint_overlap')
    a, b = plan['reserved_area']
    if any(not(a[0] <= px-.5 and px+.5 <= b[0] and a[1] <= py-.5 and py+.5 <= b[1]) for px, py in occupied):
        errors.append('entity_outside_reservation')
    if any(geometry._occupied_by_plan([slot]) & occupied for slot in plan['extension_slots']): errors.append('future_machine_slot_occupied')
    belts, graph = _transport(es, plan['underground_pairs']); declared = set()
    for p in plan['underground_pairs']:
        start, end, d = tuple(p['input']), tuple(p['output']), p['direction']; dx, dy = VECTORS[d]; span = p['span']
        if not 2 <= span <= 5 or end != (start[0]+span*dx, start[1]+span*dy): errors.append('invalid_underground_span')
        for pt, role in ((start, 'input'), (end, 'output')):
            declared.add(pt); e = belts.get(pt, {})
            if (e.get('name'), e.get('direction'), e.get('belt_to_ground_type')) != ('underground-belt', d, role): errors.append('missing_underground_mouth')
        if belts.get((start[0]-dx, start[1]-dy), {}).get('direction') != d or belts.get((end[0]+dx, end[1]+dy), {}).get('direction') != d:
            errors.append('underground_requires_straight_mouths')
        for pt, e in belts.items():
            if pt in (start, end) or e['name'] != 'underground-belt' or e['direction'] != d: continue
            if (pt[0]-start[0])*dy == (pt[1]-start[1])*dx and 0 < (pt[0]-start[0])*dx+(pt[1]-start[1])*dy <= span:
                errors.append('underground_pair_can_be_stolen')
    if declared != {pt for pt, e in belts.items() if e['name'] == 'underground-belt'}: errors.append('undeclared_underground_mouth')
    machines = [e for e in es if e['name'] == 'assembling-machine-1']
    arms = [e for e in es if e['name'] in ARM_BUDGETS]; poles = [xy(e) for e in es if e['name'] == 'small-electric-pole']
    for e in machines+arms:
        if not any(abs(xy(e)[0]-px) <= 2.5 and abs(xy(e)[1]-py) <= 2.5 for px, py in poles): errors.append('unpowered_consumer:'+str(xy(e)))
    connected = {poles[0]} if poles else set()
    while True:
        more = {p for p in poles if any(math.dist(p, q) <= 7.5 for q in connected)}
        if more <= connected: break
        connected |= more
    if len(connected) != len(poles): errors.append('disconnected_internal_poles')
    lanes, supplied, ready = defaultdict(set), defaultdict(set), set()
    for p in plan['ports']:
        if p['direction'] == 'input':
            for lane in ('left', 'right'): lanes[xy(p), lane].add(p['item'])
    for _ in range(len(machines)+2):
        _spread(lanes, graph)
        for arm in arms:
            px, py = xy(arm); dx, dy = VECTORS[arm['direction']]; reach = 2 if arm['name'] == 'long-handed-inserter' else 1
            pickup, drop = (px+dx*reach, py+dy*reach), (px-dx*reach, py-dy*reach)
            source, dest = _machine_at(machines, pickup), _machine_at(machines, drop)
            items = {source['recipe']} if source and xy(source) in ready else set()
            if pickup in belts: items |= lanes[pickup, 'left'] | lanes[pickup, 'right']
            if dest: supplied[xy(dest)] |= items & {i['name'] for i in catalog.recipes[dest['recipe']]['ingredients']}
            elif drop in belts:
                vx, vy = VECTORS[belts[drop]['direction']]; side = (px-drop[0])*vy-(py-drop[1])*vx
                lanes[drop, 'right' if side > 0 else 'left'] |= items
        ready |= {xy(m) for m in machines if {i['name'] for i in catalog.recipes[m['recipe']]['ingredients']} <= supplied[xy(m)]}
    _spread(lanes, graph)
    for m in machines:
        if xy(m) not in ready: errors.append('unreachable_ingredients:'+str(xy(m)))
    checks = []
    for m in plan['red_machines']: checks.append(((xy(m)[0], xy(m)[1]-3), plan['component_feed_lanes']))
    for m in plan['wire_machines']+[plan['green_machine']]: checks.append(((xy(m)[0], xy(m)[1]-3), plan['raw_feed_lanes']))
    for pt, expected in checks:
        for lane, item in expected.items():
            if lanes[pt, lane] != {item}: errors.append('typed_lane_mismatch:'+str(pt)+':'+lane)
    output = plan['ports'][-1]
    if lanes[xy(output), 'left'] | lanes[xy(output), 'right'] != {'advanced-circuit'}: errors.append('output_unreachable_or_contaminated')
    rated = advanced_circuit_rates(catalog, len(plan['red_machines'])); wire_count = len(plan['wire_machines'])
    rates = {'copper-input': rated['copper_per_minute']/wire_count, 'iron-input': rated['iron_per_minute'],
             'green-wire': rated['green_wire_per_minute']/wire_count, 'direct-wire': rated['direct_wire_per_minute']/wire_count,
             'green-output': rated['green_per_minute'],
             'red-components': (rated['direct_wire_per_minute']+rated['green_per_minute'])/len(plan['red_machines']),
             'red-plastic': rated['plastic_per_minute']/len(plan['red_machines']),
             'red-output': rated['advanced_circuits_per_minute']/len(plan['red_machines'])}
    targets = []
    for arm in arms:
        px, py = xy(arm); dx, dy = VECTORS[arm['direction']]; reach = 2 if arm['name'] == 'long-handed-inserter' else 1
        pickup, drop = (px+dx*reach, py+dy*reach), (px-dx*reach, py-dy*reach)
        source, dest = _machine_at(machines, pickup), _machine_at(machines, drop)
        items = {source['recipe']} if source else lanes[pickup, 'left'] | lanes[pickup, 'right']
        if dest: items &= {i['name'] for i in catalog.recipes[dest['recipe']]['ingredients']}
        demand = rates.get(arm.get('arm_role'), math.inf)
        if not items or demand > ARM_BUDGETS[arm['name']]: errors.append('inserter_budget_or_target_invalid:'+str(xy(arm)))
        targets.append(dict(arm, pickup_position={'x': pickup[0], 'y': pickup[1]}, drop_position={'x': drop[0], 'y': drop[1]},
                            allowed_items=sorted(items), steady_items_per_minute=demand,
                            conservative_design_budget_per_minute=ARM_BUDGETS[arm['name']]))
    return {'ok': not errors, 'errors': errors, 'machine_count': len(machines), 'arm_targets': targets,
            'belt_contracts': [dict(e, allowed_items=sorted(lanes[pt, 'left'] | lanes[pt, 'right'])) for pt, e in belts.items()],
            'rated': rated}


def advanced_circuit_anchors(bus, board):
    """Finite districts beside existing science, then above/below the same corridor."""
    _, _, _, north, _, east = reconstruct_bus(bus)
    blocks = board.get('blocks', {})
    science = [b['reserved_area'] for name, b in blocks.items()
               if name in ('laboratories', 'automation_science', 'logistic_science') and b.get('reserved_area')]
    right = max([east]+[a[1][0] for a in science if a[0][1] <= north+30])
    x = math.ceil(right)+20.5
    engine = blocks.get('engine_production', {}).get('reserved_area')
    y = math.floor(engine[0][1])+.5 if engine else math.floor(north)+6.5
    return [(x+dx, y+dy) for dx, dy in ((0, 0), (32, 0), (0, -24), (32, -24), (0, 24), (32, 24))]


def _core_keepout(core):
    a, b = core['reserved_area']; approaches = set()
    for p in core['ports'][:-1]:
        px, py = xy(p)
        for xx in (a[0]+.5+i for i in range(int(b[0]-a[0]))):
            if (p['facing'] == 4 and xx <= px) or (p['facing'] == 12 and xx >= px): approaches.add((xx, py))
    return [entity('pcb-reserved', a[0]+.5+i, a[1]+.5+j)
            for i in range(int(b[0]-a[0])) for j in range(int(b[1]-a[1]))
            if (a[0]+.5+i, a[1]+.5+j) not in approaches]


def _same_bus_geometry(a, b):
    def pairs(bus):
        return {(tuple(p['input']), tuple(p['output']), p['direction'], p['span']) for p in bus['underground_pairs']}
    return ({signature(e) for e in a['entities']} == {signature(e) for e in b['entities']}
            and a['ports'] == b['ports'] and a['reservations'] == b['reservations'] and a['trunks'] == b['trunks']
            and pairs(a) == pairs(b))


def combine_tap_previews(old_bus, first, second, obs):
    """Geometry only: no prospective entity is assigned an invented native unit."""
    if first['old_bus'] != old_bus or second['old_bus'] != old_bus:
        return {'ok': False, 'reason': 'advanced_tap_previews_must_share_actual_old_bus'}
    if (abs(first['parameters']['splitter_y']-second['parameters']['splitter_y']) < 3
            or first['remove']['unit_number'] == second['remove']['unit_number']):
        return {'ok': False, 'reason': 'advanced_tap_stations_or_recovery_units_conflict'}
    combined = extend_bus(first['new_bus'], second['parameters'], obs)
    if not combined.get('ok'): return combined
    old_keys = {signature(e) for e in old_bus['entities']}
    expected = {signature(e) for e in first['added_entities']+second['added_entities']}
    actual = {signature(e) for e in combined['bus']['entities']} - old_keys
    if actual != expected:
        return {'ok': False, 'reason': 'advanced_joint_taps_would_change_independent_geometry'}
    return {'ok': True, 'bus': combined['bus'],
            'added_entities': [e for e in combined['bus']['entities'] if signature(e) in actual],
            'ignored_units': [first['remove']['unit_number'], second['remove']['unit_number']]}


class AdvancedCircuitProduction:
    def __init__(self, game, builder, catalog, perimeter, *, bus_factory=None, science=None, green=None, chemical=None):
        self.game, self.builder, self.catalog, self.perimeter = game, builder, catalog, perimeter
        self.tap = BusTap(game, builder, catalog, bus_factory=bus_factory, science=science, green=green)
        self.chemical = chemical if chemical is not None else ChemicalProduction(game, builder, catalog, perimeter)
        self.path = Path(game.cfg.runtime_dir)/'advanced-circuits.json'
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        self.last_proof_failure = None

    def _identity(self, obs, value):
        return (obs.get('ok', True) and isinstance(obs.get('world_id'), str) and bool(obs['world_id'])
                and value.get('world_id') == obs['world_id'] and value.get('catalog_fingerprint') == self.catalog.fingerprint)

    def _save(self):
        _atomic_json(self.path, self.state)

    def _plastic_source(self, obs):
        entry = self.chemical.state.get('products', {}).get('plastic-bar', {})
        if not self._identity(obs, self.chemical.state) or entry.get('phase') != 'verified':
            return {'ok': False, 'reason': 'commissioned_automatic_plastic_factory_required'}
        proof = self.chemical.proof(obs, {'product': 'plastic-bar'})
        if not proof:
            return {'ok': False, 'reason': 'fresh_native_automatic_plastic_proof_required',
                    'evidence': getattr(self.chemical, 'last_proof_failure', None)}
        source = proof.get('source', {}); actual = next((e for e in obs['entities'] if e['name'] == 'transport-belt' and xy(e) == xy(source)), None) if source.get('position') else None
        if (source.get('kind') != 'item' or source.get('item') != 'plastic-bar' or source.get('direction') != 'output'
                or source.get('world_id') != obs['world_id'] or type(source.get('unit_number')) is not int
                or not actual or actual.get('unit_number') != source['unit_number'] or actual.get('direction') != source.get('facing')):
            return {'ok': False, 'reason': 'exact_fixed_typed_plastic_outlet_required'}
        return {'ok': True, 'source': deepcopy(source), 'plan_revision': _hash(entry['plan']), 'proof': proof}

    def prepare(self, obs):
        """Read-only whole two-tap, three-route, growth, power and defense preflight."""
        if self.state:
            return deepcopy(self.state) if self._identity(obs, self.state) else {'ok': False, 'reason': 'advanced_circuit_world_or_catalog_changed'}
        required = ('advanced-circuit', 'electronic-circuit', 'copper-cable', 'assembling-machine-1',
                    'fast-inserter', 'inserter', 'long-handed-inserter', 'underground-belt', 'splitter')
        if not obs.get('technologies', {}).get('advanced-circuit') or not all(obs.get('enabled_recipes', {}).get(r) for r in required):
            return {'ok': False, 'reason': 'native_advanced_circuit_technology_and_component_recipes_required'}
        plastic = self._plastic_source(obs)
        if not plastic.get('ok'): return plastic
        try:
            docs = self.tap._documents(); old_bus = docs['factory']['bus']; board = docs['board']; reconstruct_bus(old_bus)
            if not all(self._identity(obs, d) for d in docs.values()) or board.get('shared_main_bus') != old_bus:
                return {'ok': False, 'reason': 'advanced_circuit_bus_authority_identity_changed'}
            if self.tap.state and self.tap.state.get('phase') != 'verified': return {'ok': False, 'reason': 'another_bus_tap_cutover_unfinished'}
            if any(p.get('name') in TAP_NAMES.values() for p in old_bus['ports']): return {'ok': False, 'reason': 'advanced_tap_exists_without_factory_checkpoint'}
            rated = advanced_circuit_rates(self.catalog)
            choices = {item: free_tap_parameters(old_bus, name=name, item=item,
                       demand_per_minute=rated['iron_per_minute' if item == 'iron-plate' else 'copper_per_minute'], priority='surplus')
                       for item, name in TAP_NAMES.items()}
            cores = [advanced_circuit_core(self.catalog, x, y) for x, y in advanced_circuit_anchors(old_bus, board)]
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return {'ok': False, 'reason': 'advanced_circuit_preparation_context_unavailable', 'detail': str(exc)}
        for core in cores:
            audit = audit_advanced_circuit_core(core, self.catalog)
            if not audit['ok']: return {'ok': False, 'reason': 'advanced_circuit_core_audit_failed', 'evidence': audit}
        cores = [c for c in cores if not any(overlap(c['reserved_area'], b['reserved_area']) for b in board['blocks'].values() if b.get('reserved_area'))]
        budget = PlanningBudget(self.game)
        adapter = SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan,
                                  can_place=lambda es: preflight(budget.game, self.builder, obs, es))
        vectors = budget.game.query(ADVANCED_ARM_VECTORS_LUA)
        if not vectors.get('ok'): return vectors
        land = survey_slots(budget.game, [[c['reserved_area']] for c in cores])
        if not land.get('ok'): return land
        contexts = []
        for index in land.get('legal', []):
            core = cores[index-1]; future = advanced_circuit_core(self.catalog, *xy({'position': core['anchor']}), 4)
            ports = {p['item']: p for p in core['ports']}
            iron = sorted(choices['iron-plate'], key=lambda p: abs(p['splitter_y']-1-ports['iron-plate']['position']['y']))[:2]
            candidates = []
            for a in iron:
                copper = sorted((p for p in choices['copper-plate'] if abs(p['splitter_y']-a['splitter_y']) >= 3),
                                key=lambda p: abs(p['splitter_y']-1-ports['copper-plate']['position']['y']))
                if copper: candidates.append((a, copper[0]))
            contexts.append({'core': core, 'future': future, 'ports': ports, 'parameters': candidates, 'plastic_route': None})
        failures, attempts = [], 0
        preview = BusTap(budget.game, adapter, self.catalog)
        for rank in range(2):
            for ctx in contexts:
                if rank >= len(ctx['parameters']) or ctx.get('plastic_failed'): continue
                core, future, ports = ctx['core'], ctx['future'], ctx['ports']
                if budget.expired() or attempts >= 12: return budget.failure(failures=failures[-6:], route_attempts=attempts)
                reserved = merge_entities(core['entities'], future['entities'])+_core_keepout(core)
                plastic_route = ctx['plastic_route']
                if plastic_route is None:
                    plastic_route = plan_connection(budget.game, adapter, obs, plastic['source'], ports['plastic-bar'], reserved); attempts += 1
                    ctx['plastic_route'] = plastic_route
                    if not plastic_route.get('ok'):
                        ctx['plastic_failed'] = True; failures.append(_candidate_failure('plastic_route', plastic_route, anchor=core['anchor'])); continue
                iron_params, copper_params = ctx['parameters'][rank]
                first = preview.prepare(obs, iron_params)
                if not first.get('ok'): failures.append(_candidate_failure('iron_tap', first, parameters=iron_params)); continue
                second = preview.prepare(obs, copper_params)
                if not second.get('ok'): failures.append(_candidate_failure('copper_tap', second, parameters=copper_params)); continue
                joint = combine_tap_previews(old_bus, first, second, obs)
                if not joint.get('ok'): failures.append(_candidate_failure('joint_taps', joint)); continue
                routes = [plastic_route]; feasible = True
                for item, params in (('iron-plate', iron_params), ('copper-plate', copper_params)):
                    if budget.expired() or attempts >= 12: return budget.failure(failures=failures[-6:], route_attempts=attempts)
                    source = next(p for p in joint['bus']['ports'] if p['name'] == params['name'])
                    route = plan_connection(budget.game, adapter, obs, source, ports[item],
                                            reserved+joint['added_entities']+merge_entities(*(p['entities'] for p in routes))); attempts += 1
                    if not route.get('ok'):
                        failures.append(_candidate_failure(item+'_route', route, anchor=core['anchor'], parameters=params)); feasible = False; break
                    routes.append(route)
                if not feasible: continue
                es = merge_entities(core['entities'], *(p['entities'] for p in routes))
                # Pick only an already-planned pole, without altering any geometry.
                from .engine_production import _clear_planned_power_group
                target = _clear_planned_power_group(budget.game, core)
                if not target.get('ok'): failures.append(_candidate_failure('power_target', target)); continue
                power = connect_power_groups(budget.game, adapter, obs, [target['group']], merge_entities(es, future['entities']))
                if not power.get('ok'): failures.append(_candidate_failure('power', power)); continue
                es = merge_entities(es, power['entities'])
                whole = preflight(budget.game, self.builder, obs, merge_entities(es, joint['added_entities'], core['output_access']), ignored_units=joint['ignored_units'])
                if not whole.get('ok'): failures.append(_candidate_failure('whole_preflight', whole)); continue
                growth = preflight(budget.game, self.builder, obs,
                    merge_entities(future['entities'], *(p['entities'] for p in routes), power['entities'], joint['added_entities'], core['output_access']), ignored_units=joint['ignored_units'])
                if not growth.get('ok'): failures.append(_candidate_failure('future_preflight', growth)); continue
                plan = _plan(es, core['ports'], reserved_area=core['reserved_area'],
                             underground_pairs=core['underground_pairs']+[pair for p in routes for pair in p['underground_pairs']],
                             output_access=core['output_access'], clear_obstacles=whole['clear_obstacles'], clearance_world_id=obs['world_id'])
                defense = self.perimeter.prepare_coverage(obs, SCOPE_ID, [core['reserved_area']])
                if not defense.get('ok'): return {'ok': False, 'reason': 'advanced_circuit_defense_preflight_failed', 'evidence': defense}
                actual = {(e['name'], *xy(e)): e for e in obs['entities']}
                bindings = {_key(e): actual[(e['name'], *xy(e))]['unit_number'] for e in es if (e['name'], *xy(e)) in actual}
                return {'ok': True, 'schema_version': 1, 'world_id': obs['world_id'], 'catalog_fingerprint': self.catalog.fingerprint,
                        'phase': 'stock_taps', 'core': core, 'plan': plan, 'audit': audit_advanced_circuit_core(core, self.catalog),
                        'rated': rated, 'bindings': bindings, 'plastic_source': plastic['source'], 'plastic_plan_revision': plastic['plan_revision'],
                        'parameters': [first['parameters'], second['parameters']], 'first_tap': first,
                        'second_added_entities': second['added_entities'], 'second_remove_unit': second['remove']['unit_number'],
                        'prospective_bus': joint['bus'], 'tap_stock': dict(Counter(e['name'] for e in joint['added_entities'])),
                        'tap_sources': {}, 'tap_index': 0, 'route_attempts': attempts,
                        'route_metrics': [dict(item=item, **p['route_metrics']) for item, p in zip(('plastic-bar', 'iron-plate', 'copper-plate'), routes)],
                        'defense_preview': defense,
                        'defense_preview_scope': 'core coverage only; complete plan is registered before actual protected-post placement',
                        'prepared_tick': obs.get('tick')}
        return {'ok': False, 'reason': 'no_advanced_circuit_site_with_complete_typed_inputs',
                'evidence': {'failures': failures[-8:], 'legal_sites': len(contexts), 'route_attempts': attempts}}

    def _bind(self, obs):
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}; changed = False
        for spec in self.state['plan']['entities']:
            live = actual.get((spec['name'], *xy(spec))); old = self.state['bindings'].get(_key(spec))
            if old and (not live or live.get('unit_number') != old):
                return {'status': 'blocked', 'reason': 'owned_advanced_circuit_asset_missing_or_replaced', 'entity': spec}
            if live:
                if type(live.get('unit_number')) is not int: return {'status': 'blocked', 'reason': 'advanced_circuit_native_unit_missing'}
                if old is None: self.state['bindings'][_key(spec)] = live['unit_number']; changed = True
        if changed: self._save()
        return None

    def _reserve(self, obs):
        path = self.path.parent/'pcb-board.json'
        try: board = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc: return {'status': 'blocked', 'reason': 'advanced_circuit_board_unavailable', 'detail': str(exc)}
        if not self._identity(obs, board): return {'status': 'blocked', 'reason': 'advanced_circuit_board_identity_changed'}
        name = 'advanced_circuit_production'; old = board['blocks'].get(name); plan = self.state['plan']
        if old and old != plan: return {'status': 'blocked', 'reason': 'advanced_circuit_reserved_plan_changed'}
        if any(k != name and overlap(plan['reserved_area'], b['reserved_area']) for k, b in board['blocks'].items() if b.get('reserved_area')):
            return {'status': 'blocked', 'reason': 'advanced_circuit_reservation_conflict'}
        if not old: board['blocks'][name] = plan; _atomic_json(path, board)
        return None

    def _tap_step(self, obs):
        s = self.state; index = s['tap_index']; params = s['parameters'][index]
        if self.tap.state.get('parameters') != params:
            if self.tap.state and self.tap.state.get('phase') != 'verified':
                return {'status': 'blocked', 'reason': 'another_bus_tap_cutover_unfinished'}
            # Both actual installations get fresh identities. In particular, the
            # second snapshot must contain the REAL, now-built first tap units.
            ready = self.tap.prepare(obs, params)
            if not ready.get('ok'): return {'status': 'blocked', **ready}
            expected = s['first_tap'] if index == 0 else {
                'new_bus': s['prospective_bus'], 'added_entities': s['second_added_entities'],
                'remove': {'unit_number': s['second_remove_unit']}}
            if (not _same_bus_geometry(ready['new_bus'], expected['new_bus'])
                    or {signature(e) for e in ready['added_entities']} != {signature(e) for e in expected['added_entities']}
                    or ready['remove']['unit_number'] != expected['remove']['unit_number']):
                return {'status': 'blocked', 'reason': 'advanced_tap_fresh_preflight_changed_planned_geometry_or_owned_cutover'}
            self.tap.state = ready; self.tap._save()
        result = self.tap.step(obs, params)
        if result.get('status') != 'succeeded': return result
        source = result['evidence']['port']
        if source.get('item') != params['item'] or source.get('name') != params['name'] or source.get('priority') != 'surplus':
            return {'status': 'blocked', 'reason': 'advanced_native_tap_proof_contract_changed'}
        s['tap_sources'][params['item']] = deepcopy(source); s['tap_index'] += 1
        if s['tap_index'] == len(s['parameters']): s['phase'] = 'build'
        self._save()
        return {'status': 'waiting', 'reason': 'advanced_circuit_priority_tap_verified_fresh_observation_required', 'evidence': result['evidence']}

    def step(self, obs):
        if not self.state:
            ready = self.prepare(obs)
            if not ready.get('ok'):
                return {'status': 'blocked', **ready, 'evidence': ready.get('evidence') or {k: v for k, v in ready.items() if k not in ('ok', 'status')}}
            self.state = ready; self._save()
        s = self.state
        if not self._identity(obs, s): return {'status': 'blocked', 'reason': 'advanced_circuit_world_or_catalog_changed'}
        plastic_entry = self.chemical.state.get('products', {}).get('plastic-bar', {})
        if _hash(plastic_entry.get('plan')) != s['plastic_plan_revision']:
            return {'status': 'blocked', 'reason': 'advanced_circuit_bound_plastic_plan_changed'}
        live = next((e for e in obs['entities'] if e['name'] == 'transport-belt' and xy(e) == xy(s['plastic_source'])), None)
        if not live or live.get('unit_number') != s['plastic_source']['unit_number'] or live.get('direction') != s['plastic_source']['facing']:
            return {'status': 'blocked', 'reason': 'advanced_circuit_bound_plastic_outlet_changed'}
        result = self._reserve(obs)
        if result: return result
        if s['phase'] == 'stock_taps':
            for name, count in s['tap_stock'].items():
                if obs.get('inventory', {}).get(name, 0) < count: return self.builder.bootstrap.ensure_item(obs, name, count)
            s['phase'] = 'taps'; self._save()
        if s['phase'] == 'taps': return self._tap_step(obs)
        result = self._bind(obs)
        if result: return result
        result = self.perimeter.ensure_coverage(obs, SCOPE_ID, [s['core']['reserved_area']])
        if result is not None: return result
        if not s.get('construction_authorized'):
            check = preflight(self.game, self.builder, obs, merge_entities(s['plan']['entities'], s['core']['output_access']))
            if not check.get('ok'): return {'status': 'blocked', 'reason': 'advanced_circuit_site_changed_after_defense', 'evidence': check}
            s['construction_authorized'] = True; self._save()
        pipes = surface_pipe_proof(self.game, obs, s['plan'])
        if not pipes.get('ok'): return {'status': 'blocked', 'reason': 'advanced_circuit_preserved_pipe_network_changed', 'evidence': pipes}
        result = self.builder.ensure_plan(obs, s['plan'])
        if result.get('status') != 'succeeded': return result
        proof = self.proof(obs)
        if not proof: return {'status': 'waiting', 'reason': 'waiting_for_normally_supplied_advanced_green_wire_production', 'evidence': self.last_proof_failure}
        s.update(phase='verified', commissioning_proof=proof); self._save()
        return {'status': 'succeeded', 'reason': 'normally supplied dedicated advanced-circuit block commissioned', 'evidence': proof}

    def proof(self, obs):
        self.last_proof_failure = None; s = self.state
        def fail(reason, **evidence):
            self.last_proof_failure = {'reason': reason, **evidence}; return None
        if not s or not self._identity(obs, s) or s.get('phase') in ('stock_taps', 'taps'):
            return fail('advanced_circuit_not_built_or_identity_changed')
        if set(s.get('tap_sources', {})) != set(TAP_NAMES): return fail('advanced_circuit_both_typed_taps_required')
        defense = self.perimeter.coverage_proof(obs, SCOPE_ID)
        if not defense: return fail('advanced_circuit_defense_coverage_missing')
        if not underground_proof(obs, s['plan']): return fail('advanced_circuit_native_underground_pairs_unverified')
        pipes = surface_pipe_proof(self.game, obs, s['plan'])
        if not pipes.get('ok'): return fail('advanced_circuit_preserved_pipe_network_changed', evidence=pipes)
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        for spec in s['plan']['entities']:
            e = actual.get((spec['name'], *xy(spec))); unit = s['bindings'].get(_key(spec))
            if not e or type(unit) is not int or e.get('unit_number') != unit or (spec['name'] != 'small-electric-pole' and e.get('direction') != spec['direction']):
                return fail('advanced_circuit_asset_identity_or_direction_changed', entity=spec)
        plastic = self._plastic_source(obs)
        if not plastic.get('ok') or plastic.get('source') != s['plastic_source'] or plastic.get('plan_revision') != s['plastic_plan_revision']:
            return fail('advanced_circuit_plastic_producer_proof_changed', evidence=plastic)
        try:
            docs = self.tap._documents(); bus = docs['factory']['bus']
            if not all(self._identity(obs, d) for d in docs.values()) or docs['board'].get('shared_main_bus') != bus:
                return fail('advanced_circuit_bus_authority_identity_changed')
            for item, source in s['tap_sources'].items():
                p = next((p for p in bus['ports'] if p.get('name') == TAP_NAMES[item]), None)
                if not p or any(p.get(k) != source.get(k) for k in p): return fail('advanced_circuit_registered_tap_contract_changed', item=item)
        except (OSError, KeyError, TypeError, ValueError): return fail('advanced_circuit_bus_authority_unavailable')
        def bound(e): return dict(e, unit_number=s['bindings'][_key(e)])
        core, audit = s['core'], s['audit']
        lane_checks = [{'position': {'x': xy(m)[0], 'y': xy(m)[1]-3}, 'items': ['copper-cable', 'electronic-circuit']} for m in core['red_machines']]
        lane_checks += [{'position': {'x': xy(m)[0], 'y': xy(m)[1]-3}, 'items': ['copper-plate', 'iron-plate']} for m in core['wire_machines']+[core['green_machine']]]
        args = {'machines': [bound(e) for e in core['machines']], 'arms': [bound(e) for e in audit['arm_targets']],
                'belts': [bound(e) for e in audit['belt_contracts']], 'lane_checks': lane_checks,
                'sources': [dict(source, name='transport-belt') for source in [s['tap_sources'][item] for item in TAP_NAMES]+[s['plastic_source']]],
                'outlet': bound(dict(core['ports'][-1], name='transport-belt')),
                'ingredients': {name: {p['name']: True for p in self.catalog.recipes[name]['ingredients']} for name in ('advanced-circuit', 'electronic-circuit', 'copper-cable')}}
        result = self.game.query(ADVANCED_CIRCUIT_PROOF_LUA.replace('PAYLOAD', _payload(args)))
        if not result.get('ok'): self.last_proof_failure = result; return None
        return {'tick': result['tick'], 'native': result, 'rated': s['rated'], 'defense_coverage': defense,
                'output': dict(core['ports'][-1], name='transport-belt', unit_number=result['outlet_unit'], world_id=obs['world_id'], defense_scope_id=SCOPE_ID),
                'scope': 'native automatic advanced circuits with dedicated wire/green subcells and shared surplus; nameplate is not sustained-throughput certification'}


ADVANCED_ARM_VECTORS_LUA = r'''
for _,name in ipairs({"inserter","fast-inserter","long-handed-inserter"})do
 local p=prototypes.entity[name];local reach=name=="long-handed-inserter"and 2 or 1
 if not p or p.type~="inserter"then return {ok=false,reason="native_advanced_inserter_prototype_missing"}end
 local a,b=p.inserter_pickup_position,p.inserter_drop_position
 if math.floor(.5+(a.x or a[1]))~=0 or math.floor(.5+(a.y or a[2]))~=-reach
 or math.floor(.5+(b.x or b[1]))~=0 or math.floor(.5+(b.y or b[2]))~=reach then
  return {ok=false,reason="unsupported_native_advanced_inserter_vectors",name=name}
 end
end
return {ok=true}
'''


ADVANCED_CIRCUIT_PROOF_LUA = r'''
local args=helpers.json_to_table(PAYLOAD);local finished={};local stock={}
local function exact(q)local e=target(q.position,q.name or"transport-belt")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return nil end;return e end
local function quality(q)return type(q)=="string"and q or(q and q.name)end
local function same_tile(p,q)return math.floor(p.x)+.5==q.x and math.floor(p.y)+.5==q.y end
for _,q in ipairs(args.machines)do local e=exact(q);local r=e and e.get_recipe()
 if not e or e.direction~=q.direction or not r or r.name~=q.recipe or e.products_finished<=0
 or not e.is_connected_to_electric_network()or e.energy<=0 then return {ok=false,reason="advanced_machine_recipe_products_or_power_missing",position=q.position}end
 local input,output=e.get_inventory(defines.inventory.crafter_input),e.get_output_inventory()
 if not input or not output then return {ok=false,reason="advanced_native_machine_inventory_missing"}end
 for i=1,#input do local t=input[i];if t.valid_for_read and(not args.ingredients[q.recipe][t.name]or t.quality.name~="normal")then
  return {ok=false,reason="advanced_machine_input_contaminated",position=q.position}end end
 for i=1,#output do local t=output[i];if t.valid_for_read and(t.name~=q.recipe or t.quality.name~="normal")then
  return {ok=false,reason="advanced_machine_output_contaminated",position=q.position}end end
 finished[#finished+1]={unit_number=e.unit_number,recipe=r.name,products_finished=e.products_finished}
end
for _,q in ipairs(args.arms)do local e=exact(q)
 if not e or e.direction~=q.direction or not e.is_connected_to_electric_network()or e.energy<=0
 or not same_tile(e.pickup_position,q.pickup_position)or not same_tile(e.drop_position,q.drop_position)then
  return {ok=false,reason="advanced_native_inserter_handoff_or_power_changed",position=q.position}end
 local t=e.held_stack;if t and t.valid_for_read then local allow={};for _,name in ipairs(q.allowed_items)do allow[name]=true end
  if not allow[t.name]or t.quality.name~="normal"then return {ok=false,reason="advanced_inserter_held_item_contaminated",position=q.position}end
 end
end
for _,q in ipairs(args.belts)do local e=exact(q);if not e or e.direction~=q.direction then return {ok=false,reason="advanced_core_belt_identity_or_direction_changed"}end
 local allow={};for _,name in ipairs(q.allowed_items)do allow[name]=true end
 for lane=1,e.get_max_transport_line_index()do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if not allow[row.name]or quality(row.quality)~="normal"then return {ok=false,reason="advanced_core_belt_contaminated",position=q.position}end
  stock[row.name]=(stock[row.name]or 0)+row.count
 end end
end
for _,q in ipairs(args.sources)do local e=exact(q)
 if not e or e.direction~=q.facing then return {ok=false,reason="advanced_raw_source_identity_changed",item=q.item}end
 for lane=1,e.get_max_transport_line_index()do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if row.name~=q.item or quality(row.quality)~="normal"then return {ok=false,reason="advanced_raw_source_contaminated",item=q.item}end
 end end
 if q.priority=="surplus"then local p=q.priority_splitter;local split=p and exact(p)
  if not split or split.direction~=0 or split.splitter_input_priority~="left"or split.splitter_output_priority~="left"then return {ok=false,reason="advanced_surplus_priority_changed",item=q.item}end
 elseif q.item~="plastic-bar"then return {ok=false,reason="advanced_raw_tap_surplus_contract_missing"}end
end
local typed={};for _,q in ipairs(args.lane_checks)do local e=target(q.position,"transport-belt")
 if not e or e.force~=f or e.surface~=s or e.direction~=4 then return {ok=false,reason="advanced_native_mixed_feed_missing",position=q.position}end
 for lane=1,2 do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if row.name~=q.items[lane]or quality(row.quality)~="normal"then return {ok=false,reason="advanced_mixed_feed_lane_contaminated",position=q.position}end
  typed[row.name]=(typed[row.name]or 0)+row.count
 end end
end
for _,name in ipairs({"iron-plate","copper-plate","copper-cable","electronic-circuit","plastic-bar"})do
 if not stock[name]or stock[name]<=0 then return {ok=false,reason="advanced_raw_or_component_buffers_empty",item=name}end
end
for _,name in ipairs({"iron-plate","copper-plate","copper-cable","electronic-circuit"})do
 if not typed[name]or typed[name]<=0 then return {ok=false,reason="advanced_native_typed_lane_buffers_empty",item=name}end
end
local outlet=exact(args.outlet);if not outlet then return {ok=false,reason="advanced_output_identity_changed"}end
local count=0;for lane=1,2 do for _,row in pairs(outlet.get_transport_line(lane).get_contents())do
 if row.name~="advanced-circuit"or quality(row.quality)~="normal"then return {ok=false,reason="advanced_output_contaminated"}end;count=count+row.count
end end
if count<=0 then return {ok=false,reason="advanced_output_empty"}end
return {ok=true,tick=game.tick,machines=finished,advanced_circuits=count,outlet_unit=outlet.unit_number,normal_core_items=stock,typed_feed_items=typed}
'''
