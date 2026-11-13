"""Critical path of the roadmap and the allocation rule built on it (PLANNERS.md "Critical path").

Pure functions, no IO. One definition, used in three places: job order and
material allocation (``jobs.run_jobs``), and the timeline tool
(``tools/critical_path.py``).

Definition. A roadmap step is *satisfied* when a matching job succeeded or
its technology is researched (``progression._Progress``; a step named in
``progression.STEP_TECHNOLOGY`` also counts once that technology is
researched). A step is *on the path* unless it declares a fixed ``priority``
(the roadmap's way of keeping a step off the critical path, e.g.
``power_coal_feed``). Its *dependencies* are computed, not listed:

- its ``after`` steps;
- for each capability in ``CAPABILITIES[capability]["requires"]`` that has no
  succeeded job: the first roadmap step of that capability;
- for its technology (``parameters.technology`` or ``STEP_TECHNOLOGY``): the
  steps researching each unresearched catalog prerequisite;
- for each locked ``native_recipes`` recipe: the steps researching a
  technology that unlocks it (catalog ``unlocks``).

The *frontier* is the first ``horizon`` unsatisfied, on-path, not stalled
steps in roadmap order (the roadmap order is the intended order of the
run) that do not wait on an unsatisfied off-path or stalled step (such a
step waits for slack work and cannot be advanced by critical work). The *critical steps* are the frontier plus every unsatisfied on-path
step it transitively depends on. Everything else is slack work.

A *critical job* is an active job matching a critical step, or an active job
that supplies a non-raw item a critical job claims (``supplied_items``:
``parameters.products``/``item``/``product``), so a critical job never waits
on a slack supplier that is starved for its sake.

Allocation rule (items in general, not only plates). A critical job *claims*
the items it is waiting for or collecting: catalog item names in its reason,
``evidence.item`` (reserving ``evidence.need`` units when given) and
``evidence.needs`` (``{item: units}``). Slack jobs then plan against a view
with those units removed from the character's inventory and the claimed items
removed from machine and furnace outputs (``slack_view``), and a slack action
that would still take or spend a claimed item is turned into a wait
(``slack_refusal``). Critical jobs advance before slack jobs in every pass.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

CRITICAL_HORIZON = 3
# Controllers that store their own transfer intent before returning an action:
# a refused action would leave that intent dangling, so they only get the slack view.
GUARD_EXEMPT = frozenset(
    {
        "starter_research",
        "bootstrap_research",
        "bootstrap_logistics",
        "research",
        "legacy_coal_cleanup",
        "furnace_upgrade",
    }
)
ACTIVE = frozenset({"pending", "running", "waiting"})
STALLED = frozenset({"blocked", "failed"})
_TOKEN = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]")


@dataclass(frozen=True)
class CriticalPath:
    """Step ids in roadmap order."""

    frontier: tuple[str, ...]
    steps: tuple[str, ...]
    ready: tuple[str, ...]
    stalled: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "frontier": list(self.frontier),
            "steps": list(self.steps),
            "ready": list(self.ready),
            "stalled": list(self.stalled),
        }


def _table(catalog, name: str) -> Mapping:
    if catalog is None:
        return {}
    value = catalog.get(name) if isinstance(catalog, Mapping) else getattr(catalog, name, None)
    return value if isinstance(value, Mapping) else {}


def _names(value) -> set[str]:
    if isinstance(value, Mapping):
        return {name for name, flag in value.items() if flag}
    if isinstance(value, Iterable) and not isinstance(value, str):
        return {name for name in value if isinstance(name, str)}
    return set()


def step_technology(step) -> str | None:
    from .progression import STEP_TECHNOLOGY

    technology = step.parameters.get("technology")
    return technology if isinstance(technology, str) else STEP_TECHNOLOGY.get(step.id)


class _Graph:
    def __init__(self, roadmap, jobs, observation, capabilities, catalog):
        from .progression import _Progress

        self.roadmap = tuple(roadmap)
        self.capabilities = capabilities
        self.researched = _names(observation.get("technologies"))
        self.enabled = _names(observation.get("enabled_recipes"))
        self.has_enabled = "enabled_recipes" in observation
        self.progress = _Progress(tuple(jobs), frozenset(self.researched))
        self.succeeded = self.progress.succeeded_capabilities
        self.technologies = _table(catalog, "technologies")
        self.by_id = {step.id: step for step in self.roadmap}

    def satisfied(self, step) -> bool:
        technology = step_technology(step)
        return (technology is not None and technology in self.researched) or self.progress.satisfied(step)

    def researching(self, technology: str) -> list:
        return [step for step in self.roadmap if step_technology(step) == technology]

    def dependencies(self, step) -> list:
        found = [self.by_id[name] for name in step.after if name in self.by_id]
        spec = self.capabilities.get(step.capability) or {}
        for capability in spec.get("requires") or ():
            if capability in self.succeeded:
                continue
            first = next((s for s in self.roadmap if s.capability == capability and s.id != step.id), None)
            if first is not None:
                found.append(first)
        technology = step_technology(step)
        row = self.technologies.get(technology) if technology else None
        for prerequisite in (row or {}).get("prerequisites") or ():
            if prerequisite not in self.researched:
                found += self.researching(prerequisite)
        for recipe in spec.get("native_recipes") or ():
            if not self.has_enabled or recipe in self.enabled:
                continue
            for name, tech in self.technologies.items():
                if name not in self.researched and recipe in (tech.get("unlocks") or ()):
                    found += self.researching(name)
        unique = []
        for dep in found:
            if dep.id != step.id and dep not in unique:
                unique.append(dep)
        return unique


def critical_path(
    jobs: Iterable[Mapping],
    observation: Mapping,
    capabilities: Mapping,
    *,
    roadmap=None,
    catalog=None,
    horizon: int = CRITICAL_HORIZON,
) -> CriticalPath:
    """The critical steps now (module docstring). ``roadmap`` defaults to ``progression.ROADMAP``."""
    if roadmap is None:
        from .progression import ROADMAP as roadmap
    graph = _Graph(roadmap, jobs, observation, capabilities, catalog)
    open_steps = [s for s in graph.roadmap if s.capability in capabilities and not graph.satisfied(s)]
    stalled = tuple(s.id for s in open_steps if graph.progress.stalled(s))
    behind_slack: dict[str, bool] = {}

    def waits_on_slack(step, seen=()) -> bool:
        """An unsatisfied off-path (or stalled) step lies under ``step``: critical work cannot advance it."""
        if step.id not in behind_slack:
            found = False
            for dep in graph.dependencies(step):
                if dep.id in seen or dep.capability not in capabilities or graph.satisfied(dep):
                    continue
                if dep.priority is not None or dep.id in stalled or waits_on_slack(dep, (*seen, step.id)):
                    found = True
                    break
            behind_slack[step.id] = found
        return behind_slack[step.id]

    frontier = [s for s in open_steps if s.priority is None and s.id not in stalled and not waits_on_slack(s)]
    frontier = frontier[: max(0, horizon)]
    critical: dict[str, Any] = {}
    todo = list(frontier)
    while todo:
        step = todo.pop()
        if step.id in critical:
            continue
        critical[step.id] = step
        for dep in graph.dependencies(step):
            if dep.priority is None and dep.capability in capabilities and not graph.satisfied(dep):
                todo.append(dep)
    order = [s.id for s in graph.roadmap if s.id in critical]
    ready = tuple(
        name
        for name in order
        if all(graph.satisfied(dep) or dep.capability not in capabilities for dep in graph.dependencies(critical[name]))
    )
    return CriticalPath(tuple(s.id for s in frontier), tuple(order), ready, stalled)


# ----- jobs ---------------------------------------------------------------
def supplied_items(job: Mapping) -> set[str]:
    """Items a job makes for others, from its parameters (``products``, ``item``, ``product``)."""
    parameters = job.get("parameters") if isinstance(job.get("parameters"), Mapping) else {}
    items = set()
    for key in ("item", "product"):
        if isinstance(parameters.get(key), str):
            items.add(parameters[key])
    products = parameters.get("products")
    if isinstance(products, list):
        items |= {p for p in products if isinstance(p, str)}
    return items


def _amount(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def claims(job: Mapping, known_items: Iterable[str]) -> dict[str, float | None]:
    """Items ``job`` waits for or collects: ``{item: units to reserve, or None for all}``."""
    known = set(known_items)
    found: dict[str, float | None] = {}

    def add(item, units):
        if not isinstance(item, str) or item not in known:
            return
        if item in found:
            old = found[item]
            found[item] = None if old is None or units is None else max(old, units)
        else:
            found[item] = units

    for token in _TOKEN.findall(str(job.get("reason") or "")):
        add(token, None)
    evidence = job.get("evidence") if isinstance(job.get("evidence"), Mapping) else {}
    if isinstance(evidence.get("item"), str):
        found.pop(evidence["item"], None)  # the evidence amount refines the reason's mention
        add(evidence["item"], _amount(evidence.get("need")))
    needs = evidence.get("needs")
    if isinstance(needs, Mapping):
        for item, units in needs.items():
            add(item, _amount(units))
    return found


def _active(job: Mapping) -> bool:
    return job.get("status") in ACTIVE


def critical_jobs(jobs_by_key: Mapping[str, Mapping], path: CriticalPath, *, roadmap=None, known_items=()) -> list[str]:
    """Keys of active jobs on the critical path, then of their slack suppliers (module docstring)."""
    if roadmap is None:
        from .progression import ROADMAP as roadmap
    from .progression import RAW_ITEMS, _same

    steps = [s for s in roadmap if s.id in set(path.steps)]
    keys = [k for k, job in jobs_by_key.items() if _active(job) and any(_same(job, s) for s in steps)]
    for _ in range(3):  # suppliers of suppliers, bounded
        wanted = set()
        for key in keys:
            wanted |= {item for item in claims(jobs_by_key[key], known_items) if item not in RAW_ITEMS}
        extra = [k for k, job in jobs_by_key.items() if k not in keys and _active(job) and supplied_items(job) & wanted]
        if not extra:
            break
        keys += extra
    return keys


def critical_claims(jobs_by_key: Mapping[str, Mapping], keys: Iterable[str], known_items) -> dict[str, dict]:
    """``{item: {"units": total or None, "jobs": [capability, ...]}}`` over the given critical jobs."""
    result: dict[str, dict] = {}
    for key in keys:
        job = jobs_by_key[key]
        for item, units in claims(job, known_items).items():
            row = result.setdefault(item, {"units": 0.0, "jobs": []})
            row["units"] = None if units is None or row["units"] is None else row["units"] + units
            row["jobs"].append(str(job.get("capability")))
    return result


def slack_view(observation: Mapping, claimed: Mapping[str, Mapping]) -> dict:
    """The observation a slack job plans against: claimed units leave the hands, claimed items the outputs."""
    if not claimed:
        return dict(observation)
    view = dict(observation)
    inventory = dict(observation.get("inventory") or {})
    for item, row in claimed.items():
        held = inventory.get(item)
        if not isinstance(held, (int, float)) or isinstance(held, bool):
            continue
        units = row.get("units")
        left = 0 if units is None else max(0, int(held - units))
        if left:
            inventory[item] = left
        else:
            inventory.pop(item, None)
    view["inventory"] = inventory
    entities = []
    for entity in observation.get("entities") or []:
        copy = entity
        for field in ("output_inventory", "belt_inventory"):
            stock = entity.get(field) if isinstance(entity, Mapping) else None
            if isinstance(stock, Mapping) and any(item in stock for item in claimed):
                if copy is entity:
                    copy = dict(entity)
                copy[field] = {k: v for k, v in stock.items() if k not in claimed}
        entities.append(copy)
    view["entities"] = entities
    return view


def _ingredients(catalog, recipe_name) -> set[str]:
    recipes = _table(catalog, "recipes")
    seen, todo, items = set(), [recipe_name], set()
    while todo and len(seen) < 32:
        name = todo.pop()
        if name in seen or name not in recipes:
            continue
        seen.add(name)
        for row in recipes[name].get("ingredients") or ():
            item = row.get("name") if isinstance(row, Mapping) else None
            if isinstance(item, str):
                items.add(item)
                todo += [r for r, rec in recipes.items() if rec.get("main_product") == item or r == item]
    return items


def slack_refusal(action: Mapping, observation: Mapping, claimed: Mapping[str, Mapping], catalog=None) -> dict | None:
    """A wait instead of a slack action that takes or spends a claimed item beyond its unclaimed units."""
    if not claimed or not isinstance(action, Mapping):
        return None
    kind = action.get("type")
    inventory = observation.get("inventory") or {}
    if kind in ("take", "take_many"):
        hits = [action.get("item")] if action.get("item") in claimed else []
    elif kind in ("insert", "build"):
        item = action.get("item") if kind == "insert" else action.get("name")
        hits = []
        if item in claimed:
            units = claimed[item].get("units")
            count = action.get("count", 1) if kind == "insert" else 1
            held = inventory.get(item, 0)
            if units is None or not isinstance(count, (int, float)) or held - count < units:
                hits = [item]
    elif kind == "craft":
        used = _ingredients(catalog, action.get("recipe")) | {action.get("recipe")}
        hits = sorted(i for i in used if i in claimed and claimed[i].get("units") is None)
    else:
        hits = []
    if not hits:
        return None
    item = hits[0]
    owners = sorted(set(claimed[item].get("jobs", [])))
    return {
        "status": "waiting",
        "reason": f"slack work yields {item} to the critical path ({', '.join(owners)})"[:300],
        "evidence": {"reserved_item": item, "reserved_for": owners, "refused_action": str(kind)},
    }


def known_items(catalog, observation: Mapping) -> set[str]:
    """Item names a reason may mention: the catalog's items, else raw items and what the actor holds."""
    from .progression import RAW_ITEMS

    items = set(_table(catalog, "items")) | set(_table(catalog, "recipes"))
    return items or set(RAW_ITEMS) | set((observation.get("inventory") or {}).keys())


@dataclass(frozen=True)
class Allocation:
    """One pass of ``jobs.run_jobs``: who goes first and what slack jobs may not touch."""

    path: CriticalPath
    critical: tuple[str, ...]
    claimed: Mapping[str, Mapping]


def allocation(
    jobs_by_key: Mapping[str, Mapping], observation: Mapping, capabilities: Mapping, catalog=None
) -> Allocation:
    path = critical_path(jobs_by_key.values(), observation, capabilities, catalog=catalog)
    known = known_items(catalog, observation)
    keys = critical_jobs(jobs_by_key, path, known_items=known)
    return Allocation(path, tuple(keys), critical_claims(jobs_by_key, keys, known))
