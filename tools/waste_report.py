"""Wasted game time of a run or a fleet, by waste type, ranked by seconds. Read-only.

Usage: python -B tools/waste_report.py runtime/v2/<run> [--json]
       python -B tools/waste_report.py <fleet-name> [--json]      (every runtime/v2/<fleet-name>-<seed>)

Works from the logs every run already writes: ``job-events.jsonl`` (actions
with ticks, submissions, terminals, retries), ``jobs.json`` (capabilities),
``logs/controller-events.jsonl`` (slice ends with each job's status and reason)
and ``catalog.json``. Actor time of one action = game seconds to the next
action, capped at ``SPAN_CAP``. Waste types (seconds are game seconds; types
overlap, they are views, not a partition):

- ``action_loop``: one job taking and inserting the same item on the same
  entity back and forth (>= ``LOOP_MIN`` of each), or a run of >= ``TINY_MIN``
  transfers of the same item/reason with count <= ``TINY_COUNT``.
- ``hand_logistics_during_critical``: fuel/collect/supply-cell/surplus hand
  hauling (``HAND_REASONS``) by any job while a critical job built something in
  the last ``CRITICAL_BUILD_WINDOW`` seconds.
- ``slow_construction``: per job with >= ``BUILD_MIN`` builds, its actor seconds
  above ``TARGET_BUILD_SECONDS`` per placed entity.
- ``lab_idle``: lab seconds without science after power (``critical_path.lab_idle``).
- ``actor_idle``: gaps between actions longer than ``IDLE_GAP`` (the part above
  it), with the waits reported at that time.
- ``run_stalled``: from the last action to the run end, when nothing acted.
- ``long_wait``: a job waiting on one reason without acting for >= ``LONG_WAIT``,
  per (capability, reason); ``decision_wait`` is the subset whose reason is a
  planner's wait decision (``DECISION_WAIT``).
- ``blocked_unretried``: a blocked job never retried nor succeeded, until the run end.
- ``repeated_block``: the same job blocked again with the same reason after a
  retry (seconds blocked between the blocks).
- ``dispatch_latency``: a job submitted more than ``DISPATCH_SLACK`` after the
  last of its ``requires`` capabilities first succeeded (and after its own
  roadmap ``after`` steps; only the requires are checked).
- ``rework``: an entity built and then mined at the same position (any job)
  within ``REWORK_WINDOW`` (both actions' actor seconds).

Not measurable from current logs (need a new log field): supply counted that
cannot reach the consumer (needs per-machine ingredient status over time in the
slice record), wait decisions vs another receipt's ``seconds_saved`` (needs the
receipts logged per slice), planning seconds burnt on failed candidates (needs
per-planner wall seconds in the slice record).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import importlib.util
import json
from pathlib import Path
import re
import statistics

TICKS = 60
SPAN_CAP = 60.0
LOOP_MIN = 3
TINY_COUNT = 2
TINY_MIN = 5
HAND_REASONS = (
    "fuel starter production",
    "collect live construction stocks",
    "collect produced",
    "feed the supply cell",
    "hand-fed boiler",
    "put carried surplus",
    "refuel",
)
CRITICAL_BUILD_WINDOW = 120
BUILD_MIN = 20
TARGET_BUILD_SECONDS = 3.0
IDLE_GAP = 10.0
LONG_WAIT = 120.0
DISPATCH_SLACK = 60.0
REWORK_WINDOW = 600.0
TOP = 5
DECISION_WAIT = re.compile(r"as fast|are idle|is idle|covered|no faster|not faster|wait is shorter", re.I)
ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "v2"

_spec = importlib.util.spec_from_file_location("critical_path_tool", Path(__file__).with_name("critical_path.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _hand(action) -> bool:
    reason = str(action.get("reason") or "")
    return any(reason.startswith(h) for h in HAND_REASONS)


def _pos(action):
    p = action.get("position")
    if isinstance(p, dict):
        return (p.get("x"), p.get("y"))
    return None


class Waste:
    def __init__(self, seed):
        self.seed = seed
        self.rows = defaultdict(lambda: {"seconds": 0.0, "count": 0, "items": []})

    def add(self, kind, seconds, example, count=1):
        row = self.rows[kind]
        row["seconds"] += seconds
        row["count"] += count
        row["items"].append(dict(example, seed=self.seed, seconds=round(seconds)))

    def as_dict(self):
        out = {}
        for kind, row in sorted(self.rows.items(), key=lambda kv: -kv[1]["seconds"]):
            items = sorted(row["items"], key=lambda item: -item["seconds"])
            out[kind] = {
                "seconds": round(row["seconds"]),
                "count": row["count"],
                "example": items[0],
                "top": items[:TOP],
                "all": items,
            }
        return out


def _shape(reason) -> str:
    """A reason without numbers and positions, so equal causes group across runs."""
    text = re.sub(r"\(.*?\)", "()", str(reason))
    return re.sub(r"\d+(\.\d+)?", "#", text)[:80]


def _end_tick(run, root):
    ticks = [t for t, _k, _a in run.actions] + [s["tick"] for s in run.slices if isinstance(s.get("tick"), int)]
    ticks += [e["tick"] for e in run.events if isinstance(e.get("tick"), int)]
    return max(ticks) if ticks else 0


def analyse(root: Path) -> dict:
    run = cp.Run(root)
    seed = root.name.rsplit("-", 1)[-1]
    w = Waste(seed)
    end = _end_tick(run, root)
    actions = sorted(run.actions, key=lambda r: r[0])
    spans = []
    for (tick, key, action), nxt in zip(actions, actions[1:] + [(end, None, None)]):
        spans.append(min(SPAN_CAP, max(0.0, (nxt[0] - tick) / TICKS)))
    cap = run.capability

    # --- action loops -------------------------------------------------------
    per_target = defaultdict(list)  # (key, item, entity, position) -> [(i, type)]
    for i, (tick, key, a) in enumerate(actions):
        if a.get("type") in ("take", "take_many", "insert") and a.get("item"):
            per_target[(key, a.get("item"), a.get("name"), _pos(a))].append((i, a["type"]))
    looped = set()
    for (key, item, name, pos), rows in per_target.items():
        kinds = [("in" if t == "insert" else "out") for _i, t in rows]
        flips = sum(1 for a, b in zip(kinds, kinds[1:]) if a != b)
        if kinds.count("in") >= LOOP_MIN and kinds.count("out") >= LOOP_MIN and flips >= 2 * LOOP_MIN - 1:
            idx = [i for i, _t in rows]
            looped.update(idx)
            seconds = sum(spans[i] for i in idx)
            tick = actions[idx[0]][0]
            w.add(
                "action_loop",
                seconds,
                {"tick": tick, "job": cap(key), "reason": f"take/insert {item} on {name} {pos} x{len(idx)}"},
                len(idx),
            )
    tiny = defaultdict(list)
    for i, (tick, key, a) in enumerate(actions):
        count = a.get("count")
        if i in looped or a.get("type") not in ("insert", "take", "take_many") or not isinstance(count, int):
            continue
        if count <= TINY_COUNT:
            tiny[(key, a.get("type"), a.get("item"), str(a.get("reason"))[:60])].append(i)
    for (key, kind, item, reason), idx in tiny.items():
        if len(idx) >= TINY_MIN:
            w.add(
                "action_loop",
                sum(spans[i] for i in idx),
                {
                    "tick": actions[idx[0]][0],
                    "job": cap(key),
                    "reason": f"{len(idx)}x {kind} {item} count<={TINY_COUNT}: {reason}",
                },
                len(idx),
            )

    # --- critical membership (10 s samples, the critical_path tool's rule) -----
    critical_at = {}
    for tick in range(0, end + 1, 600):
        critical_at[tick] = run.critical_keys_at(tick)
    last_critical_build = None
    for i, (tick, key, a) in enumerate(actions):
        crit = critical_at.get(tick - tick % 600, set())
        if key in crit and a.get("type") == "build":
            last_critical_build = (tick, key)
        reason = str(a.get("reason") or "")
        if _hand(a) and last_critical_build:
            if (tick - last_critical_build[0]) / TICKS <= CRITICAL_BUILD_WINDOW and key not in crit:
                w.add(
                    "hand_logistics_during_critical",
                    spans[i],
                    {
                        "tick": tick,
                        "job": cap(key),
                        "reason": f"{reason[:60]} while {cap(last_critical_build[1])} builds",
                    },
                )

    # --- slow construction ------------------------------------------------------
    per_job = defaultdict(lambda: [0, 0.0, None])
    for i, (tick, key, a) in enumerate(actions):
        row = per_job[key]
        row[1] += spans[i]
        if a.get("type") == "build":
            row[0] += 1
            row[2] = row[2] or tick
    for key, (builds, seconds, first) in per_job.items():
        if builds >= BUILD_MIN:
            lost = seconds - builds * TARGET_BUILD_SECONDS
            if lost > 0:
                rate = builds / max(seconds / 60.0, 1e-9)
                w.add(
                    "slow_construction",
                    lost,
                    {
                        "tick": first,
                        "job": cap(key),
                        "reason": f"{builds} entities in {seconds:.0f} actor-s ({rate:.1f}/actor-min)",
                    },
                )

    # --- lab idle -----------------------------------------------------------
    lab = cp.lab_idle(run, 0, end)
    if lab.get("lab_idle_seconds"):
        w.add(
            "lab_idle",
            lab["lab_idle_seconds"],
            {"tick": int(lab["from_minute"] * 3600), "job": "lab", "reason": f"{lab['labs_built']} labs built"},
        )

    # --- slices: statuses over time -------------------------------------------
    slices = [s for s in run.slices if isinstance(s.get("tick"), int)]

    def waits_at(tick):
        best = None
        for s in slices:
            if s["tick"] >= tick:
                best = s
                break
        if best is None:
            return ""
        rs = [
            f"{cap(k)}: {str(v.get('reason'))[:50]}"
            for k, v in (best.get("jobs") or {}).items()
            if v.get("status") in ("waiting", "blocked")
        ]
        return "; ".join(rs[:3])

    # --- actor idle and stalled tail ----------------------------------------------
    for (tick, key, a), nxt in zip(actions, actions[1:]):
        gap = (nxt[0] - tick) / TICKS
        if gap > IDLE_GAP:
            w.add("actor_idle", gap - IDLE_GAP, {"tick": tick, "job": cap(key), "reason": waits_at(tick)})
    if actions:
        tail = (end - actions[-1][0]) / TICKS
        if tail > SPAN_CAP:
            w.add(
                "run_stalled",
                tail,
                {"tick": actions[-1][0], "job": "-", "reason": waits_at(actions[-1][0] + 1) or "no slice"},
            )

    # --- long waits per (capability, reason) ------------------------------------
    # Upkeep (fuel, collect, feed) by a waiting job is not progress on what it waits for.
    action_ticks = defaultdict(list)
    for tick, key, a in actions:
        if not _hand(a):
            action_ticks[key].append(tick)
    open_wait = {}  # key -> (reason, start tick, last tick)

    def close(key):
        reason, start, last = open_wait.pop(key)
        seconds = (last - start) / TICKS
        if seconds >= LONG_WAIT:
            kind = "decision_wait" if DECISION_WAIT.search(reason) else "long_wait"
            w.add(kind, seconds, {"tick": start, "job": cap(key), "reason": reason[:110]})

    previous = None
    for s in slices:
        tick = s["tick"]
        for key, st in (s.get("jobs") or {}).items():
            moved = previous is not None and any(previous < t <= tick for t in action_ticks.get(key, ()))
            reason = str(st.get("reason") or "")
            if reason == "execution_slice_complete" and key in open_wait and not moved:
                reason = open_wait[key][0]  # a slice yield inside a wait continues it
            waiting = st.get("status") == "waiting" and reason != "execution_slice_complete" and not moved
            if key in open_wait and (not waiting or open_wait[key][0] != reason):
                close(key)
            if waiting:
                if key in open_wait:
                    open_wait[key] = (reason, open_wait[key][1], tick)
                else:
                    open_wait[key] = (reason, previous if previous is not None else tick, tick)
        for key in [k for k in open_wait if k not in (s.get("jobs") or {})]:
            close(key)
        previous = tick
    for key in list(open_wait):
        close(key)

    # --- blocks, retries, dispatch ------------------------------------------------
    history = defaultdict(list)
    for e in run.events:
        if e.get("event") in ("terminal", "retry", "submitted"):
            tick = e.get("tick") if isinstance(e.get("tick"), int) else cp._tick_at([], None, e)
            if not isinstance(tick, int):
                ev = e.get("evidence") if isinstance(e.get("evidence"), dict) else {}
                tick = ev.get("tick") if isinstance(ev.get("tick"), int) else None
            history[e.get("key")].append((e["event"], tick, e.get("status"), str(e.get("reason") or "")))
    # A terminal without a tick: the next slice that no longer lists the job running is close enough;
    # use the first slice where the job is blocked.
    first_blocked = defaultdict(list)
    for s in slices:
        for key, st in (s.get("jobs") or {}).items():
            if st.get("status") == "blocked":
                first_blocked[key].append(s["tick"])
    for key, rows in history.items():
        blocks = [r for r in rows if r[0] == "terminal" and r[2] in ("blocked", "failed")]
        if not blocks:
            continue
        last = rows[-1]
        if last[0] == "terminal" and last[2] in ("blocked", "failed"):
            tick = last[1] or (first_blocked.get(key) or [None])[-1]
            retries = [r[1] for r in rows if r[0] == "retry" and r[1]]
            start = max([t for t in [tick] + retries if t] or [end])
            seconds = (end - start) / TICKS
            if seconds > 0:
                w.add("blocked_unretried", seconds, {"tick": start, "job": cap(key), "reason": last[3][:110]})
        reasons = [r[3] for r in blocks]
        for reason in set(reasons):
            n = reasons.count(reason)
            if n >= 2:
                ticks = sorted(set(first_blocked.get(key, [])))
                retries = [r[1] for r in rows if r[0] == "retry" and r[1]]
                blocked_s = 0.0
                if ticks and retries:
                    blocked_s = (max(retries) - min(ticks)) / TICKS
                w.add(
                    "repeated_block",
                    max(0.0, blocked_s),
                    {"tick": min(ticks) if ticks else None, "job": cap(key), "reason": f"{n}x {reason[:100]}"},
                    n,
                )

    # Dispatch latency of on-path roadmap steps only (a fixed priority keeps a step off the path on purpose):
    # ready = the later of every `requires` capability's and every `after` step's first success.
    first_success = {}
    step_done = {}
    for key, (tick, status, _r) in run.end.items():
        if status != "succeeded":
            continue
        c = cap(key)
        first_success[c] = min(first_success.get(c, tick), tick)
        job = dict(run.jobs.get(key) or {})
        for step in cp.ROADMAP:
            if cp._same(job, step):
                step_done[step.id] = min(step_done.get(step.id, tick), tick)
    for step in cp.ROADMAP:
        if step.priority is not None or step.gate:
            continue
        keys = [k for k in run.start if cp._same(dict(run.jobs.get(k) or {}), step)]
        if not keys:
            continue
        submitted = min(run.start[k] for k in keys)
        requires = (cp.CAPABILITIES.get(step.capability) or {}).get("requires") or []
        ready = [first_success.get(r) for r in requires] + [step_done.get(a) for a in step.after]
        if not ready or any(t is None for t in ready):
            continue
        latency = (submitted - max(ready)) / TICKS
        if latency > DISPATCH_SLACK:
            names = ", ".join(list(requires) + list(step.after))
            w.add(
                "dispatch_latency",
                latency - DISPATCH_SLACK,
                {
                    "tick": max(ready),
                    "job": step.id,
                    "reason": f"submitted {latency:.0f}s after {names} were done (research gates not checked)",
                },
            )

    # --- rework --------------------------------------------------------------
    built = {}
    for i, (tick, key, a) in enumerate(actions):
        pos = _pos(a)
        if pos is None:
            continue
        if a.get("type") == "build":
            built[(a.get("name"), pos)] = (tick, i, key)
        elif a.get("type") == "mine" and (a.get("name"), pos) in built:
            btick, bi, bkey = built.pop((a.get("name"), pos))
            if (tick - btick) / TICKS <= REWORK_WINDOW:
                w.add(
                    "rework",
                    spans[bi] + spans[i],
                    {
                        "tick": btick,
                        "job": cap(bkey),
                        "reason": f"{a.get('name')} at {pos} built, mined {(tick - btick) / TICKS:.0f}s later by {cap(key)}: {str(a.get('reason'))[:60]}",
                    },
                )

    return {"run": str(root), "seed": seed, "end_minute": round(end / 3600, 1), "waste": w.as_dict()}


def runs_for(target: str, runtime: Path = RUNTIME) -> list[Path]:
    path = Path(target)
    if (path / "jobs.json").exists():
        return [path]
    found = sorted(p for p in Path(runtime).glob(f"{target}-*") if (p / "jobs.json").exists())
    if not found:
        raise SystemExit(f"no run or fleet named {target}")
    return found


def aggregate(results: list[dict]) -> dict:
    kinds = sorted({k for r in results for k in r["waste"]})
    out = {}
    for kind in kinds:
        values = [r["waste"].get(kind, {}).get("seconds", 0) for r in results]
        rows = [r["waste"][kind] for r in results if kind in r["waste"]]
        example = max(rows, key=lambda row: row["example"]["seconds"] if row["example"] else 0)["example"]
        groups = defaultdict(lambda: [0, 0, set()])
        for row in rows:
            for item in row["all"]:
                group = groups[(item["job"], _shape(item["reason"]))]
                group[0] += item["seconds"]
                group[1] += 1
                group[2].add(item["seed"])
        causes = sorted(groups.items(), key=lambda kv: -kv[1][0])[:TOP]
        out[kind] = {
            "causes": [
                {"job": job, "reason": reason, "seconds": s, "count": n, "runs": len(seeds)}
                for (job, reason), (s, n, seeds) in causes
            ],
            "total_seconds": sum(values),
            "median_seconds": statistics.median(values),
            "max_seconds": max(values),
            "runs": len(rows),
            "count": sum(row["count"] for row in rows),
            "example": example,
        }
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["total_seconds"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", help="a runtime directory or a fleet name")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--runtime", type=Path, default=RUNTIME, help="folder holding the fleet's runs")
    args = parser.parse_args(argv)
    results = [analyse(root) for root in runs_for(args.target, args.runtime)]
    total = aggregate(results)
    for result in results:
        for row in result["waste"].values():
            row.pop("all", None)
    if args.json:
        print(json.dumps({"runs": results, "aggregate": total}, indent=1))
        return 0
    print(f"waste in {len(results)} run(s) (game seconds; types overlap)")
    print(f"{'type':32s} {'total':>7s} {'median':>7s} {'max':>6s} {'runs':>4s} {'count':>6s}  example")
    for kind, row in total.items():
        ex = row["example"] or {}
        print(
            f"{kind:32s} {row['total_seconds']:>7.0f} {row['median_seconds']:>7.0f} {row['max_seconds']:>6.0f} "
            f"{row['runs']:>4d} {row['count']:>6d}  {ex.get('seed')}@{round((ex.get('tick') or 0) / 3600, 1)}m "
            f"{ex.get('job')}: {str(ex.get('reason'))[:90]}"
        )
        for cause in row["causes"][:3]:
            print(
                f"{'':34s}{cause['seconds']:>6}s {cause['runs']:>2} runs x{cause['count']:<4} {cause['job']}: {cause['reason']}"
            )
    if len(results) > 1:
        return 0
    print("top items per type:")
    for kind, row in results[0]["waste"].items():
        print(f"  {kind} {row['seconds']}s x{row['count']}")
        for item in row["top"]:
            print(
                f"      {item['seconds']:>5}s @{round((item.get('tick') or 0) / 3600, 1)}m {item['job']}: {str(item['reason'])[:110]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
