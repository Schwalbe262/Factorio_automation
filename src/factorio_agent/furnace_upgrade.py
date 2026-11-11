"""Paid, one-at-a-time stone -> steel furnace replacement in paired rows."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .planning import preflight
from .smelting import FURNACE_NAMES, bank_capacities
from .transport import xy


def validate_upgrade(parameters):
    if (not isinstance(parameters, dict) or set(parameters) not in ({'resource'}, {'resource', 'rate_per_minute'})
            or parameters['resource'] not in ('iron-ore', 'copper-ore')):
        raise ValueError('furnace_upgrade requires resource and optional rate_per_minute')
    rate = parameters.get('rate_per_minute')
    if 'rate_per_minute' in parameters and (type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0):
        raise ValueError('furnace upgrade rate must be finite and positive')


def _signature(plan):
    specs = [e for e in plan['entities'] if e['name'] not in FURNACE_NAMES]
    specs.sort(key=lambda e: (e['name'], *xy(e)))
    return hashlib.sha256(json.dumps({'entities': specs, 'ports': plan['ports']}, sort_keys=True).encode()).hexdigest()


def _revision(plan):
    value = {key: plan.get(key, []) for key in ('entities', 'ports', 'underground_pairs')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _replace(value, positions):
    result = deepcopy(value)
    if isinstance(result, dict):
        if result.get('name') == 'stone-furnace' and result.get('position') and xy(result) in positions:
            result['name'] = 'steel-furnace'
        for key, child in list(result.items()):
            if isinstance(child, (dict, list)):
                result[key] = _replace(child, positions)
    elif isinstance(result, list):
        result = [_replace(child, positions) for child in result]
    return result


def _replan(plan, positions):
    changed = _replace(plan, positions)
    extra = {k: v for k, v in changed.items() if k not in ('ok', 'reason', 'entities', 'ports', 'required_items')}
    return _plan(changed['entities'], changed['ports'], **extra)


class FurnaceUpgrade:
    def __init__(self, game, builder, catalog, smelting, paired, *, outposts=None):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.smelting, self.paired = smelting, paired
        self.outposts = outposts
        self.path = Path(game.cfg.runtime_dir)/'furnace-upgrade.json'
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def _load(self, component):
        path = getattr(component, 'path', None)
        return json.loads(Path(path).read_text(encoding='utf-8')) if path and Path(path).exists() else deepcopy(component.state)

    def _save(self):
        _atomic_json(self.path, self.state)

    def _mining_supply(self, obs, resource):
        path = self.path.parent/'mining-outposts.json'
        mining = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if mining.get('world_id') != obs['world_id']:
            return {'ok': False, 'reason': 'furnace_upgrade_mining_world_changed'}
        banks = [b for b in mining.get('banks', []) if b.get('ready') and b['plan']['resource'] == resource]
        if getattr(self, 'outposts', None) is not None:
            proofs = [self.outposts.bank_proof(obs, deepcopy(b)) for b in banks]
            if any(p is None for p in proofs):
                return {'ok': False, 'reason': 'restore_existing_mining_before_furnace_upgrade'}
            return {'ok': True, 'available': sum(p['nominal_rate_per_minute'] for p in proofs),
                    'basis': 'native remaining mining capacity', 'banks': proofs}
        return {'ok': True, 'available': sum(b['nominal_rate_per_minute'] for b in banks),
                'basis': 'registered installed mining capacity'}

    def _mining_guard(self, obs, resource, requested):
        supply = self._mining_supply(obs, resource)
        if not supply['ok']:
            return supply
        if supply['available'] < requested:
            return {'ok': False, 'reason': 'expand_connected_mining_before_furnace_upgrade',
                    'required_nominal_ore_rate': requested, 'available_nominal_ore_rate': supply['available'],
                    'capacity_basis': supply['basis']}
        return None

    def prepare(self, obs, parameters):
        validate_upgrade(parameters)
        if not obs.get('technologies', {}).get('advanced-material-processing') or not obs.get('enabled_recipes', {}).get('steel-furnace'):
            return {'ok': False, 'reason': 'native_advanced_material_processing_required'}
        source, paired = self._load(self.smelting), self._load(self.paired)
        if any(s.get('world_id') != obs['world_id'] or s.get('catalog_fingerprint') != self.catalog.fingerprint for s in (source, paired)):
            return {'ok': False, 'reason': 'furnace_upgrade_source_world_or_catalog_changed'}
        if not source.get('paired') or paired.get('phase') != 'verified':
            return {'ok': False, 'reason': 'commissioned_paired_smelting_required'}
        bank = next((b for b in source['banks'] if b['resource'] == parameters['resource']), None)
        if not bank:
            return {'ok': False, 'reason': 'requested_smelting_bank_missing'}
        furnaces = [e for e in bank['entities'] if e['name'] in FURNACE_NAMES]
        if not furnaces or any(e['name'] not in ('stone-furnace', 'steel-furnace') for e in furnaces):
            return {'ok': False, 'reason': 'upgrade_requires_existing_two_by_two_burner_furnaces'}
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        for spec in source['plan']['entities']:
            live = actual.get((spec['name'], *xy(spec)))
            if not live or live.get('direction', 0) != spec.get('direction', 0):
                return {'ok': False, 'reason': 'repair_source_before_furnace_upgrade', 'entity': spec['name'], 'position': spec['position']}
        targets = []
        for spec in furnaces:
            if spec['name'] != 'stone-furnace':
                continue
            live = actual[(spec['name'], *xy(spec))]
            if type(live.get('unit_number')) is not int:
                return {'ok': False, 'reason': 'upgrade_source_unit_identity_missing'}
            replacement = dict(spec, name='steel-furnace')
            if self.builder._occupied_by_plan([spec]) != self.builder._occupied_by_plan([replacement]):
                return {'ok': False, 'reason': 'steel_furnace_changes_owned_footprint'}
            targets.append({k: live[k] for k in ('name', 'position', 'direction', 'unit_number')})
        targets.sort(key=lambda e: xy(e))
        all_positions = {xy(e) for e in targets}
        maximum = bank_capacities(self.catalog, [_replace(bank, all_positions)])[bank['resource']]
        requested = parameters.get('rate_per_minute', maximum)
        if requested > maximum+1e-9:
            return {'ok': False, 'reason': 'requested_capacity_exceeds_existing_bank_steel_slots', 'maximum_nominal_rate': maximum}
        selected, positions = [], set()
        target_rate = bank_capacities(self.catalog, [bank])[bank['resource']]
        for target in targets:
            if target_rate+1e-9 >= requested:
                break
            selected.append(target); positions.add(xy(target))
            target_rate = bank_capacities(self.catalog, [_replace(bank, positions)])[bank['resource']]
        targets = selected
        final_plan = _replan(source['plan'], positions)
        mining_error = self._mining_guard(obs, bank['resource'], requested)
        if mining_error:
            return mining_error
        check = preflight(self.game, self.builder, obs, final_plan['entities'], ignored_units=[e['unit_number'] for e in targets])
        if not check.get('ok'):
            return {'ok': False, 'reason': 'whole_upgraded_smelting_preflight_failed', 'preflight': check}
        return {'ok': True, 'world_id': obs['world_id'], 'catalog_fingerprint': self.catalog.fingerprint,
                'resource': bank['resource'], 'phase': 'upgrade', 'targets': targets, 'completed': [],
                'infrastructure_signature': _signature(source['plan']), 'target_nominal_rate': target_rate,
                'source_revision': _revision(source['plan']), 'paired_revision': _revision(paired['plan']),
                'requested_rate': requested, 'requested_parameter': parameters.get('rate_per_minute'),
                'prepared_tick': obs['tick']}

    def _publish(self, position):
        source = self._load(self.smelting)
        paired = self._load(self.paired)
        if any(s.get('world_id') != self.state['world_id'] or s.get('catalog_fingerprint') != self.catalog.fingerprint for s in (source, paired)):
            return {'status': 'blocked', 'reason': 'furnace_upgrade_source_world_or_catalog_changed'}
        if _signature(source['plan']) != self.state['infrastructure_signature']:
            return {'status': 'blocked', 'reason': 'smelting_ports_or_infrastructure_changed_during_upgrade'}
        board_path = self.path.parent/'pcb-board.json'
        board = json.loads(board_path.read_text(encoding='utf-8'))
        consumers = {}
        for filename in ('bus-factory.json', 'logistic-science.json'):
            path = self.path.parent/filename
            if path.exists():
                consumers[filename] = json.loads(path.read_text(encoding='utf-8'))
        documents = {'pcb-board.json': board, **consumers}
        if any(doc.get('world_id') != self.state['world_id']
               or doc.get('catalog_fingerprint') != self.catalog.fingerprint for doc in documents.values()):
            return {'status': 'blocked', 'reason': 'furnace_upgrade_publication_document_identity_changed'}
        try:
            copies = {'smelting': source['plan'], 'board': board['blocks']['smelting_district'],
                      **{name: doc['source_plan'] for name, doc in consumers.items()}}
            revisions = {name: _revision(plan) for name, plan in copies.items()}
            paired_revision = _revision(paired['plan'])
        except (KeyError, TypeError, AttributeError):
            return {'status': 'blocked', 'reason': 'furnace_upgrade_publication_source_plan_missing'}
        positions = {position}
        next_plan = _replan(source['plan'], positions)
        next_revision = _revision(next_plan)
        intent = self.state.get('publication')
        if intent:
            if (intent['position'] != list(position) or set(consumers) != set(intent['consumer_files'])
                    or any(revision not in (intent['old_revision'], intent['new_revision']) for revision in revisions.values())
                    or paired_revision not in (intent['paired_old_revision'], intent['new_revision'])
                    or next_revision != intent['new_revision']):
                return {'status': 'blocked', 'reason': 'furnace_upgrade_publication_revision_changed'}
        else:
            expected = self.state.get('source_revision')
            if (not expected or any(revision != expected for revision in revisions.values())
                    or paired_revision != self.state.get('paired_revision')):
                return {'status': 'blocked', 'reason': 'furnace_upgrade_publication_revision_changed'}
            if next_revision == expected and paired_revision == expected:
                return None
            intent = {'position': list(position), 'old_revision': expected, 'new_revision': next_revision,
                      'paired_old_revision': paired_revision, 'consumer_files': sorted(consumers)}
            self.state['publication'] = intent
            self._save()
        source['plan'] = next_plan
        updated = []
        for bank in source['banks']:
            changed = _replan(bank, positions)
            if any(xy(e) == position for e in changed['entities'] if e['name'] in FURNACE_NAMES):
                changed['furnace_name'] = 'steel-furnace'
            updated.append(changed)
        source['banks'] = updated
        source['nominal_smelting_capacity_per_minute'] = bank_capacities(self.catalog, updated)
        paired.update(plan=deepcopy(source['plan']), banks=deepcopy(updated), phase='verified',
                      nominal_smelting_capacity_per_minute=deepcopy(source['nominal_smelting_capacity_per_minute']))
        board['blocks']['smelting_district'] = _replan(copies['board'], positions)
        for doc in consumers.values():
            doc['source_plan'] = _replan(doc['source_plan'], positions)
        _atomic_json(self.smelting.path, source)
        _atomic_json(self.paired.path, paired)
        _atomic_json(board_path, board)
        for filename, doc in consumers.items():
            _atomic_json(self.path.parent/filename, doc)
        self.smelting.state, self.paired.state = source, paired
        self.state['source_revision'] = next_revision
        self.state['paired_revision'] = next_revision
        self.state.pop('publication', None)
        self._save()
        return None

    def step(self, obs, parameters):
        validate_upgrade(parameters)
        if self.state and (self.state['world_id'] != obs['world_id'] or self.state['catalog_fingerprint'] != self.catalog.fingerprint):
            return {'status': 'blocked', 'reason': 'furnace_upgrade_world_or_catalog_changed'}
        if self.state and (self.state['resource'] != parameters['resource']
                           or self.state.get('requested_parameter') != parameters.get('rate_per_minute')):
            if self.state['phase'] != 'verified':
                return {'status': 'blocked', 'reason': 'another_furnace_bank_upgrade_is_in_progress'}
            self.state = {}
        if not self.state:
            prepared = self.prepare(obs, parameters)
            if not prepared.get('ok'):
                return {'status': 'blocked', **prepared}
            self.state = prepared
            self._save()
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        pending = self.state.get('pending')
        if pending:
            result = self._publish(xy(pending))
            if result:
                return result
            old = actual.get(('stone-furnace', *xy(pending)))
            new = actual.get(('steel-furnace', *xy(pending)))
            if old:
                if old['unit_number'] != pending['unit_number'] or old.get('direction', 0) != pending['direction']:
                    return {'status': 'blocked', 'reason': 'old_furnace_identity_changed_never_retarget_replacement'}
                if not self._normal_stock():
                    return self._acquire_replacement(obs)
                return self._mine(pending, obs['world_id'])
            if new:
                if new.get('direction', 0) != pending['direction'] or (pending.get('replacement_unit') and pending['replacement_unit'] != new['unit_number']):
                    return {'status': 'blocked', 'reason': 'steel_furnace_replacement_identity_changed'}
                if not pending.get('replacement_unit'):
                    pending['replacement_unit'] = new['unit_number']; self._save()
                if new.get('products_finished', 0) < 1:
                    return {'status': 'waiting', 'reason': 'waiting for normally belt-fed replacement furnace production'}
                self.state['completed'].append(dict(pending, replacement_unit=new['unit_number']))
                self.state.pop('pending'); self._save()
            else:
                if not self._normal_stock():
                    return self._acquire_replacement(obs)
                replacement = {k: pending[k] for k in ('position', 'direction')}
                replacement.update(name='steel-furnace')
                check = self.builder.can_place([replacement])
                if not check.get('ok'):
                    return {'status': 'blocked', 'reason': 'steel_furnace_replacement_footprint_obstructed', 'preflight': check}
                return dict(replacement, type='build', item='steel-furnace', reason='normally build the prepaid replacement in the freshly observed owned gap')
        done = {xy(e) for e in self.state['completed']}
        target = next((e for e in self.state['targets'] if xy(e) not in done), None)
        if target:
            # Finish a committed replacement above; validate remaining supply
            # again before starting another physical replacement transaction.
            mining_error = self._mining_guard(obs, self.state['resource'], self.state['requested_rate'])
            if mining_error:
                return {'status': 'blocked', **mining_error}
            current = actual.get(('stone-furnace', *xy(target)))
            if not current or current['unit_number'] != target['unit_number'] or current.get('direction', 0) != target['direction']:
                return {'status': 'blocked', 'reason': 'unstarted_upgrade_source_identity_changed'}
            if not self._normal_stock():
                return self._acquire_replacement(obs)
            self.state['pending'] = deepcopy(target)
            self._save()
            result = self._publish(xy(target))
            return result or self._mine(target, obs['world_id'])
        self.smelting.state = self._load(self.smelting)
        self.paired.state = self._load(self.paired)
        result = self.builder.ensure_plan(obs, self.smelting.state['plan'])
        if result.get('status') != 'succeeded':
            return result
        proof = self.proof(obs)
        if not proof:
            return {'status': 'waiting', 'reason': 'waiting for every native furnace, automatic fuel/feed and both producer lanes'}
        self.state['phase'] = 'verified'; self._save()
        return {'status': 'succeeded', 'reason': 'paired furnace bank meets requested catalog capacity after paid replacement', 'evidence': proof}

    def _normal_stock(self):
        result = self.game.query('''
if not a or not a.valid then return {count=0} end
return {count=a.get_main_inventory().get_item_count{name="steel-furnace",quality="normal"}}
''')
        return result.get('count', 0) >= 1

    def _acquire_replacement(self, obs):
        normal = dict(obs, inventory=dict(obs.get('inventory', {})))
        normal['inventory']['steel-furnace'] = 0
        return self.builder.bootstrap.ensure_item(normal, 'steel-furnace', 1)

    @staticmethod
    def _mine(target, world):
        return {'type': 'mine', 'name': 'stone-furnace', 'position': target['position'], 'count': 1,
                'expected_entity_unit': target['unit_number'], 'expected_entity_world_id': world,
                'reason': 'normally recover one exact owned stone furnace after stocking its paid steel replacement'}

    def proof(self, obs):
        if not self.state or self.state['world_id'] != obs['world_id'] or self.state.get('pending'):
            return None
        self.smelting.state = self._load(self.smelting)
        self.paired.state = self._load(self.paired)
        bank = next((b for b in self.smelting.state['banks'] if b['resource'] == self.state['resource']), None)
        if not bank:
            return None
        specs = [e for e in bank['entities'] if e['name'] in FURNACE_NAMES]
        selected = {xy(e) for e in self.state['targets']}
        if not specs or any(e['name'] != 'steel-furnace' for e in specs if xy(e) in selected):
            return None
        actual = {(e['name'], *xy(e)): e for e in obs['entities']}
        live = [actual.get((e['name'], *xy(e))) for e in specs]
        if any(not e or e.get('products_finished', 0) < 1 for e in live):
            return None
        capacity = bank_capacities(self.catalog, [bank])[bank['resource']]
        if capacity+1e-9 < self.state['requested_rate']:
            return None
        smelting, paired = self.smelting.proof(obs), self.paired.proof(obs)
        if not smelting or not paired:
            return None
        return {'tick': obs['tick'], 'resource': bank['resource'], 'steel_furnace_units': [e['unit_number'] for e in live if e['name'] == 'steel-furnace'],
                'nominal_capacity_per_minute': capacity, 'requested_rate_per_minute': self.state['requested_rate'],
                'smelting': smelting, 'paired_producer_lanes': paired,
                'scope': 'native paid furnace production with preserved ports; catalog capacity is not measured sustained throughput'}
