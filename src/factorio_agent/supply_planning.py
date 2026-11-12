"""Read-only, bounded bootstrap supply advice; never creates jobs or effects."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path

from .construction_supply import V2Bootstrap
from .resource_policy import resource_decision


RESOURCES = {'iron-plate': 'iron-ore', 'copper-plate': 'copper-ore', 'coal': 'coal', 'stone': 'stone'}
FLOORS = {'iron-plate': 32, 'copper-plate': 16, 'coal': 16, 'stone': 10}
LEAVES = frozenset((*FLOORS, 'wood'))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _position_key(e):
    p = e.get('position', {})
    return e.get('name'), p.get('x'), p.get('y')


def _read(root, name):
    path = root / name
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def _pending(root, world):
    jobs = _read(root, 'jobs.json'); queue = _read(root, 'work_queue.json')
    if jobs and jobs.get('world_id') != world or queue and queue.get('world_id') != world:
        return {}, True
    result = {}; unknown = False
    rows = list(jobs.get('jobs', {}).values()) + list(queue.get('nodes', []))
    for row in rows:
        if row.get('world_id', world) != world: continue
        if 'pending_action' in row or 'outcome_unknown' in str(row.get('reason', '')): unknown = True
        status = row.get('status')
        if status in ('completed', 'succeeded', 'cancelled', 'failed'): continue
        if row.get('capability') in ('starter_capacity', 'mining_outpost', 'paired_mining'):
            resource = row.get('parameters', {}).get('resource')
            if resource in RESOURCES.values(): result[resource] = status
    return result, unknown


def _cell_cost(catalog, enabled, item):
    """Exact current solid recipe BOM, plus the finite eight-coal seed budget."""
    result = Counter(coal=8)
    def expand(name, count, stack=()):
        if name in LEAVES: result[name] += count; return
        if name in stack: raise ValueError('cyclic current cell recipe')
        recipe = catalog.recipe_for_product(name)
        if not recipe or not enabled.get(recipe['name']): raise ValueError('current cell recipe unavailable')
        products = recipe.get('products', [])
        if (len(products) != 1 or products[0].get('name') != name or products[0].get('probability', 1) != 1
                or not _number(products[0].get('amount')) or products[0]['amount'] <= 0):
            raise ValueError('deterministic current cell recipe unavailable')
        crafts = math.ceil(count / products[0]['amount'])
        for row in recipe['ingredients']:
            if row.get('type', 'item') != 'item' or not _number(row.get('amount')):
                raise ValueError('current cell requires unsupported material')
            expand(row['name'], crafts * row['amount'], (*stack, name))
    expand('burner-mining-drill', 1)
    expand('stone-furnace' if item.endswith('-plate') else 'wooden-chest', 1)
    return dict(result)


def _starter_capacity(catalog, item, resource):
    """Conservative *burner* base capacity, never an electric-cell average."""
    drill = catalog.entities['burner-mining-drill']; ore = catalog.entities[resource]
    speed, seconds = drill['mining_speed'], ore['mining_time']
    product = next(p for p in ore['mining_products'] if p['name'] == resource)
    amount = product['amount']
    if not all(_number(v) and v > 0 for v in (speed, seconds, amount)):
        raise ValueError('native starter cell capacity unknown')
    rate = 60 * speed * amount / seconds
    if item.endswith('-plate'):
        recipe = catalog.recipe_for_product(item)
        inputs, outputs = recipe['ingredients'], recipe['products']
        if len(inputs) != 1 or inputs[0]['name'] != resource or len(outputs) != 1 or outputs[0]['name'] != item:
            raise ValueError('native plate conversion unsupported')
        needed, made = inputs[0]['amount'], outputs[0]['amount']
        furnace_speed = catalog.entities['stone-furnace']['crafting_speed']
        if not all(_number(v) and v > 0 for v in (needed, made, furnace_speed, recipe['energy'])):
            raise ValueError('native plate cell capacity unknown')
        rate = min(rate * made / needed, 60 * furnace_speed * made / recipe['energy'])
    return rate


def supply_options(root, game, obs, catalog, budget, rates):
    """Four keyed semantic options from four existing-cell native read surveys.

    ``budget`` accepts diagnostics.base_material_upper_bound (or ``base``).
    ``rates`` is diagnostics.production_rates: measured status, window and
    rates_per_game_minute. Missing/warmup rates remain unknown. The cost passed
    to the balance model is this item's literal raw spend. Every other raw cost
    must already be funded independently; no cross-resource exchange is invented.
    All outputs are bounded summaries without positions, raw logs or job keys.
    """
    root = Path(root)
    if not obs.get('ok', True) or not obs.get('world_id') or getattr(game, 'expected_world', None) != obs['world_id']:
        return {item: {'action': 'wait', 'reason': 'supply_observation_world_unbound'} for item in RESOURCES}
    bootstrap = V2Bootstrap(game, catalog)
    pending, unknown = _pending(root, obs['world_id'])
    measured = (isinstance(rates, dict) and rates.get('status') == 'measured'
                and _number(rates.get('window_game_seconds')) and rates['window_game_seconds'] >= 5)
    samples = rates.get('rates_per_game_minute', {}) if measured else {}
    samples = samples if isinstance(samples, dict) else {}
    base = budget.get('base_material_upper_bound', budget.get('base', {})) if isinstance(budget, dict) else {}
    budget_known = (isinstance(budget, dict) and isinstance(base, dict) and all(_number(n) for n in base.values())
                    and not (budget or {}).get('unresolved_materials'))
    base = base if budget_known else {}
    cells = {}; surveys_ok = {}
    for item, resource in RESOURCES.items():
        report = bootstrap._existing_cells(resource)
        surveys_ok[item] = report.get('ok') is True
        cells[item] = [c for c in report.get('cells', []) if c.get('remaining', 0) > 0
                       and (c.get('receiver') or c.get('direct_belt'))] if report.get('ok') else []
    available = {}; held = {}; output_full = {}; stock_entities = {}
    for item in RESOURCES:
        filtered = bootstrap._unreserved_stock_observation(obs, item)
        held[item] = obs.get('inventory', {}).get(item, 0)
        held[item] = held[item] if _number(held[item]) else 0
        receivers = {_position_key(c['receiver']) for c in cells[item] if c.get('receiver')}
        drills = {_position_key(c['drill']) for c in cells[item]}
        stock = 0; full = False; seen = set(); stock_entities[item] = filtered['entities']
        full = any(_position_key(e) in drills and e.get('status_name') in
                   ('full_output', 'waiting_for_space', 'waiting_for_space_in_destination') for e in obs.get('entities', []))
        for e in filtered['entities']:
            unit = e.get('unit_number')
            if type(unit) is not int or unit in seen: continue
            seen.add(unit)
            relevant=_position_key(e)in receivers or e.get('recipe')==item
            if e.get('name') in ('stone-furnace', 'steel-furnace', 'electric-furnace'):
                count = e.get('output_inventory', {}).get(item, 0)
            elif _position_key(e) in receivers and e.get('name') in ('wooden-chest', 'iron-chest', 'steel-chest'):
                count = e.get('inventory', {}).get(item, 0)
            else: continue
            if _number(count): stock += count
            relevant=relevant or count>0
            full |= relevant and e.get('status_name') in ('full_output', 'waiting_for_space', 'waiting_for_space_in_destination')
        available[item] = held[item] + stock; output_full[item] = full
    available['wood'] = obs.get('inventory', {}).get('wood', 0)
    options = {}
    for item, resource in RESOURCES.items():
        row = samples.get(item, {}); produced = row.get('produced'); consumed = row.get('consumed')
        produced = produced if _number(produced) else None; consumed = consumed if _number(consumed) else None
        bill = (base.get(item, 0) if budget_known else 0) + FLOORS[item] + (consumed or 0)
        complete = sum(bool(c.get('receiver')) for c in cells[item])
        productive = sum(bool(c.get('operating') if c.get('electric') else c.get('burning') or c.get('fuel', 0) > 0) for c in cells[item])
        stage = {'held': held[item], 'output_full': output_full[item]}
        # Ore already waiting in furnace inputs proves a finite smelting bill can
        # be funded; coal in those fuel inventories is never collectable stock.
        if item.endswith('-plate'):
            ore_stock = sum(e.get('inventory', {}).get(resource, 0) for e in stock_entities[item]
                            if e.get('name') in ('stone-furnace', 'steel-furnace', 'electric-furnace'))
            if _number(ore_stock): stage['ore_stock'] = ore_stock
            ore_rate = samples.get(resource, {}).get('produced')
            if _number(ore_rate): stage['ore_per_minute'] = ore_rate
        raw = {}; equivalent = None; cost_reason = None
        try:
            raw = _cell_cost(catalog, obs.get('enabled_recipes', {}), item)
            equivalent = raw.get(item, 0)
            capacity = _starter_capacity(catalog, item, resource)
            if produced is not None and productive:
                stage['cell_capacity_per_minute'] = min(capacity, produced / productive)
        except (ValueError, KeyError, TypeError, StopIteration): cost_reason = 'current_cell_recipe_cost_unknown'
        if cost_reason: equivalent = None
        # A known-stock decision needs no economic rate; a missing cost must
        # nevertheless never accidentally turn a shortage into a free expansion.
        decision = resource_decision(item, available[item], bill, produced if equivalent is not None else None, consumed,
                                     max(productive, 1), equivalent or 0, stage=stage)
        if equivalent is None and decision['action'] == 'wait' and cost_reason:
            decision['reason'] = cost_reason
        decision['evidence'].update(held=held[item], collectable=available[item]-held[item],
            bill_base=base.get(item, 0), bootstrap_floor=FLOORS[item], consumption_headroom=consumed,
            productive_cells=productive, installed_live_cells=len(cells[item]), complete_receiver_cells=complete,
            raw_cost_per_cell=raw, target_item_cost_per_cell=equivalent, measurement_status='measured' if measured else 'unknown',
            cost_model='literal target-item spend; all other raw inputs must be independently prefunded',
            scope='finite current bill and observed stocks/rates; not a guaranteed sustained source rate')
        if equivalent is None:
            net = produced-consumed if produced is not None and consumed is not None else None
            decision['evidence'].update(produced_per_minute=produced,net_per_minute=net,
                extra_cell_raw_cost=None,wait_seconds=60*max(0,bill-available[item])/net if net is not None and net>0 else None)
        decision.update(capability=None, parameters=None)
        if decision['action'] == 'expand_supply':
            if not surveys_ok[item] or not budget_known or equivalent is None or productive == 0 or unknown or resource in pending:
                decision.update(action='wait', reason=('existing_capacity_task' if resource in pending else
                    'unresolved_world_effect' if unknown else cost_reason or 'supply_measurement_or_live_cell_incomplete'))
            else:
                funded = [q for q in decision['evidence']['alternatives'] if (q['save_seconds'] is None or q['save_seconds'] > 0)
                          and complete+q['extra_cells']<=8
                          and all(_number(available.get(m)) and available[m] >= n*q['extra_cells'] for m,n in raw.items())]
                if not funded: decision.update(action='wait', reason='expansion_raw_materials_not_funded')
                elif item.endswith('-plate') and obs.get('enabled_recipes', {}).get('electric-mining-drill'):
                    decision.update(action='wait', reason='prefer_powered_mining_outpost_and_smelting_after_native_unlock',
                                    preferred_capability='mining_outpost', planning_required=True)
                else:
                    best = min(funded, key=lambda q: (q['projected_wait_seconds'], q['extra_cells']))
                    target = complete + best['extra_cells']
                    if target > 8: decision.update(action='wait', reason='starter_capacity_absolute_limit_reached')
                    else:
                        decision['evidence'].update(best, target_cells=target)
                        decision.update(capability='starter_capacity', parameters={'resource':resource,'cells':target})
        if decision['action'] != 'expand_supply':
            decision['evidence'].update(extra_cells=0, target_cells=complete)
        if not budget_known:
            decision.update(action='wait',reason='current_construction_budget_incomplete',capability=None,parameters=None)
        options[item] = decision
    return options
