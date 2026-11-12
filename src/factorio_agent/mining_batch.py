"""One-query assisted recovery of at most eight exact owned entities."""
from __future__ import annotations

import json
import math

from factorio_agent.core.deterministic_game import GUARDED_MINE_LUA, validate_mine_guard

# Advanced replacement guards are forbidden by this envelope. Reuse the exact
# base owned-world/unit/force prefix without unrelated replacement programs.
OWNED_MINE_GUARD_LUA = GUARDED_MINE_LUA.split('\nif x.lab_replacement then', 1)[0]

def validate_mine_many(action, *, backend='assisted', expected_world=None):
    if backend != 'assisted':
        raise ValueError('guarded mine batches require assisted backend')
    rows = action.get('actions')
    if (set(action) - {'type', 'actions', 'reason'} or action.get('type') != 'mine_many'
            or not isinstance(rows, list) or not 1 <= len(rows) <= 8
            or ('reason' in action and not isinstance(action['reason'], str))):
        raise ValueError('mine_many requires one to eight guarded ordinary mines')
    units, positions, worlds = set(), set(), set()
    for row in rows:
        if (not isinstance(row, dict)
                or set(row) - {'type', 'name', 'position', 'count', 'expected_entity_unit', 'expected_entity_world_id', 'reason'}
                or row.get('type') != 'mine' or not isinstance(row.get('name'), str) or not row['name']
                or not {'expected_entity_unit', 'expected_entity_world_id'}.issubset(row)
                or ('reason' in row and not isinstance(row['reason'], str))):
            raise ValueError('mine_many accepts only identity-guarded owned entity recovery')
        p = row.get('position')
        if (not isinstance(p, dict) or set(p) != {'x', 'y'}
                or any(type(p[k]) not in (int, float) or not math.isfinite(p[k]) for k in ('x', 'y'))):
            raise ValueError('mine_many requires finite positions')
        validate_mine_guard(row)
        position = p['x'], p['y']
        if row['expected_entity_unit'] in units or position in positions:
            raise ValueError('mine_many requires distinct unit identities and positions')
        units.add(row['expected_entity_unit']); positions.add(position)
        worlds.add(row['expected_entity_world_id'])
    if len(worlds) != 1 or (expected_world is not None and expected_world not in worlds):
        raise ValueError('mine_many must target the bound world only')


MINE_MANY_LUA = r'''
if not a or not a.valid or a.force~=f or a.surface~=s then return {ok=false,status="blocked",reason="agent_dead_or_force_changed",completed=0,results={}} end
local inv=a.get_main_inventory()
if not inv or not inv.valid then return {ok=false,status="blocked",reason="character_main_inventory_missing",completed=0,results={}} end
local function target_error(spec)
 if not d or d.world_id~=spec.expected_entity_world_id then return "owned_mine_world_changed" end
 local e=target(spec.position,spec.name)
 if not e or not e.valid then return "target_missing" end
 if e.unit_number~=spec.expected_entity_unit or e.force~=f or e.surface~=s
  or e.position.x~=spec.position.x or e.position.y~=spec.position.y then return "owned_mine_target_changed" end
 if e.type=="resource" then return "mine_batch_resource_forbidden" end
 if string.find(e.name,"crash%-site") or string.find(e.name,"wreck") then return "protected_artifact" end
 if not e.minable then return "target_not_minable" end
 return nil
end
for i,spec in ipairs(batch.actions) do
 local reason=target_error(spec)
 if reason then return {ok=false,status="blocked",reason=reason,completed=0,results={},failed_index=i-1} end
end
local completed=0;local results={}
local function unknown(index,reason)
 return {ok=false,status="blocked",reason="mine_batch_outcome_unknown",completed=completed,
  results=results,uncertain_index=index,exception=tostring(reason)}
end
for i,spec in ipairs(batch.actions) do
 local good,receipt=pcall(function()
  local reason=target_error(spec)
  if reason then return failure(reason) end
  local x=spec;local e=target(x.position,x.name)
''' + OWNED_MINE_GUARD_LUA + r'''
  if not a.mine_entity(e) then return failure("mining_failed") end
  if e.valid then error("mined_owned_entity_still_valid") end
  return success{status="succeeded",unit_number=x.expected_entity_unit}
 end)
 if not good then return unknown(i-1,receipt) end
 if type(receipt)~="table" or type(receipt.ok)~="boolean" then return unknown(i-1,"invalid_child_receipt") end
 if receipt.ok and receipt.status~="succeeded" then return unknown(i-1,"unexpected_child_status") end
 results[#results+1]=receipt
 if not receipt.ok then
  return {ok=false,status="blocked",reason=receipt.reason or "mine_batch_child_failed",
   completed=completed,results=results,failed_index=i-1}
 end
 completed=completed+1
end
return success{status="succeeded",completed=completed,results=results}
'''


def _unknown(reason, *, completed=0, results=None):
    return {'ok': False, 'status': 'blocked', 'reason': 'mine_batch_outcome_unknown',
            'completed': completed, 'results': results or [], 'exception': str(reason)}


def execute_mine_many(game, action):
    """Never retry a possibly applied native command or malformed receipt."""
    validate_mine_many(action, backend=game.backend, expected_world=getattr(game, 'expected_world', None))
    encoded = json.dumps(json.dumps(action, separators=(',', ':')))
    try:
        receipt = game.query('local batch=helpers.json_to_table(' + encoded + ');' + MINE_MANY_LUA)
    except Exception as exc:
        return _unknown(type(exc).__name__ + ': ' + str(exc))
    if not isinstance(receipt, dict):
        return _unknown('invalid_native_batch_receipt')
    # Explicit preflight failures happen before any effect. Native exceptions
    # outside our inner pcall have no trustworthy prefix and are uncertain.
    uncertain = receipt.get('reason') == 'mine_batch_outcome_unknown'
    completed, rows = receipt.get('completed'), receipt.get('results')
    if rows == {}:
        rows = []
        receipt = dict(receipt, results=rows)
    if (type(completed) is not int or not 0 <= completed <= len(action['actions'])
            or not isinstance(rows, list) or len(rows) not in (completed, completed + 1)
            or any(not isinstance(row, dict) for row in rows)
            or any(not isinstance(row, dict) or row.get('ok') is not True
                   or row.get('status') != 'succeeded'
                   or row.get('unit_number') != action['actions'][index]['expected_entity_unit']
                   for index, row in enumerate(rows[:completed]))
            or (receipt.get('ok') is True and (completed != len(action['actions']) or len(rows) != completed
                                               or receipt.get('status') != 'succeeded'))
            or (uncertain and (len(rows) != completed or type(receipt.get('uncertain_index')) is not int
                               or receipt['uncertain_index'] != completed))
            or (receipt.get('ok') is False and not uncertain
                and (receipt.get('status') != 'blocked' or not isinstance(receipt.get('reason'), str)
                     or completed == len(action['actions'])
                     or not ((completed == 0 and not rows)
                             or (len(rows) == completed + 1 and rows[-1].get('ok') is False))))
            or (receipt.get('ok') is not True and receipt.get('ok') is not False)):
        return _unknown('invalid_native_batch_receipt')
    return receipt
