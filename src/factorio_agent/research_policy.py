"""Pick the next lab technology from catalog data alone.

Pure functions: no game queries, no file IO, no hardcoded technology names.

Rules used throughout:
- A technology is *researched* when ``obs["technologies"][name]`` is truthy.
- A technology is *researchable now* when ``normal_research.ready_options``
  lists it under ``"technologies"`` (enabled, not researched, every
  prerequisite researched, not a research-trigger technology) and, when the
  caller passes ``supplied_packs``, every science pack it needs is in that set.
- A *trigger technology* has a ``research_trigger`` row; it completes through
  gameplay, never through labs, so it is never placed in a lab order.
- A technology is *infinite* when its row has a ``count_formula``.
- Recipe unlocks come from each technology row's ``unlocks`` list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from factorio_agent.normal_research import ready_options

REASON_WANTED = "wanted"
REASON_PREFERENCE = "preference"
REASON_FALLBACK = "fallback"

BLOCKER_PACK_SUPPLY = "missing_science_pack_supply"
BLOCKER_TRIGGER = "trigger_technology"
BLOCKER_DISABLED = "technology_disabled"
BLOCKER_PREREQUISITE = "missing_prerequisite"
BLOCKER_NO_UNLOCKER = "no_unlocking_technology"


@dataclass(frozen=True)
class UnlockPath:
    """Unresearched technologies needed for ``target``.

    ``lab_order`` lists lab (science pack) technologies, prerequisites first;
    it ends with ``target`` when ``target`` is itself a lab technology.
    ``trigger_technologies`` lists research-trigger technologies on the same
    chain, in dependency order; labs cannot research them.
    """

    target: str
    lab_order: tuple[str, ...]
    trigger_technologies: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.lab_order and not self.trigger_technologies


@dataclass(frozen=True)
class ResearchChoice:
    """One technology to start now, why, and which goal it serves."""

    technology: str
    reason: str
    target: str


@dataclass(frozen=True)
class ResearchBlocker:
    """Why ``target`` cannot progress: ``technology`` is stuck because of ``kind``.

    ``missing`` holds the missing names (packs, prerequisites, or recipes).
    ``trigger`` holds the catalog ``research_trigger`` row for trigger blockers.
    """

    target: str
    technology: str | None
    kind: str
    missing: tuple[str, ...] = ()
    trigger: Mapping[str, Any] | None = field(default=None)


def legacy_upgrade_preferences() -> tuple[str, ...]:
    """The old ``upgrades.PRIORITIES`` list, usable as ``preferences``."""
    from factorio_agent.upgrades import PRIORITIES

    return tuple(PRIORITIES)


def _researched_names(technologies_researched: Mapping[str, Any] | Iterable[str]) -> set[str]:
    """Accept either ``obs["technologies"]`` (name -> bool) or a set of names."""
    if isinstance(technologies_researched, Mapping):
        return {name for name, done in technologies_researched.items() if done}
    return set(technologies_researched)


def _obs_researched(obs: Mapping[str, Any]) -> set[str]:
    return _researched_names(obs.get("technologies") or {})


def _is_trigger(catalog, name: str) -> bool:
    return bool(catalog.technologies[name].get("research_trigger"))


def _is_infinite(catalog, name: str) -> bool:
    return bool(catalog.technologies[name].get("count_formula"))


def _packs(catalog, name: str) -> set[str]:
    return {row["name"] for row in catalog.technologies[name].get("ingredients", [])}


def science_cost(catalog, name: str) -> float:
    """Unit count times number of distinct science packs; infinite when unknown."""
    tech = catalog.technologies[name]
    count = tech.get("unit_count")
    if tech.get("count_formula") or count is None:
        return float("inf")
    return float(count) * len(tech.get("ingredients", []))


def unlock_path(catalog, technologies_researched, target: str) -> UnlockPath:
    """Unresearched prerequisite chain for ``target``, prerequisites first.

    Depth-first over ``prerequisites`` in sorted order, so the result is
    deterministic. Researched technologies and everything behind them are
    skipped. Raises ``ValueError`` for an unknown technology or a cycle.
    """
    done = _researched_names(technologies_researched)
    order: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visited or name in done:
            return
        if name not in catalog.technologies:
            raise ValueError(f"unknown technology: {name}")
        if name in visiting:
            raise ValueError(f"technology prerequisite cycle: {name}")
        visiting.add(name)
        for parent in sorted(catalog.technologies[name].get("prerequisites", [])):
            visit(parent)
        visiting.remove(name)
        visited.add(name)
        order.append(name)

    visit(target)
    return UnlockPath(
        target=target,
        lab_order=tuple(n for n in order if not _is_trigger(catalog, n)),
        trigger_technologies=tuple(n for n in order if _is_trigger(catalog, n)),
    )


def technologies_for_recipes(catalog, recipe_names: Iterable[str]) -> set[str]:
    """Technologies whose ``unlocks`` list contains any of ``recipe_names``."""
    wanted = set(recipe_names)
    return {name for name, tech in catalog.technologies.items() if wanted & set(tech.get("unlocks", []))}


def researchable_now(catalog, obs, supplied_packs: Iterable[str] | None = None) -> set[str]:
    """Lab technologies ``ready_options`` offers, filtered by ``supplied_packs`` if given."""
    names = {row["technology"] for row in ready_options(catalog, obs)["technologies"]}
    if supplied_packs is None:
        return names
    supplied = set(supplied_packs)
    return {name for name in names if _packs(catalog, name) <= supplied}


def _recipe_unlocked(catalog, obs, recipe: str) -> bool:
    """A recipe counts as unlocked if the game says so or any unlocker is researched."""
    if (obs.get("enabled_recipes") or {}).get(recipe) is True:
        return True
    done = _obs_researched(obs)
    return any(name in done for name in technologies_for_recipes(catalog, [recipe]))


def _recipe_targets(catalog, obs, recipe: str) -> list[str]:
    """Unlockers for one still-locked recipe, shortest remaining path first, then by name."""
    if _recipe_unlocked(catalog, obs, recipe):
        return []
    done = _obs_researched(obs)

    def length(name: str) -> int:
        path = unlock_path(catalog, done, name)
        return len(path.lab_order) + len(path.trigger_technologies)

    return sorted(technologies_for_recipes(catalog, [recipe]), key=lambda n: (length(n), n))


def _wanted_targets(catalog, obs, wanted_recipes, wanted_technologies) -> list[str]:
    """Wanted technologies in given order, then recipe unlockers in recipe order."""
    targets: list[str] = []
    for name in wanted_technologies:
        if name not in catalog.technologies:
            raise ValueError(f"unknown technology: {name}")
        targets.append(name)
    for recipe in wanted_recipes:
        targets.extend(_recipe_targets(catalog, obs, recipe))
    return list(dict.fromkeys(targets))


def _first_on_path(catalog, obs, target: str, ready: set[str]) -> str | None:
    """First lab technology on ``target``'s unlock path that is researchable now."""
    path = unlock_path(catalog, _obs_researched(obs), target)
    for name in path.lab_order:
        if name in ready:
            return name
    return None


def _fallback(catalog, ready: set[str]) -> str | None:
    """Cheapest finite technology (ties by name); infinite ones only if nothing else."""
    finite = sorted((n for n in ready if not _is_infinite(catalog, n)), key=lambda n: (science_cost(catalog, n), n))
    if finite:
        return finite[0]
    infinite = sorted(ready, key=lambda n: (len(catalog.technologies[n].get("ingredients", [])), n))
    return infinite[0] if infinite else None


def choose_research(
    catalog,
    obs,
    *,
    wanted_recipes: Iterable[str] = (),
    wanted_technologies: Iterable[str] = (),
    preferences: Iterable[str] = (),
    supplied_packs: Iterable[str] | None = None,
) -> ResearchChoice | None:
    """Pick one technology to start now, or ``None``.

    Returns ``None`` while ``obs["research"]`` is set or when nothing is
    researchable. Otherwise, highest rank first:
    a. the first researchable element on the unlock path of a wanted
       technology, or of a technology unlocking a wanted recipe;
    b. the same for each name in ``preferences`` (unknown names are skipped);
    c. the cheapest researchable technology (see ``_fallback``).
    ``supplied_packs`` restricts choices to technologies whose packs are all
    supplied; ``None`` means no pack filter.
    Unknown ``wanted_technologies`` raise ``ValueError``.
    """
    if obs.get("research"):
        return None
    ready = researchable_now(catalog, obs, supplied_packs)
    for target in _wanted_targets(catalog, obs, wanted_recipes, wanted_technologies):
        name = _first_on_path(catalog, obs, target, ready)
        if name:
            return ResearchChoice(technology=name, reason=REASON_WANTED, target=target)
    for target in preferences:
        if target not in catalog.technologies:
            continue
        name = _first_on_path(catalog, obs, target, ready)
        if name:
            return ResearchChoice(technology=name, reason=REASON_PREFERENCE, target=target)
    name = _fallback(catalog, ready)
    if name:
        return ResearchChoice(technology=name, reason=REASON_FALLBACK, target=name)
    return None


def _frontier_blocker(catalog, target: str, name: str, supplied: set[str] | None) -> ResearchBlocker | None:
    """Blocker for one path element whose prerequisites are all researched."""
    tech = catalog.technologies[name]
    if tech.get("enabled") is False:
        return ResearchBlocker(target=target, technology=name, kind=BLOCKER_DISABLED)
    if tech.get("research_trigger"):
        return ResearchBlocker(target=target, technology=name, kind=BLOCKER_TRIGGER, trigger=tech["research_trigger"])
    if supplied is not None:
        missing = sorted(_packs(catalog, name) - supplied)
        if missing:
            return ResearchBlocker(target=target, technology=name, kind=BLOCKER_PACK_SUPPLY, missing=tuple(missing))
    return None


def _target_blockers(catalog, obs, target: str, ready: set[str], supplied: set[str] | None) -> list[ResearchBlocker]:
    done = _obs_researched(obs)
    path = unlock_path(catalog, done, target)
    if path.complete or any(name in ready for name in path.lab_order):
        return []
    remaining = list(path.lab_order) + list(path.trigger_technologies)
    blockers = []
    for name in sorted(remaining):
        prerequisites = catalog.technologies[name].get("prerequisites", [])
        if all(p in done for p in prerequisites):
            blocker = _frontier_blocker(catalog, target, name, supplied)
            if blocker:
                blockers.append(blocker)
    if target in remaining and not any(b.technology == target for b in blockers):
        missing = sorted(p for p in catalog.technologies[target].get("prerequisites", []) if p not in done)
        blockers.append(
            ResearchBlocker(target=target, technology=target, kind=BLOCKER_PREREQUISITE, missing=tuple(missing))
        )
    return blockers


def research_blockers(
    catalog,
    obs,
    *,
    wanted_recipes: Iterable[str] = (),
    wanted_technologies: Iterable[str] = (),
    supplied_packs: Iterable[str] | None = None,
) -> list[ResearchBlocker]:
    """Explain why wanted targets cannot progress through labs right now.

    A target with a researchable element on its lab path has no blockers.
    Otherwise every path element whose prerequisites are researched is
    reported when it is disabled, a trigger technology, or (with
    ``supplied_packs`` given) needs unsupplied packs; the target itself gets a
    ``missing_prerequisite`` entry naming its unresearched prerequisites.
    A wanted recipe no technology unlocks gets ``no_unlocking_technology``.
    Running research is ignored: blockers describe the targets, not the lab.
    """
    wanted_recipes = list(wanted_recipes)
    supplied = None if supplied_packs is None else set(supplied_packs)
    ready = researchable_now(catalog, obs, supplied)
    blockers: list[ResearchBlocker] = []
    for recipe in wanted_recipes:
        if not _recipe_unlocked(catalog, obs, recipe) and not technologies_for_recipes(catalog, [recipe]):
            blockers.append(
                ResearchBlocker(target=recipe, technology=None, kind=BLOCKER_NO_UNLOCKER, missing=(recipe,))
            )
    for target in _wanted_targets(catalog, obs, wanted_recipes, wanted_technologies):
        blockers.extend(_target_blockers(catalog, obs, target, ready, supplied))
    return blockers
