"""Early start ("pre-build"): a roadmap block starts once its inputs are planned, not finished.

Why: on a live map the smelting district could only be proposed after all three
mining outposts succeeded and logistics was researched, although its saved
plan needs only the outposts' fixed output ports and most of its hardware
(stone furnaces, inserters, poles, plain belts) needs no new research. The
actor stood idle for minutes while that hardware could have been built.

Rule (``docs/v2/PLANNERS.md``, "Early start"):

1. Declaration: ``jobs.CAPABILITIES[c]["early_start"]`` =
   ``{"requires": [...], "running": [...], "inputs": <check>, "locked_until": [...]}``.
   ``requires`` replaces the capability's ``requires`` for admission
   (``admission_requires``); ``running`` capabilities must be succeeded or
   have an active job; ``inputs`` names a check in ``INPUT_CHECKS`` that reads
   the saved plans of the prerequisites (receipt ``early_start_inputs``);
   ``locked_until`` lists technologies whose recipes the block may wait for.
2. Proposal (``progression.autopilot_nodes`` -> ``early_ready``): a step whose
   strict conditions fail is still proposed when the admission requires
   succeeded, every ``after`` step is satisfied or in flight, every
   ``running`` capability is succeeded or in flight, the inputs check is
   known, and every ``locked_until`` technology is researched or coming
   (``technologies_coming``). Priority stays ``TOP_PRIORITY - index``: below
   every step it runs ahead of.
3. Admission (``jobs._admission_error``): an early job never takes the last
   free job slot (``EARLY_FREE_SLOTS``).
4. Build (``Deferral`` passed to ``FactoryBuilder.ensure_plan``): an entity
   whose recipe waits for a coming technology, or whose hand craft would use
   a plate another active job waits for (only while the job is early), or
   whose item acquisition only waits, is left for later; the rest of the plan
   is built. The job reports ``waiting`` naming the research or the plate,
   never ``blocked``. Success needs the unchanged native proof.
5. Clocks: while early, the job deadline does not run (``track_early_time``).
6. Inputs final: a mining bank's output port is final once the bank is saved
   (the exit-side fallback runs before saving; bridge re-plans keep ports).
   The district stores the ports it planned against; when one changes it
   re-plans if nothing of it is built, else it blocks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

# An early job leaves this many job slots free for the work it runs ahead of.
EARLY_FREE_SLOTS = 1
ACTIVE_STATUSES = frozenset({"pending", "running", "waiting"})
TERMINAL_STATUSES = frozenset({"succeeded", "blocked", "failed"})


def spec(capabilities: Mapping, capability: str) -> Mapping | None:
    row = capabilities.get(capability) if isinstance(capabilities, Mapping) else None
    early = row.get("early_start") if isinstance(row, Mapping) else None
    return early if isinstance(early, Mapping) else None


def admission_requires(capabilities: Mapping, capability: str) -> list[str]:
    """Capabilities that must have succeeded before a job of ``capability`` is admitted."""
    early = spec(capabilities, capability)
    if early is not None:
        return list(early.get("requires", []))
    return list(capabilities[capability].get("requires", []))


def succeeded_capabilities(jobs: Iterable[Mapping]) -> set[str]:
    return {job.get("capability") for job in jobs if job.get("status") == "succeeded"}


def is_early(capabilities: Mapping, capability: str, succeeded: Iterable[str]) -> bool:
    """True while a capability with an early start still lacks one of its strict ``requires``."""
    if spec(capabilities, capability) is None:
        return False
    return not set(capabilities[capability].get("requires", [])) <= set(succeeded)


def _names(value) -> set[str]:
    if isinstance(value, Mapping):
        return {name for name, flag in value.items() if flag}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {name for name in value if isinstance(name, str)}
    return set()


def technologies_coming(observation: Mapping, jobs: Iterable[Mapping]) -> set[str]:
    """Technologies being researched, natively queued, or owned by an active job."""
    from .progression import STEP_TECHNOLOGY

    coming = set()
    if isinstance(observation.get("research"), str):
        coming.add(observation["research"])
    coming |= _names(observation.get("research_queue"))
    for job in jobs:
        if job.get("status") not in ACTIVE_STATUSES:
            continue
        parameters = job.get("parameters") if isinstance(job.get("parameters"), Mapping) else {}
        technology = parameters.get("technology") or STEP_TECHNOLOGY.get(job.get("capability"))
        if isinstance(technology, str):
            coming.add(technology)
    return coming


def locks_pending(early: Mapping, observation: Mapping, jobs: Iterable[Mapping], gate=None) -> list[str] | None:
    """The ``locked_until`` technologies still to come, or None when the block cannot start early.

    None when such a technology is neither researched nor coming, or when the
    native gate (``observe.native_capability_gates``) needs a technology that
    the declaration does not list.
    """
    researched = _names(observation.get("technologies"))
    waiting = [t for t in early.get("locked_until", []) if t not in researched]
    if waiting and not set(waiting) <= technologies_coming(observation, jobs):
        return None
    if isinstance(gate, Mapping) and gate.get("native_recipes_ready") is False:
        allowed = set(early.get("locked_until", []))
        for technologies in (gate.get("recipe_unlockers") or {}).values():
            if not set(technologies or ()) & allowed:
                return None
    return waiting


def ledger_jobs(root, world_id) -> list[dict]:
    """Jobs of the runtime's ``jobs.json`` for this world ([] without a ledger)."""
    path = Path(root) / "jobs.json"
    if root is None or not path.exists():
        return []
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if ledger.get("world_id") != world_id:
        return []
    return [job for job in (ledger.get("jobs") or {}).values() if job.get("world_id", world_id) == world_id]


def capability_pending(root, world_id, capability: str) -> bool:
    """A ledger exists for this world and no job of ``capability`` succeeded yet."""
    path = Path(root) / "jobs.json"
    if not path.exists():
        return False
    return capability not in succeeded_capabilities(ledger_jobs(root, world_id))


def contested_plates(jobs: Iterable[Mapping], *, exclude: Iterable[str] = ()) -> set[str]:
    """Raw items another active job waits for: its reason starts "waiting" and names it, or its evidence item."""
    from .progression import RAW_ITEMS

    skip = set(exclude)
    items = set()
    for job in jobs:
        if job.get("capability") in skip or job.get("status") not in ACTIVE_STATUSES:
            continue
        reason = str(job.get("reason") or "")
        evidence = job.get("evidence") if isinstance(job.get("evidence"), dict) else {}
        for raw in RAW_ITEMS:
            named = re.search(rf"(?<![\w-]){re.escape(raw)}(?![\w-])", reason)
            if evidence.get("item") == raw or (reason.startswith("waiting") and named):
                items.add(raw)
    return items


# ----- inputs ---------------------------------------------------------------
def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _published_ports(board: Mapping) -> list:
    return [
        port.get("position")
        for block in (board.get("blocks") or {}).values()
        if isinstance(block, Mapping)
        for port in block.get("ports") or ()
        if isinstance(port, Mapping)
    ]


def mining_bank_ports(root, observation: Mapping) -> dict:
    """District inputs: one saved bank per source (built or not) whose output port is published on the board."""
    from .smelting import district_sources

    world = observation.get("world_id")
    mining = _read(Path(root) / "mining-outposts.json")
    if mining.get("world_id") != world:
        return {"known": False, "reason": "no saved mining banks for this world"}
    sources, reason = district_sources(mining)
    if sources is None:
        return {"known": False, "reason": reason}
    board = _read(Path(root) / "pcb-board.json")
    if board.get("world_id") != world:
        return {"known": False, "reason": "board of another world"}
    published = _published_ports(board)
    missing = [resource for resource, port in sources.items() if port.get("position") not in published]
    if missing:
        return {"known": False, "reason": "bank port not published on the board: " + ",".join(sorted(missing))}
    return {"known": True, "reason": "every source bank has a saved, published output port"}


def district_feed_anchors(root, observation: Mapping) -> dict:
    """Bus inputs: the district plan is registered and the belt under every bus feed splitter is built."""
    from .planned_bus import BUILT_MATERIALS, feed_branch
    from .plate_supply import district_ports

    world = observation.get("world_id")
    district = _read(Path(root) / "smelting-district.json")
    board = _read(Path(root) / "pcb-board.json")
    if district.get("world_id") != world or board.get("world_id") != world or not district.get("plan"):
        return {"known": False, "reason": "no saved smelting district plan for this world"}
    if "smelting_district" not in (board.get("blocks") or {}):
        return {"known": False, "reason": "smelting district not registered on the board"}
    ports = {p["item"]: p for p in district_ports(Path(root), board, world)}
    belts = {
        (e["position"]["x"], e["position"]["y"])
        for e in observation.get("entities", [])
        if e.get("name") == "transport-belt" and isinstance(e.get("position"), Mapping)
    }
    for item in BUILT_MATERIALS:
        if item not in ports:
            return {"known": False, "reason": f"district port {item} missing"}
        try:
            anchor = feed_branch(district["plan"], ports[item])["anchor"]
        except (KeyError, ValueError, TypeError):
            return {"known": False, "reason": f"district port {item} has no feed branch"}
        if (anchor["x"], anchor["y"]) not in belts:
            return {"known": False, "reason": f"bus feed anchor belt for {item} not built yet"}
    return {"known": True, "reason": "district plan registered and every bus feed anchor belt built"}


INPUT_CHECKS: dict[str, Callable[[Any, Mapping], dict]] = {
    "mining_bank_ports": mining_bank_ports,
    "district_feed_anchors": district_feed_anchors,
}


def inputs_receipt(root, observation: Mapping, capabilities: Mapping) -> dict:
    """``{capability: {"known", "reason"}}`` for every capability with an early start (observe receipt)."""
    rows = {}
    for name in sorted(capabilities):
        early = spec(capabilities, name)
        if early is None:
            continue
        check = INPUT_CHECKS.get(early.get("inputs"))
        if check is None:
            rows[name] = {"known": False, "reason": "unknown inputs check"}
            continue
        try:
            rows[name] = check(root, observation)
        except Exception as exc:  # noqa: BLE001 - an estimate must never break observe
            rows[name] = {"known": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    return rows


# ----- proposal -------------------------------------------------------------
def early_ready(step, progress, steps: Mapping, capabilities: Mapping, observation: Mapping, jobs) -> str | None:
    """A reason note when ``step`` may be proposed early (see module rule 2), else None."""
    early = spec(capabilities, step.capability)
    if early is None:
        return None
    succeeded = progress.succeeded_capabilities
    if not set(early.get("requires", [])) <= succeeded:
        return None
    for name in step.after:
        after = steps[name]
        if not (progress.satisfied(after) or progress.in_flight(after)):
            return None
    active = {job.get("capability") for job in jobs if job.get("status") in ACTIVE_STATUSES}
    if not set(early.get("running", [])) <= succeeded | active:
        return None
    row = (observation.get("early_start_inputs") or {}).get(step.capability)
    if not isinstance(row, Mapping) or row.get("known") is not True:
        return None
    gates = observation.get("native_capability_gates")
    gate = gates.get(step.capability) if isinstance(gates, Mapping) else None
    waiting = locks_pending(early, observation, jobs, gate)
    if waiting is None:
        return None
    note = "; early start: " + str(row.get("reason", "inputs planned"))
    return note + ("; waits for research " + ",".join(waiting) if waiting else "")


# ----- build ----------------------------------------------------------------
class Deferral:
    """Which plan entities ``FactoryBuilder.ensure_plan`` leaves for later (module rule 4)."""

    def __init__(self, catalog, observation: Mapping, *, coming: Iterable[str] = (), held: Iterable[str] = ()):
        self.catalog = catalog
        self.enabled = (
            observation.get("enabled_recipes") if isinstance(observation.get("enabled_recipes"), Mapping) else None
        )
        self.coming = set(coming) - _names(observation.get("technologies"))
        self.held = set(held)
        self.unlockers: dict[str, list[str]] = {}
        for name, technology in (getattr(catalog, "technologies", None) or {}).items():
            for recipe in (technology or {}).get("unlocks", []) if isinstance(technology, Mapping) else ():
                self.unlockers.setdefault(recipe, []).append(name)

    def before(self, item: str) -> str | None:
        """A reason to leave ``item`` for later before any acquisition: its recipe waits for coming research."""
        if self.enabled is None or not self.coming:
            return None
        recipe = self.catalog.recipe_for_product(item) if hasattr(self.catalog, "recipe_for_product") else None
        name = recipe.get("name") if isinstance(recipe, Mapping) else None
        if not name or self.enabled.get(name) is True:
            return None
        technologies = sorted(set(self.unlockers.get(name, [])) & self.coming)
        if not technologies:
            return None
        return f"waiting for research {','.join(technologies)} to unlock {name}"

    def after(self, item: str, result: Mapping) -> str | None:
        """A reason to leave ``item`` for later given its acquisition result, else None (use the result)."""
        if not isinstance(result, Mapping):
            return None
        if not result.get("type") and result.get("status") == "waiting":
            return str(result.get("reason") or "waiting")
        if result.get("type") == "craft" and self.held:
            from .progression import raw_cost

            recipe = (getattr(self.catalog, "recipes", None) or {}).get(result.get("recipe"))
            ingredients = {
                row["name"]: row["amount"]
                for row in (recipe or {}).get("ingredients") or ()
                if isinstance(row, Mapping) and row.get("type", "item") == "item" and "name" in row and "amount" in row
            }
            plates = sorted((set(raw_cost(self.catalog, ingredients)) | set(ingredients)) & self.held)
            if plates:
                return f"waiting: {','.join(plates)} kept for jobs waiting for it; {item} crafted later"
        return None


def deferral(root, observation: Mapping, catalog, capability: str, capabilities: Mapping | None = None):
    """The ``Deferral`` for a job of ``capability``, or None when nothing is left for later (normal build)."""
    if capabilities is None:
        from .jobs import CAPABILITIES as capabilities  # noqa: N811 - lazy: jobs imports the controllers
    if spec(capabilities, capability) is None:
        return None
    jobs = ledger_jobs(root, observation.get("world_id"))
    early = is_early(capabilities, capability, succeeded_capabilities(jobs))
    researched = _names(observation.get("technologies"))
    locked = set(spec(capabilities, capability).get("locked_until", [])) - researched
    coming = technologies_coming(observation, jobs) & locked
    held = contested_plates(jobs, exclude=(capability,)) if early else set()
    if not early and not coming:
        return None
    return Deferral(catalog, observation, coming=coming, held=held)


def track_early_time(job: dict, tick: int, now: float, early: bool) -> None:
    """Accumulate ``early_ticks``/``early_seconds`` while the job runs early; the deadline ignores them."""
    mark = job.get("early_mark")
    if early and isinstance(mark, list) and len(mark) == 2:
        job["early_ticks"] = job.get("early_ticks", 0) + max(0, tick - mark[0])
        job["early_seconds"] = job.get("early_seconds", 0) + max(0.0, now - mark[1])
    if early:
        job["early_mark"] = [tick, now]
    else:
        job.pop("early_mark", None)
