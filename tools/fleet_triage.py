"""Round-end triage of one fleet run from its logs: healthy / blocked / stalled / slow / errored.

Used by ``tools/fleet.py`` when a run reaches the target game minute (before its
server stops) and by ``fleet.py report`` ("problem runs"). Pure functions over
files the run already writes: ``jobs.json``, ``logs/controller-events.jsonl``
(slice rows with tick, actions and the jobs' statuses), the controller errors,
and the milestone minutes the fleet report computes (``fleet.milestones``).
``tools/waste_report.py`` detectors (``run_stalled``, ``blocked_unretried``,
``repeated_block``, ``long_wait``) are attached as evidence when available.

Classes (``flags`` lists every one that holds, ``class`` is the first in
``CLASS_ORDER``):

- ``errored``: >= ``ERROR_MIN`` controller errors, a job ended by an error reason
  (``ERROR_REASONS``: repeated executor failure, planner fault, unknown action
  outcome), or the run's processes were down before the stop.
- ``stalled``: no action and no success for ``STALL_MINUTES`` game minutes, no
  slice in that time, or an empty queue (no active job in the trailing slices)
  for that long.
- ``blocked``: a blocked/failed job that stayed so >= ``BLOCKED_MINUTES``.
- ``slow``: a roadmap milestone later than the fleet median by more than
  ``max(SLOW_MARGIN_MINUTES, SLOW_MARGIN_SHARE * median)``, or missing while at
  least half the fleet reached it and the run is past that late mark.
"""

from __future__ import annotations

import json
from pathlib import Path
import statistics

TICKS_PER_MINUTE = 3600
STALL_MINUTES = 8.0
BLOCKED_MINUTES = 3.0
ERROR_MIN = 3
SLOW_MARGIN_MINUTES = 5.0
SLOW_MARGIN_SHARE = 0.25
MIN_REACHED_FOR_MEDIAN = 3
ERROR_REASONS = (
    "repeated_executor_failure",
    "planner_fault",
    "action_outcome_unknown",
    "postcondition_verification_error",
)
ACTIVE = ("pending", "running", "waiting")
CLASS_ORDER = ("errored", "stalled", "blocked", "slow")
# Which problem runs are kept alive for inspection first (fleet.py --keep-problem).
KEEP_WEIGHT = {"stalled": 4, "blocked": 3, "errored": 2, "slow": 1, "healthy": 0}


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _rows(path):
    try:
        with Path(path).open(encoding="utf-8") as lines:
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _scalars(value):
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if type(v) in (int, float, bool, str) and len(str(v)) <= 200}


def run_facts(root, *, end_tick=None) -> dict:
    """What triage needs from one run folder (cheap: one pass over the controller log)."""
    root = Path(root)
    jobs = (_read(root / "jobs.json", {}) or {}).get("jobs", {}) or {}
    state = _read(root / "logs" / "controller-state.json", {}) or {}
    slices, errors = [], 0
    for row in _rows(root / "logs" / "controller-events.jsonl"):
        if row.get("event") == "error":
            errors += 1
        elif row.get("event") == "slice" and type(row.get("tick")) is int:
            slices.append(row)
    ticks = [j.get("last_tick") for j in jobs.values() if type(j.get("last_tick")) is int]
    if end_tick is None:
        end_tick = state.get("last_observed_tick") if type(state.get("last_observed_tick")) is int else None
    if end_tick is None:
        end_tick = max(ticks + [s["tick"] for s in slices], default=None)
    successes = [
        j["last_tick"] for j in jobs.values() if j.get("status") == "succeeded" and type(j.get("last_tick")) is int
    ]
    acted = [s["tick"] for s in slices if (s.get("actions") or 0) > 0]
    empty_since = None
    for s in reversed(slices):
        if any((v or {}).get("status") in ACTIVE for v in (s.get("jobs") or {}).values()):
            break
        empty_since = s["tick"]
    problems, waiting = [], []
    for key, job in jobs.items():
        if not isinstance(job, dict):
            continue
        row = dict(
            key=key,
            capability=job.get("capability"),
            status=job.get("status"),
            reason=str(job.get("reason") or "")[:200],
            evidence=_scalars(job.get("evidence")),
            since_tick=job.get("blocked_at_tick") if type(job.get("blocked_at_tick")) is int else job.get("last_tick"),
            parameters=job.get("parameters"),
        )
        if job.get("status") in ("blocked", "failed"):
            problems.append(row)
        elif job.get("status") in ACTIVE:
            row["since_tick"] = (
                job.get("progress_at_tick") if type(job.get("progress_at_tick")) is int else row["since_tick"]
            )
            waiting.append(row)
    problems.sort(key=lambda r: -(r["since_tick"] or 0))
    waiting.sort(key=lambda r: r["since_tick"] or 0)
    return dict(
        runtime=str(root),
        end_tick=end_tick,
        controller_errors=errors,
        last_success_tick=max(successes, default=None),
        last_action_tick=max(acted, default=None),
        last_slice_tick=slices[-1]["tick"] if slices else None,
        empty_queue_since_tick=empty_since,
        problems=problems,
        waiting=waiting,
    )


def fleet_medians(milestone_maps, steps) -> dict:
    """``step -> {median, reached, runs}`` over the runs' ``step -> minute`` maps."""
    runs = len(milestone_maps)
    out = {}
    for step in steps:
        values = [m[step] for m in milestone_maps if isinstance(m.get(step), (int, float))]
        if len(values) >= MIN_REACHED_FOR_MEDIAN:
            out[step] = dict(median=round(statistics.median(values), 1), reached=len(values), runs=runs)
    return out


def _margin(median):
    return max(SLOW_MARGIN_MINUTES, SLOW_MARGIN_SHARE * median)


def lateness(milestones, medians, game_minutes) -> list[dict]:
    """Steps this run reached late or missed while the fleet had them, worst first."""
    rows = []
    for step, row in medians.items():
        late_mark = row["median"] + _margin(row["median"])
        minute = milestones.get(step)
        if isinstance(minute, (int, float)):
            if minute > late_mark:
                rows.append(dict(step=step, minute=minute, median=row["median"], late=round(minute - row["median"], 1)))
        elif row["reached"] * 2 >= row["runs"] and isinstance(game_minutes, (int, float)) and game_minutes > late_mark:
            rows.append(dict(step=step, minute=None, median=row["median"], late=round(game_minutes - row["median"], 1)))
    return sorted(rows, key=lambda r: -r["late"])


def classify(facts, *, milestones=None, medians=None, alive=True, waste=None) -> dict:
    """The verdict of one run: ``class``, ``flags``, ``reasons``, the job keys to probe and a keep score."""
    end = facts.get("end_tick")
    minutes = lambda tick: round((end - tick) / TICKS_PER_MINUTE, 1) if end is not None and tick is not None else None  # noqa: E731
    flags, reasons, jobs = [], [], []
    game_minutes = round(end / TICKS_PER_MINUTE, 1) if end is not None else None
    error_jobs = [p for p in facts["problems"] if p["reason"].startswith(ERROR_REASONS)]
    if facts["controller_errors"] >= ERROR_MIN or error_jobs or not alive:
        flags.append("errored")
        if facts["controller_errors"] >= ERROR_MIN:
            reasons.append(f"{facts['controller_errors']} controller errors")
        if not alive:
            reasons.append("run processes were down before the stop")
        for p in error_jobs[:2]:
            reasons.append(f"{p['capability']}: {p['reason'][:90]}")
            jobs.append(p["key"])
    progress = max(
        [t for t in (facts.get("last_success_tick"), facts.get("last_action_tick")) if t is not None], default=None
    )
    idle = minutes(progress) if progress is not None else game_minutes
    quiet = minutes(facts.get("last_slice_tick")) if facts.get("last_slice_tick") is not None else game_minutes
    empty = minutes(facts.get("empty_queue_since_tick"))
    stall = None
    if idle is not None and idle >= STALL_MINUTES:
        stall = f"no action or success for {idle} game min"
    elif quiet is not None and quiet >= STALL_MINUTES:
        stall = f"no slice for {quiet} game min"
    if empty is not None and empty >= STALL_MINUTES:
        stall = (stall + "; " if stall else "") + f"empty queue for {empty} game min"
    if stall:
        flags.append("stalled")
        reasons.append(stall)
        jobs += [w["key"] for w in facts["waiting"][:2]]
    blocked = [
        p
        for p in facts["problems"]
        if (minutes(p["since_tick"]) is None or minutes(p["since_tick"]) >= BLOCKED_MINUTES)
    ]
    if blocked:
        flags.append("blocked")
        for p in blocked[:3]:
            since = minutes(p["since_tick"])
            reasons.append(
                f"{p['capability']} {p['status']} {p['reason'][:100]}"
                + (f" ({since} min)" if since is not None else "")
            )
        jobs += [p["key"] for p in blocked]
    late = lateness(milestones or {}, medians or {}, game_minutes)
    if late:
        flags.append("slow")
        worst = late[0]
        where = "missing" if worst["minute"] is None else "at %s" % worst["minute"]
        reasons.append(f"{worst['step']} {where} vs fleet median {worst['median']} (+{worst['late']} min)")
    verdict_class = next((c for c in CLASS_ORDER if c in flags), "healthy")
    first = (blocked or error_jobs or facts["waiting"] or [None])[0]
    return dict(
        **{"class": verdict_class},
        flags=flags,
        reasons=reasons,
        jobs=list(dict.fromkeys(jobs)),
        blocking=dict(key=first["key"], capability=first["capability"], reason=first["reason"]) if first else None,
        game_minutes=game_minutes,
        idle_minutes=idle,
        late=late[:4],
        reached=len([v for v in (milestones or {}).values() if isinstance(v, (int, float))]),
        waste=waste or {},
        score=keep_score(verdict_class, flags, idle, len(milestones or {})),
    )


def keep_score(verdict_class, flags, idle_minutes, reached) -> float:
    """Higher = worse = kept first: class weight, extra flags, fewer milestones, longer idle."""
    weight = KEEP_WEIGHT.get(verdict_class, 0)
    if weight == 0:
        return 0.0
    extra = sum(KEEP_WEIGHT.get(f, 0) for f in flags if f != verdict_class)
    return round(weight * 1000 + extra * 100 + max(0, 20 - reached) * 10 + min(idle_minutes or 0, 99), 1)


def select_kept(candidates, count) -> list:
    """The ``count`` worst problem runs: ``candidates`` are (seed, verdict) pairs; healthy runs never."""
    rows = [(seed, v) for seed, v in candidates if v and v.get("class") != "healthy"]
    rows.sort(key=lambda sv: (-(sv[1].get("score") or 0), str(sv[0])))
    return [seed for seed, _ in rows[: max(0, int(count))]]


WASTE_KINDS = ("run_stalled", "blocked_unretried", "repeated_block", "long_wait")


def waste_evidence(root) -> dict:
    """The waste report's stall/block detectors for one run (top item each), or {} when it cannot run."""
    try:
        import waste_report
    except Exception:  # noqa: BLE001 - optional evidence
        return {}
    try:
        result = waste_report.analyse(Path(root))
    except Exception as exc:  # a half-written run must not stop the triage
        return {"error": f"{type(exc).__name__}: {exc}"[:160]}
    out = {}
    for kind in WASTE_KINDS:
        row = result["waste"].get(kind)
        if row:
            top = row["top"][0] if row.get("top") else {}
            out[kind] = dict(
                seconds=row["seconds"], count=row["count"], job=top.get("job"), reason=str(top.get("reason"))[:120]
            )
    return out


def summary(verdict) -> dict:
    """The small form stored in the fleet manifest."""
    return {
        "class": verdict.get("class"),
        "flags": verdict.get("flags"),
        "reasons": (verdict.get("reasons") or [])[:3],
        "blocking": verdict.get("blocking"),
        "score": verdict.get("score"),
    }
