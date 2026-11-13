"""Critical-path timeline of a run, for any window of game minutes. Read-only.

Usage: python -B tools/critical_path.py runtime/v2/<name> [--from 0] [--to 30] [--at 10 --at 30] [--json]

The critical steps at each moment come from the same function the scheduler
uses (``factorio_agent.critical_path.critical_path``), with the job states
replayed from ``job-events.jsonl`` and the researched technologies taken from
succeeded research jobs. Printed:

- ``at``: the critical path at the given minutes (frontier, critical steps,
  ready ones);
- per critical job in the window: submitted, first action, end, and its wait
  reasons in game seconds (slice ends in ``logs/controller-events.jsonl``
  where the job reported a wait and made no action since the previous slice);
- actor time: game seconds between consecutive actions, charged to the job
  that acted, split into critical and slack work by capability;
- lab idle: game seconds without science in a lab between the power milestone
  and the window end or the last bootstrap research, from the packs inserted
  into labs and each technology's unit time (catalog), shared over built labs.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from factorio_agent.critical_path import critical_path  # noqa: E402
from factorio_agent.jobs import CAPABILITIES  # noqa: E402
from factorio_agent.progression import ROADMAP, STEP_TECHNOLOGY, _same  # noqa: E402

TICKS = 60
RESEARCH = ("starter_research", "bootstrap_research", "bootstrap_logistics", "research")


def _lines(path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _tick_at(clock, time, event):
    """A terminal event has no tick: its evidence tick, else the last earlier event's tick."""
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    if isinstance(evidence.get("tick"), int):
        return evidence["tick"]
    if not isinstance(time, (int, float)):
        return None
    earlier = [tick for when, tick in clock if when <= time]
    return earlier[-1] if earlier else None


class Run:
    def __init__(self, root: Path):
        self.root = root
        ledger = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
        self.jobs = ledger.get("jobs", {})
        try:
            self.catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.catalog = {}
        self.events = list(_lines(root / "job-events.jsonl"))
        self.slices = [e for e in _lines(root / "logs" / "controller-events.jsonl") if e.get("event") == "slice"]
        self.start = {}
        self.end = {}
        self.first_action = {}
        self.actions = []  # (tick, key, action)
        clock = []  # (wall time, tick) of every event that has both
        for e in self.events:
            if isinstance(e.get("tick"), int) and isinstance(e.get("time"), (int, float)):
                clock.append((e["time"], e["tick"]))
        clock.sort()
        for e in self.events:
            key, tick = e.get("key"), e.get("tick")
            if not isinstance(tick, int):
                tick = _tick_at(clock, e.get("time"), e)
            if not isinstance(tick, int):
                continue
            if e.get("event") == "submitted":
                self.start.setdefault(key, tick)
            elif e.get("event") == "terminal":
                self.end[key] = (tick, e.get("status"), str(e.get("reason") or ""))
            elif e.get("event") == "action_intent":
                self.first_action.setdefault(key, tick)
                self.actions.append((tick, key, e.get("action") or {}))
        for key, job in self.jobs.items():
            if key not in self.start and isinstance(job.get("created_tick"), int):
                self.start[key] = job["created_tick"]

    def capability(self, key):
        return (self.jobs.get(key) or {}).get("capability", "idle" if key == "lane:research" else "?")

    def jobs_at(self, tick):
        rows = []
        for key, job in self.jobs.items():
            if self.start.get(key, 10**12) > tick:
                continue
            end = self.end.get(key)
            status = end[1] if end and end[0] <= tick else "running"
            rows.append(dict(job, key=key, status=status))
        return rows

    def researched(self, jobs):
        names = {"automation-science-pack"}
        for job in jobs:
            if job["status"] != "succeeded" or job.get("capability") not in RESEARCH:
                continue
            technology = (job.get("parameters") or {}).get("technology") or STEP_TECHNOLOGY.get(job["capability"])
            if technology:
                names.add(technology)
        return names

    def observation(self, tick):
        jobs = self.jobs_at(tick)
        researched = self.researched(jobs)
        technologies = self.catalog.get("technologies", {})
        unlocked_by_any = {r for t in technologies.values() for r in t.get("unlocks") or ()}
        enabled = {r for r in self.catalog.get("recipes", {}) if r not in unlocked_by_any}
        for name in researched:
            enabled |= set((technologies.get(name) or {}).get("unlocks") or ())
        return jobs, {"technologies": {t: True for t in researched}, "enabled_recipes": {r: True for r in enabled}}

    def path_at(self, tick):
        jobs, obs = self.observation(tick)
        return critical_path(jobs, obs, CAPABILITIES, catalog=self.catalog or None), jobs

    def critical_keys_at(self, tick):
        path, jobs = self.path_at(tick)
        steps = [s for s in ROADMAP if s.id in set(path.steps)]
        return {j["key"] for j in jobs if j["status"] == "running" and any(_same(j, s) for s in steps)}


def lab_idle(run: Run, lo: int, hi: int) -> dict:
    """Science-free lab seconds from packs inserted into labs (see module docstring)."""
    techs = run.catalog.get("technologies", {})
    labs = 0
    current = None
    queue = 0.0  # lab-seconds of science waiting in the labs
    idle = 0.0
    last = lo
    power = min(
        (
            run.end[k][0]
            for k, j in run.jobs.items()
            if j.get("capability") == "power" and run.end.get(k, (0, ""))[1] == "succeeded"
        ),
        default=None,
    )
    if power is None:
        return {"lab_idle_seconds": None, "reason": "power never succeeded"}
    lo = max(lo, power)
    last = lo
    for tick, _key, action in sorted(run.actions, key=lambda r: r[0]):
        if tick > hi:
            break
        kind = action.get("type")
        if kind == "build" and action.get("name") == "lab":
            labs += 1
        if kind == "research" and action.get("technology"):
            current = action["technology"]
        if tick < lo:
            continue
        span = (tick - last) / TICKS
        work = min(queue, span * max(1, labs))
        queue -= work
        idle += max(0.0, span - work / max(1, labs))
        last = tick
        if kind == "insert" and action.get("name") == "lab" and isinstance(action.get("count"), int):
            unit = (techs.get(current) or {}).get("unit_energy", 600) / TICKS
            queue += action["count"] * unit
    span = (hi - last) / TICKS
    work = min(queue, span * max(1, labs))
    idle += max(0.0, span - work / max(1, labs))
    return {"lab_idle_seconds": round(idle), "from_minute": round(lo / 3600, 1), "labs_built": labs}


def waits(run: Run, key: str, lo: int, hi: int) -> dict:
    out = defaultdict(float)
    previous = None
    acted = set()
    action_ticks = sorted(t for t, k, _ in run.actions if k == key)
    for row in run.slices:
        tick = row.get("tick")
        if not isinstance(tick, int):
            continue
        state = (row.get("jobs") or {}).get(key)
        if previous is not None and state and lo <= tick <= hi:
            moved = any(previous < t <= tick for t in action_ticks)
            if state.get("status") == "waiting" and not moved:
                reason = str(state.get("reason") or "")
                if reason == "execution_slice_complete":
                    reason = "(slice yield)"
                out[reason[:70]] += (tick - previous) / TICKS
            acted.add(moved)
        previous = tick
    return {k: round(v) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def report(root: Path, lo_min: float, hi_min: float, at: list[float]) -> dict:
    run = Run(root)
    lo, hi = int(lo_min * 3600), int(hi_min * 3600)
    moments = {}
    for minute in at:
        path, _ = run.path_at(int(minute * 3600))
        moments[str(minute)] = path.as_dict()
    # critical membership sampled every 10 game seconds
    critical_at = {}
    # on multiples of 600 ticks: the actor loop looks samples up at tick - tick % 600
    # (a window starting off the grid, e.g. --from 18.6, counted every action as slack)
    for tick in range(lo - lo % 600, hi + 1, 600):
        critical_at[tick] = run.critical_keys_at(tick)
    ever = set().union(*critical_at.values()) if critical_at else set()
    jobs = []
    for key in sorted(ever, key=lambda k: run.start.get(k, 0)):
        end = run.end.get(key)
        jobs.append(
            {
                "job": run.capability(key) + _label(run.jobs.get(key, {})),
                "submitted_min": round(run.start.get(key, 0) / 3600, 2),
                "first_action_min": round(run.first_action[key] / 3600, 2) if key in run.first_action else None,
                "end_min": round(end[0] / 3600, 2) if end else None,
                "end": end[1] if end else "open",
                "wait_seconds": waits(run, key, lo, hi),
            }
        )
    actor = {"critical": defaultdict(float), "slack": defaultdict(float)}
    ordered = sorted(run.actions, key=lambda r: r[0])
    for (tick, key, _), nxt in zip(ordered, ordered[1:] + [(hi, None, None)]):
        if not lo <= tick < hi:
            continue
        span = (min(nxt[0], hi) - tick) / TICKS
        sample = critical_at.get(tick - tick % 600, set())
        group = "critical" if key in sample else "slack"
        actor[group][run.capability(key)] += span
    return {
        "runtime": str(root),
        "window_min": [lo_min, hi_min],
        "critical_path_at": moments,
        "critical_jobs": jobs,
        "actor_seconds": {
            g: {k: round(v) for k, v in sorted(d.items(), key=lambda kv: -kv[1])} for g, d in actor.items()
        },
        "lab": lab_idle(run, lo, hi),
    }


def _label(job):
    parameters = job.get("parameters") or {}
    value = parameters.get("technology") or parameters.get("resource") or parameters.get("product") or ""
    return f"[{value}]" if value else ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--from", dest="lo", type=float, default=0.0)
    parser.add_argument("--to", dest="hi", type=float, default=30.0)
    parser.add_argument("--at", action="append", type=float, default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    data = report(args.runtime, args.lo, args.hi, args.at)
    if args.json:
        print(json.dumps(data, indent=1))
        return 0
    print(f"runtime: {data['runtime']}  window: {args.lo}-{args.hi} game min")
    for minute, path in data["critical_path_at"].items():
        print(f"at {minute} min: frontier={path['frontier']} critical={path['steps']} ready={path['ready']}")
    print("critical jobs (min; waits in game s):")
    for row in data["critical_jobs"]:
        print(
            f"  {row['job']:42s} sub {row['submitted_min']:>6} act {row['first_action_min']} end {row['end_min']} {row['end']}"
        )
        for reason, seconds in list(row["wait_seconds"].items())[:4]:
            print(f"      wait {seconds:>5}s  {reason}")
    for group in ("critical", "slack"):
        total = sum(data["actor_seconds"][group].values())
        print(f"actor {group}: {total}s {data['actor_seconds'][group]}")
    print(f"lab: {data['lab']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
