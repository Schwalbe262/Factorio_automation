"""Exact declarations for ordinary downstream construction at fixed fluid ports.

Callers own writer.lock. These functions change registry metadata only; they
never build pipes or alter fluid contents.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re

from factorio_ai.deterministic_state import _atomic_json
from .transport import VECTORS


def _descriptor(neighbor, source, *, bound=False):
    keys = {'name', 'position', 'direction', 'fluidbox_index'}
    if bound: keys.add('unit_number')
    if not isinstance(neighbor, dict) or set(neighbor) != keys:
        raise ValueError('fluid handoff needs an exact first-neighbor descriptor')
    p = neighbor.get('position'); facing = source.get('facing')
    if (neighbor.get('name') not in ('pipe', 'pipe-to-ground')
            or type(neighbor.get('fluidbox_index')) is not int or neighbor['fluidbox_index'] != 1
            or type(facing) is not int or facing not in VECTORS
            or type(neighbor.get('direction')) is not int
            or not isinstance(p, dict) or set(p) != {'x', 'y'}
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in p.values())):
        raise ValueError('fluid handoff supports a cardinal normal pipe connection')
    dx, dy = VECTORS[facing]
    if p != {'x': source['position']['x']+dx, 'y': source['position']['y']+dy}:
        raise ValueError('fluid handoff must leave the fixed source in its declared facing')
    expected_direction = 0 if neighbor['name'] == 'pipe' else (facing+8) % 16
    if neighbor['direction'] != expected_direction:
        raise ValueError('fluid handoff neighbor must expose its surface connection to the source')
    if bound and (type(neighbor['unit_number']) is not int or neighbor['unit_number'] < 1):
        raise ValueError('binding requires the consumer layout exact saved unit')
    return deepcopy(neighbor)


def _context(game, catalog, obs, consumer_id, fluid, source, neighbor, *, bound=False):
    if (not isinstance(consumer_id, str) or not re.fullmatch(r'[a-z][a-z0-9:_-]{0,79}', consumer_id)
            or not isinstance(fluid, str) or not fluid
            or not obs.get('ok', True) or not obs.get('world_id')
            or getattr(game, 'expected_world', None) != obs['world_id']):
        raise ValueError('fluid handoff requires an explicit consumer and bound world')
    path = Path(game.cfg.runtime_dir)/'fluid-sources.json'
    registry = json.loads(path.read_text(encoding='utf-8'))
    if (registry.get('world_id') != obs['world_id'] or registry.get('catalog_fingerprint') != catalog.fingerprint
            or not isinstance(source,dict) or not isinstance(registry.get('sources'),dict)
            or source not in registry['sources'].values()
            or source.get('kind') != 'fluid' or source.get('item') != fluid
            or source.get('direction') != 'output' or source.get('name') != 'pipe'
            or source.get('fluidbox_index') != 1 or source.get('world_id') != obs['world_id']
            or type(source.get('unit_number')) is not int or source['unit_number'] < 1):
        raise ValueError('fluid handoff must preserve the exact published source')
    return path, registry, _descriptor(neighbor, source, bound=bound)


def _existing(registry, consumer_id, fluid):
    handoffs = registry.get('handoffs', {})
    if not isinstance(handoffs, dict) or not isinstance(handoffs.get(consumer_id, {}), dict):
        raise ValueError('invalid fluid handoff registry')
    return handoffs.get(consumer_id, {}).get(fluid)


def _rows(registry):
    handoffs = registry.get('handoffs', {})
    if not isinstance(handoffs, dict): raise ValueError('invalid fluid handoff registry')
    for owner, entries in handoffs.items():
        if not isinstance(entries, dict) or any(not isinstance(row, dict) for row in entries.values()):
            raise ValueError('invalid fluid handoff consumer records')
        for row in entries.values(): yield owner, row


def _record(catalog, obs, source, neighbor, phase):
    return dict(world_id=obs['world_id'], catalog_fingerprint=catalog.fingerprint,
                source=deepcopy(source), first_neighbor=deepcopy(neighbor), phase=phase)


def declare_handoff(game, catalog, obs, consumer_id, fluid, source, first_neighbor):
    """Reserve a fixed external edge before returning any consumer build."""
    path, registry, neighbor = _context(game, catalog, obs, consumer_id, fluid, source, first_neighbor)
    proposed = _record(catalog, obs, source, neighbor, 'declared')
    old = _existing(registry, consumer_id, fluid)
    if old:
        plain = deepcopy(old); plain.get('first_neighbor', {}).pop('unit_number', None); plain['phase'] = 'declared'
        if plain != proposed or old.get('phase') not in ('declared', 'bound'):
            return {'ok': False, 'reason': 'fluid_handoff_declaration_changed'}
        return {'ok': True, 'handoff': old}
    for owner, row in _rows(registry):
        if row.get('source') == source:
            return {'ok': False, 'reason': 'fluid_output_edge_already_reserved', 'consumer_id': owner}
    payload = json.dumps(json.dumps({'source': source, 'neighbor': neighbor, 'fluid': fluid}))
    native = game.query('local q=helpers.json_to_table('+payload+');'+DECLARE_LUA)
    if not native.get('ok'): return native
    registry.setdefault('handoffs', {}).setdefault(consumer_id, {})[fluid] = proposed
    _atomic_json(path, registry)
    return {'ok': True, 'handoff': proposed}


def bind_handoff(game, catalog, obs, consumer_id, fluid, source, first_neighbor):
    """Bind only the consumer's saved unit after reciprocal native connection."""
    path, registry, neighbor = _context(game, catalog, obs, consumer_id, fluid, source, first_neighbor, bound=True)
    old = _existing(registry, consumer_id, fluid)
    proposed = _record(catalog, obs, source, neighbor, 'bound')
    plain = deepcopy(proposed); plain['phase'] = 'declared'; plain['first_neighbor'].pop('unit_number')
    if old not in (plain, proposed): return {'ok': False, 'reason': 'matching_fluid_handoff_declaration_required'}
    payload = json.dumps(json.dumps({'source': source, 'neighbor': neighbor, 'fluid': fluid}))
    native = game.query('local q=helpers.json_to_table('+payload+');'+BIND_LUA)
    if not native.get('ok'): return native
    if old != proposed:
        registry['handoffs'][consumer_id][fluid] = proposed
        _atomic_json(path, registry)
    return {'ok': True, 'handoff': proposed}


def bound_handoffs(registry, obs, catalog, source):
    """Return only valid declarations for this exact source; inspect peers later."""
    result = []
    for _, row in _rows(registry):
        if row.get('source') != source: continue
        if row.get('world_id') != obs['world_id'] or row.get('catalog_fingerprint') != catalog.fingerprint:
            raise ValueError('fluid handoff identity changed')
        if row.get('phase') == 'declared':
            _descriptor(row.get('first_neighbor'), source)
        elif row.get('phase') == 'bound':
            _descriptor(row.get('first_neighbor'), source, bound=True); result.append(deepcopy(row))
        else: raise ValueError('unknown fluid handoff phase')
    if len(result) > 1: raise ValueError('fixed fluid source has multiple external neighbors')
    return result


SOURCE_LUA = r'''
local src=target(q.source.position,q.source.name)
if not src or src.force~=f or src.surface~=s or src.unit_number~=q.source.unit_number
 or src.position.x~=q.source.position.x or src.position.y~=q.source.position.y then return {ok=false,reason="handoff_source_identity_changed"}end
for name in pairs(src.get_fluid_contents())do if name~=q.fluid then return {ok=false,reason="handoff_source_fluid_contaminated"}end end
'''
DECLARE_LUA = SOURCE_LUA + r'''
if target(q.neighbor.position,q.neighbor.name)then return {ok=false,reason="handoff_neighbor_exists_before_declaration"}end
return {ok=true}
'''
BIND_LUA = SOURCE_LUA + r'''
local e=target(q.neighbor.position,q.neighbor.name)
if not e or e.force~=f or e.surface~=s or e.unit_number~=q.neighbor.unit_number
 or e.position.x~=q.neighbor.position.x or e.position.y~=q.neighbor.position.y
 or(q.neighbor.name=="pipe-to-ground"and e.direction~=q.neighbor.direction)then return {ok=false,reason="handoff_neighbor_identity_changed"}end
for name in pairs(e.get_fluid_contents())do if name~=q.fluid then return {ok=false,reason="handoff_neighbor_fluid_contaminated"}end end
local forward=false;local reverse=false
for _,p in pairs(src.get_fluid_box_neighbours(1)or{})do if p.entity==e and p.index==1 then forward=true end end
for _,p in pairs(e.get_fluid_box_neighbours(1)or{})do if p.entity==src and p.index==1 then reverse=true end end
return {ok=forward and reverse,reason=not(forward and reverse)and"handoff_native_reciprocal_edge_missing"or nil}
'''
