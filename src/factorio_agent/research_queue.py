"""Keep the native research queue filled so labs never idle between technologies.

Pure planning plus the Lua body of the ``research_queue`` action.

Policy (``plan_queue``):
- the running research stays first and is never interrupted;
- then the unlock paths of the wanted technologies (roadmap order, then
  technologies unlocking wanted recipes), then of ``preferences``, then the
  cheapest supplied technology, repeated until ``RESEARCH_QUEUE_SIZE``;
- a technology enters only when it is enabled, not a research-trigger
  technology, every prerequisite is researched or earlier in the plan, and
  every science pack it needs is in ``supplied_packs`` (proven automatic lab
  supply). Without supplied packs nothing is queued: the bootstrap phase's
  finite hand-funded batches keep owning one technology at a time.

The action (``RESEARCH_QUEUE_LUA``) writes current research, then the listed
technologies, then the rest of the existing native queue, skipping anything
the force cannot research in that order, truncated to ``RESEARCH_QUEUE_SIZE``;
the receipt lists the native queue read back after the write.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from factorio_agent.research_policy import (
    _fallback,
    _obs_researched,
    _packs,
    _wanted_targets,
    unlock_path,
)

# Length of the native queue the planner keeps filled, current research
# included. Factorio's in-game queue holds 7 technologies; the API documents
# no limit for scripts, so the action also reads the queue back.
RESEARCH_QUEUE_SIZE = 7

_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")


def native_queue(obs: Mapping[str, Any]) -> list[str]:
    """The observed native queue, current research first (``research`` alone when no queue is observed)."""
    raw = obs.get("research_queue")
    names = [name for name in raw if isinstance(name, str)] if isinstance(raw, list) else []
    current = obs.get("research")
    if isinstance(current, str) and current not in names:
        names.insert(0, current)
    return list(dict.fromkeys(names))


def _queueable(catalog, name: str, done: set[str], planned: list[str], supplied: set[str]) -> bool:
    tech = catalog.technologies.get(name)
    if tech is None or name in done or name in planned:
        return False
    if tech.get("enabled") is False or tech.get("research_trigger"):
        return False
    if not _packs(catalog, name) <= supplied:
        return False
    return all(p in done or p in planned for p in tech.get("prerequisites", []))


def plan_queue(
    catalog,
    obs: Mapping[str, Any],
    *,
    wanted_technologies: Iterable[str] = (),
    wanted_recipes: Iterable[str] = (),
    preferences: Iterable[str] = (),
    supplied_packs: Iterable[str] = (),
    size: int = RESEARCH_QUEUE_SIZE,
) -> list[str]:
    """The ordered native queue the policy wants (see module docstring); empty without supplied packs."""
    supplied = set(supplied_packs)
    if not supplied:
        return []
    done = _obs_researched(obs)
    plan: list[str] = []
    current = obs.get("research")
    if isinstance(current, str) and current not in done:
        plan.append(current)

    def extend(target: str) -> None:
        for name in unlock_path(catalog, done, target).lab_order:
            if len(plan) >= size:
                return
            if name in plan:
                continue
            if not _queueable(catalog, name, done, plan, supplied):
                return
            plan.append(name)

    wanted = [name for name in wanted_technologies if name in catalog.technologies]
    for target in _wanted_targets(catalog, obs, wanted_recipes, wanted):
        extend(target)
    for target in preferences:
        if target in catalog.technologies:
            extend(target)
    while len(plan) < size:
        ready = {name for name in catalog.technologies if _queueable(catalog, name, done, plan, supplied)}
        name = _fallback(catalog, ready)
        if name is None:
            break
        plan.append(name)
    return plan[:size]


def queue_action(obs: Mapping[str, Any], plan: list[str], *, reason: str, require_room: bool = True) -> dict | None:
    """A ``research_queue`` action adding the planned technologies missing from the native queue, or None.

    ``require_room`` (the idle lane) only acts while the native queue is shorter
    than ``RESEARCH_QUEUE_SIZE``; a roadmap step passes False so its technology
    goes in even when lower-ranked fillers occupy every slot.
    """
    queue = native_queue(obs)
    if not [name for name in plan if name not in queue]:
        return None
    if require_room and len(queue) >= RESEARCH_QUEUE_SIZE:
        return None
    current = obs.get("research")
    technologies = [name for name in plan if name != current][:RESEARCH_QUEUE_SIZE]
    if not technologies:
        return None
    return {"type": "research_queue", "technologies": technologies, "reason": reason[:300]}


def validate_queue_action(action: Mapping[str, Any]) -> None:
    if set(action) - {"type", "technologies", "reason"}:
        raise ValueError("research_queue takes only technologies and reason")
    names = action.get("technologies")
    if not isinstance(names, list) or not 1 <= len(names) <= RESEARCH_QUEUE_SIZE:
        raise ValueError(f"research_queue requires 1 to {RESEARCH_QUEUE_SIZE} technologies")
    if any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in names):
        raise ValueError("invalid technology name")
    if len(set(names)) != len(names):
        raise ValueError("research_queue technologies must be distinct")
    if "reason" in action and not isinstance(action["reason"], str):
        raise ValueError("research_queue reason must be a string")


# Writes the whole queue once; current research stays first so it is never
# switched. Entries the force cannot research in this order are skipped and
# reported, never failed: the read-back queue is the receipt.
RESEARCH_QUEUE_LUA = (
    """
local limit="""
    + str(RESEARCH_QUEUE_SIZE)
    + """
local before={}
for _,t in pairs(f.research_queue or {}) do before[#before+1]=t.name end
local order={};local seen={};local skipped={}
local function put(name)
 if seen[name] then return end
 if #order>=limit then skipped[#skipped+1]=name;return end
 local t=f.technologies[name]
 if not t or t.researched or not t.enabled or t.prototype.research_trigger then skipped[#skipped+1]=name;return end
 for _,p in pairs(t.prerequisites) do if not p.researched and not seen[p.name] then skipped[#skipped+1]=name;return end end
 order[#order+1]=name;seen[name]=true
end
if f.current_research then put(f.current_research.name) end
for _,name in ipairs(x.technologies) do put(name) end
for _,name in ipairs(before) do put(name) end
local changed=#order~=#before
for i=1,#order do if order[i]~=before[i] then changed=true end end
if changed then f.research_queue=order end
local after={}
for _,t in pairs(f.research_queue or {}) do after[#after+1]=t.name end
local listed={}
for _,name in ipairs(after) do listed[name]=true end
local added=0
for _,name in ipairs(x.technologies) do if listed[name] then added=added+1 end end
if #after==0 then return failure("research_queue_rejected") end
return success{status=added>0 and "running" or "waiting",research_queue=after,before=before,skipped=skipped,changed=changed}
"""
)
