"""The remedy ladder: what a blocked planning failure tries before it may stay blocked.

A spatial planner that finds no plan blocks its job with a ``needs`` record
(``plan_needs``). The supervisor (``Supervisor._remedy_ladder``) then walks this
ladder for the job, one rung per check, in a fixed order (rules, not a model):

``replan``  the same request again: neighbours, trees, the budget changed the answer;
``layout``  another layout candidate of the same block (other form, other port side);
``site``    another site / a longer connection (long-route fallback, detour);
``relax``   the planner's declared soft-constraint relaxations (growth, turn caps, margins);
``clear``   remove what is in the way when it is ours and cheap (paid, guarded);
``shrink``  a smaller request now (fewer drills, a lower rate), growth later.

Every rung is a set of *options* the planner reads while it plans
(``option(name, default)``; set with ``applied(options)`` around the step by
``jobs._advance_job`` from ``job["remedy"]``). Before a rung is used the
supervisor re-plans read-only with those options (``what_if``: the post-mortem
dry run in a sandbox, nothing sent to the world, bounded planning budget) and
retries the job with the rung only when the dry run yields a plan, so retries
are not burned.

Bounds (``choose``): each rung at most once per distinct ``needs`` signature;
``replan`` again only when published facts changed or a recheck period passed
(the caller decides); a rung that spends more actor time than waiting for a
known unblock event (``needs["wait_seconds"]``) is skipped (wait-vs-spend), and
so is a rung that spends an item the critical path claims. Unknown action
outcomes are never touched: a job with ``pending_action`` or an unknown outcome
is not on the ladder (``eligible``). When every rung is spent the job stays
blocked with ``needs["change"]`` and ``next_remedy`` names the fact it waits for.

A planner declares its rungs in ``RELAXATIONS`` (docs/v2/ADDING_A_CAPABILITY.md).
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Mapping

from . import plan_needs

LADDER = ("replan", "layout", "site", "relax", "clear", "shrink")
WHAT_IF_SECONDS = 30.0  # planning budget of one what-if (postmortem uses 90 s for a full sweep)
# Hard maxima of the shared route relaxation (``route_relax`` option, transport.plan_connection).
ROUTE_RELAX = {
    0: {"long_route_distance": None, "long_route_turns": None, "max_detour_ratio": None},
    1: {"long_route_distance": 48, "long_route_turns": 12, "max_detour_ratio": 1.6},
    2: {"long_route_distance": 16, "long_route_turns": 14, "max_detour_ratio": 2.0},
}

_ACTIVE: contextvars.ContextVar[Mapping] = contextvars.ContextVar("remedy_options", default={})


@dataclass(frozen=True)
class Rung:
    """One remedy: ``options`` the planner reads, the needs kinds it addresses (empty: any)."""

    name: str
    kind: str
    options: Mapping = field(default_factory=dict)
    kinds: tuple = ()
    cost_seconds: float = 0.0  # actor seconds it spends beyond planning (clear: mining + rebuild)
    items: Mapping = field(default_factory=dict)  # items it spends (critical-path claims)
    note: str = ""

    def to_dict(self) -> dict:
        return {"rung": self.name, "kind": self.kind, "options": dict(self.options), "note": self.note}


REPLAN = Rung("replan", "replan", note="same request; facts changed")
ROUTE_RELAX_1 = Rung(
    "site:longer_connection",
    "site",
    {"route_relax": 1},
    kinds=("route", "site", "tap", "plan"),
    note="sites with a longer connection: long-route fallback from 48 tiles, 12 turns, detour 1.6",
)
ROUTE_RELAX_2 = Rung(
    "relax:route_turns_max",
    "relax",
    {"route_relax": 2},
    kinds=("route", "site", "tap", "plan"),
    note="turn/detour caps at their hard maximum: fallback from 16 tiles, 14 turns, detour 2.0",
)
ROUTE_WAYPOINTS = Rung(
    "relax:waypoint_route",
    "relax",
    {"route_relax": 2, "route_waypoints": True},
    kinds=("route", "site", "tap", "plan"),
    note="a cross-base route as two legs through a waypoint (transport.waypoint_connection)",
)

# Declared per planner (ADDING_A_CAPABILITY.md: every new planner declares its relaxations).
RELAXATIONS: dict[str, tuple[Rung, ...]] = {
    "logistic_science": (
        Rung("layout:next_form", "layout", {"layout_rotate": 1}, note="the other green form (cluster/split) first"),
        ROUTE_RELAX_1,
        ROUTE_RELAX_2,
    ),
    "mining_outpost": (
        Rung(
            "shrink:half_drills",
            "shrink",
            {"count_scale": 0.5},
            kinds=("bank",),
            note="half the drills now, grow later",
        ),
        Rung("shrink:two_drills", "shrink", {"count_max": 2}, kinds=("bank",), note="one drill pair now, grow later"),
    ),
    "dedicated_materials": (ROUTE_RELAX_1,),
    # Pack block growth builds slots its frame reserved: no route or site to relax. A slot that meets
    # something (preflight) is re-planned once the blocker moved (replan); no slot left is the next unit
    # (pack_growth next_step), never a relaxation.
    "science_growth": (),
    "produce": (
        Rung("layout:next_variant", "layout", {"layout_rotate": 1}, note="the next ranked row layout first"),
        ROUTE_RELAX_1,
        ROUTE_RELAX_2,
    ),
    # planned_products: engines and magazines are produce rows on a planned world.
    "engine_production": (
        Rung("layout:next_variant", "layout", {"layout_rotate": 1}, note="the next ranked row layout first"),
        ROUTE_RELAX_1,
        ROUTE_RELAX_2,
    ),
    "ammo_production": (
        Rung("layout:next_variant", "layout", {"layout_rotate": 1}, note="the next ranked row layout first"),
        ROUTE_RELAX_1,
        ROUTE_RELAX_2,
    ),
    "steam_expansion": (
        Rung(
            "site:wider_steam_search",
            "site",
            {"steam_fallback_sites": 12, "steam_water_run_max": 64},
            kinds=("site",),
            note="twelve free sites near the plant, water runs up to 64 tiles (steam_siting hard maxima)",
        ),
        ROUTE_RELAX_1,
        ROUTE_RELAX_2,
        ROUTE_WAYPOINTS,
    ),
    "trunk_return_feed": (
        Rung(
            "site:past_first_tap",
            "site",
            {"return_feed_past_taps": True},
            kinds=("site",),
            note="trunk runs past the first tapped run (returned items reach only the later taps)",
        ),
    ),
    "smelting_district": (ROUTE_RELAX_1,),
    "planned_main_bus": (ROUTE_RELAX_1,),
    "brick_production": (ROUTE_RELAX_1, ROUTE_RELAX_2, ROUTE_WAYPOINTS),
    "steel_production": (ROUTE_RELAX_1, ROUTE_RELAX_2, ROUTE_WAYPOINTS),
    "steel_supply": (ROUTE_RELAX_1, ROUTE_RELAX_2, ROUTE_WAYPOINTS),
    "supply_cell": (ROUTE_RELAX_1,),
    "oil_gathering": (
        Rung(
            "site:wider_survey",
            "site",
            {"oil_survey_radius": 1024},  # oil_gathering.OIL_SURVEY_RADIUS_MAX (the planner clamps to it)
            kinds=("site",),
            cost_seconds=240.0,  # scouting walks into the wider ring before a field is known
            note="crude-oil survey rings widened from 512 to the hard maximum of 1024 tiles, scouting included",
        ),
    ),
    # Late fluids: their typed pipe routes (chemical_production.plan_typed_fluid_connection) read route_relax.
    "advanced_petroleum": (ROUTE_RELAX_1, ROUTE_RELAX_2),
    "fluid_product": (ROUTE_RELAX_1, ROUTE_RELAX_2),
    "fluid_distribution": (ROUTE_RELAX_1, ROUTE_RELAX_2),
}


def ladder(capability: str) -> tuple[Rung, ...]:
    """The rungs of ``capability`` in ladder order (``replan`` first for every planner)."""
    rungs = (REPLAN,) + tuple(RELAXATIONS.get(capability, ()))
    return tuple(sorted(rungs, key=lambda r: LADDER.index(r.kind)))


# ----- options a planner reads --------------------------------------------------------------
def option(name: str, default: Any = None) -> Any:
    """The active remedy's option ``name`` (``default`` outside a remedy)."""
    return _ACTIVE.get().get(name, default)


def route_limits() -> dict:
    """``ROUTE_RELAX`` row of the active ``route_relax`` level (all None at level 0)."""
    level = option("route_relax", 0)
    return dict(ROUTE_RELAX.get(level if isinstance(level, int) else 0, ROUTE_RELAX[0]))


def scaled_count(count: int, minimum: int = 1) -> int:
    """A machine count under the active ``shrink`` rung (``count_scale``, ``count_max``)."""
    scale = option("count_scale")
    if isinstance(scale, (int, float)) and 0 < scale < 1:
        count = int(count * scale)
    cap = option("count_max")
    if isinstance(cap, int) and cap >= minimum:
        count = min(count, cap)
    return max(minimum, count)


@contextlib.contextmanager
def applied(options: Mapping | None):
    """Plan with ``options`` (a job's ``remedy["options"]``); nothing outside the block sees them."""
    token = _ACTIVE.set(dict(options or {}))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def merged(active: Mapping | None, rung: Rung) -> dict:
    """Options of ``rung`` on top of the job's active remedy (relaxations accumulate)."""
    return {**dict((active or {}).get("options") or {}), **dict(rung.options)}


# ----- resolver ------------------------------------------------------------------------------
def needs_of(job: Mapping) -> dict | None:
    """The job's needs, or derived from its reason and evidence (a job blocked before needs existed)."""
    if isinstance(job.get("needs"), Mapping) and job["needs"].get("kind"):
        return dict(job["needs"])
    evidence = job.get("evidence") if isinstance(job.get("evidence"), Mapping) else {}
    return plan_needs.from_result(
        str(job.get("capability")),
        {"status": "blocked", "reason": job.get("reason"), "evidence": evidence},
        job.get("parameters") if isinstance(job.get("parameters"), Mapping) else None,
    )


def eligible(job: Mapping) -> bool:
    """A blocked planning failure with needs; never an unknown outcome, a rollback or a deadline."""
    reason = str(job.get("reason") or "")
    return (
        job.get("status") == "blocked"
        and bool(needs_of(job))
        and not job.get("pending_action")
        and reason != "action_outcome_unknown_reconcile_required"
        and "rollback" not in reason
        and "deadline" not in reason
    )


def tried_for(tried, sig: str) -> set[str]:
    return {t.get("rung") for t in tried or () if isinstance(t, Mapping) and t.get("signature") == sig}


def choose(capability: str, needs: Mapping, tried=(), *, claimed: Mapping | None = None) -> tuple[Rung | None, list]:
    """The next rung for ``needs`` and the rungs skipped on the way (``[{rung, why}]``).

    ``tried``: rows ``{"rung", "signature", ...}`` already checked; a rung is used once per
    signature. ``claimed``: critical-path item claims (``critical_path.critical_claims``).
    """
    sig = plan_needs.signature(needs)
    done = tried_for(tried, sig)
    skipped = []
    wait = needs.get("wait_seconds") if isinstance(needs, Mapping) else None
    for rung in ladder(capability):
        if rung.name in done:
            continue
        if rung.kinds and needs.get("kind") not in rung.kinds:
            continue
        if isinstance(wait, (int, float)) and rung.cost_seconds > wait:
            skipped.append({"rung": rung.name, "why": f"waiting {wait:.0f}s is cheaper than {rung.cost_seconds:.0f}s"})
            continue
        hits = sorted(item for item in rung.items if claimed and item in claimed)
        if hits:
            skipped.append({"rung": rung.name, "why": "spends critical-path items: " + ", ".join(hits)})
            continue
        return rung, skipped
    return None, skipped


def next_remedy(capability: str, needs: Mapping, tried=(), *, claimed=None) -> dict:
    """Dashboard field: the rung the ladder tries next, or what the job waits for."""
    rung, _ = choose(capability, needs, tried, claimed=claimed)
    if rung is not None:
        return {"rung": rung.name, "kind": rung.kind}
    return {"rung": None, "wait_for": (needs or {}).get("change")}


# ----- what-if: read-only re-plan ------------------------------------------------------------
def dry_runs() -> dict:
    """Pure planners per capability (``postmortem.DRY_RUN``: adding one there puts it on the ladder)."""
    from . import postmortem

    return {c: spec.prepare for c, spec in postmortem.DRY_RUN.items()}


def fits(result) -> bool | None:
    """A pure prepare's answer: True (a plan), False (blocked), None (not a plan result)."""
    if not isinstance(result, Mapping):
        return None
    if result.get("ok") is True:
        return True
    if result.get("ok") is False or result.get("status") in ("blocked", "failed"):
        return False
    return None


def what_if(
    root,
    live,
    catalog,
    job: Mapping,
    options: Mapping | None,
    *,
    budget_seconds=WHAT_IF_SECONDS,
    executors_factory=None,
) -> dict:
    """Re-plan ``job`` read-only with ``options``: ``{"fits", "reason", "needs", "seconds"}``.

    The planner runs against ``postmortem.ReadOnlyGame`` on a sandbox copy of the
    run's state (a temporary folder outside the run, removed afterwards); its
    queries are world-bound reads, an action or a writing query is refused
    (``fits`` None). ``fits`` None also when the capability has no pure planner.
    """
    from . import postmortem

    capability = str(job.get("capability"))
    planner = dry_runs().get(capability)
    started = time.perf_counter()
    if planner is None:
        return {"fits": None, "reason": "no_pure_planner", "seconds": 0.0}
    factory = executors_factory or postmortem._default_executors
    sandbox = Path(tempfile.mkdtemp(prefix="whatif-"))
    try:
        postmortem.make_sandbox(Path(root), sandbox)
        game = postmortem.ReadOnlyGame(live, sandbox)
        try:
            obs = game.observe()
            with postmortem._planning_budget(budget_seconds), applied(options):
                result = planner(factory(game, catalog), obs, dict(job.get("parameters") or {}))
        except postmortem.ReadOnlyViolation as exc:
            return {"fits": None, "reason": f"read_only_violation: {exc}"[:200], "seconds": _since(started)}
        answer = fits(result)
        out = {"fits": answer, "reason": str((result or {}).get("reason") or ("plan" if answer else ""))[:200]}
        if answer is False:
            out["needs"] = plan_needs.from_result(capability, dict(result, status="blocked"))
        out["seconds"] = _since(started)
        return out
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def _since(started) -> float:
    return round(time.perf_counter() - started, 2)


def record(tried, rung: Rung, needs: Mapping, outcome: Mapping, now: float) -> list:
    """``tried`` plus one bounded row for this check (the last 24 rows are kept)."""
    row = {
        "rung": rung.name,
        "kind": rung.kind,
        "signature": plan_needs.signature(needs),
        "needs_kind": (needs or {}).get("kind"),
        "fits": outcome.get("fits"),
        "reason": str(outcome.get("reason") or "")[:120],
        "seconds": outcome.get("seconds"),
        "time": round(now, 1),
    }
    return (list(tried or ()) + [row])[-24:]


def dumps(options: Mapping | None) -> str:
    return json.dumps(dict(options or {}), sort_keys=True, separators=(",", ":"))
