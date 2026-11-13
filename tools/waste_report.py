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
- ``actor_death``: per actor death (``factorio_agent.actor_death.deaths``), the
  seconds from the death to its recovery (respawn, then the corpse items back or
  given up), or to the run end when it was never recovered; the example names
  the cause seen at the corpse and the items lost.
- ``idle_with_work``: the supervisor's idle watchdog (``logs/controller-events.jsonl``
  event ``idle_with_work``, ``bottleneck_audit``): actor seconds without an action
  beyond ``IDLE_SECONDS`` while a bottleneck, an untried remedy or ready queue work
  existed; the row names the top item and the gates that held it.

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


def postmortem_cause(root, key) -> dict:
    """``cause`` (+ ``cause_file``) of job ``key`` from the run's newest post-mortem probe, or {}.

    A blocked row then names the tile, entity or constraint the dry-run planner
    met (``factorio_agent.postmortem.cause_of``) instead of only its reason."""
    try:
        from factorio_agent.postmortem import probe_cause
    except ImportError:
        return {}
    found = probe_cause(root, key)
    if not found or not found.get("summary"):
        return {}
    return {"cause": str(found["summary"])[:200], "cause_file": found.get("file")}


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
                item = {"tick": start, "job": cap(key), "reason": last[3][:110]}
                item.update(postmortem_cause(root, key))
                w.add("blocked_unretried", seconds, item)
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

    # --- idle with work: the supervisor's idle watchdog (bottleneck_audit) ------------------------
    for episode in idle_with_work(root):
        gates = sorted(episode["gates"].items(), key=lambda kv: -kv[1])
        w.add(
            "idle_with_work",
            episode["seconds"],
            {
                "tick": episode["since_tick"],
                "job": gates[0][0] if gates else "-",
                "reason": f"{episode.get('top_item') or '-'}; gates "
                + ", ".join(f"{g} {round(s)}s" for g, s in gates[:3]),
            },
        )

    # --- remedy ladder: seconds blocked per needs kind, remedies applied and resolved ---------
    remedies = remedy_summary(run.events, end, w, cap)

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

    actor_death_waste(root, end, w)

    return {
        "run": str(root),
        "seed": seed,
        "end_minute": round(end / 3600, 1),
        "waste": w.as_dict(),
        "remedies": remedies,
        "critical_chain": critical_chain(root, run),
    }


def actor_death_waste(root, end, w) -> list[dict]:
    """``actor_death`` rows: game seconds from each death to its recovery (respawn, then the corpse items
    back or given up), or to the run end when never recovered; ``items_lost`` = corpse items not recovered."""
    try:
        from factorio_agent.actor_death import deaths
    except ImportError:
        return []
    rows = deaths(root, end_tick=end or None)
    for d in rows:
        lost = d.get("items_lost")
        w.add(
            "actor_death",
            max(0.0, d.get("lost_seconds") or 0.0),
            {
                "tick": d["death_tick"],
                "job": "actor",
                "reason": f"{d.get('cause')}; belongings {d.get('belongings') or '-'}"
                + (f", {lost} items lost" if lost else "")
                + ("" if d.get("recovered_tick") is not None else ", never recovered"),
                "items_lost": lost,
            },
        )
    return rows


# --- critical chain ---------------------------------------------------------------------
# Segments of the chain to the owner's 20-minute target (smelting district + main bus), each with
# the constraint that bound it. Thresholds, documented here:
# - research: lab-limited when elapsed <= LAB_BOUND_SLACK x the catalog lab time on the labs built
#   when the drill research started; fewer than 2 labs is named (owner: 2 labs first).
# - producer: the district's first bank (every plan entity before the second bank's first, the
#   producer-first prefix) is complete; its last entity names the wait; a splitter/underground
#   finished within RESEARCH_GATE_MINUTES of the logistics research is the research.
# - district / bus: plate-bound when at least PLATE_SHORT_SHARE of the iron takes in the segment
#   moved less than they asked for (the starter furnaces were empty).
#   Not gated: "admitted after logistics" when the district job was submitted within
#   ADMITTED_SLACK_TICKS of the logistics end (its slot came only then), else "plates" when at least
#   PLATE_SHORT_SHARE of its slice reasons after logistics wait for a plate, else "waits for <entity>".
LAB_BOUND_SLACK = 1.25
RESEARCH_GATE_MINUTES = 1.0
ADMITTED_SLACK_TICKS = 600
PLATE_SHORT_SHARE = 0.4
CHAIN_RESEARCH = ("electric-mining-drill", "logistics")


def _job_end(run, capability, **parameters):
    ticks = [
        run.end[key][0]
        for key, job in run.jobs.items()
        if job.get("capability") == capability
        and run.end.get(key, (0, ""))[1] == "succeeded"
        and all((job.get("parameters") or {}).get(k) == v for k, v in parameters.items())
    ]
    return min(ticks) if ticks else None


def _built_ticks(run):
    out = {}
    for tick, _key, a in sorted(run.actions, key=lambda r: r[0]):
        rows = a.get("actions") if a.get("type") == "build_many" else [a] if a.get("type") == "build" else []
        for b in rows or ():
            p = b.get("position") or {}
            out.setdefault((b.get("name"), p.get("x"), p.get("y")), tick)
    return out


def _iron_short_share(run, lo, hi):
    """Share of iron take/take_many in [lo, hi) that moved less than requested (None without takes)."""
    pending, rows = {}, []
    for e in run.events:
        if e.get("event") == "action_intent":
            pending[e.get("key")] = e
        elif e.get("event") == "action_result" and e.get("key") in pending:
            intent = pending.pop(e.get("key"))
            a, r = intent.get("action") or {}, e.get("result") or {}
            tick = intent.get("tick")
            if a.get("type") in ("take", "take_many") and a.get("item") == "iron-plate" and isinstance(tick, int):
                if lo <= tick < hi and isinstance(a.get("count"), int):
                    rows.append((r.get("moved") or 0) < a["count"])
    return round(sum(rows) / len(rows), 2) if rows else None


def _plate_wait_share(run, capability, lo, hi):
    """Share of ``capability``'s slice reasons in [lo, hi] that wait for a plate (0.0 without slices)."""
    if lo is None or hi is None:
        return 0.0
    keys = {k for k, j in run.jobs.items() if j.get("capability") == capability}
    reasons = [
        str(row.get("reason") or "")
        for s in run.slices
        if lo <= s.get("tick", -1) <= hi
        for k, row in (s.get("jobs") or {}).items()
        if k in keys and isinstance(row, dict)
    ]
    plates = sum(1 for r in reasons if re.search(r"waiting for .*-plate", r))
    return plates / len(reasons) if reasons else 0.0


def _first_bank_prefix(root):
    try:
        state = json.loads((root / "smelting-district.json").read_text(encoding="utf-8"))
        entities = state["plan"]["entities"]
        banks = state.get("banks") or []
    except (OSError, ValueError, KeyError, TypeError):
        return None
    first = next((b.get("resource") for b in banks), None)
    later = {
        (e["name"], e["position"]["x"], e["position"]["y"])
        for b in banks
        if b.get("resource") != first
        for e in b.get("entities") or ()
    }
    prefix = []
    for e in entities:
        key = (e["name"], e["position"]["x"], e["position"]["y"])
        if key in later:
            break
        prefix.append(key)
    return prefix, {(e["name"], e["position"]["x"], e["position"]["y"]) for e in entities}


def critical_chain(root, run=None) -> list[dict]:
    """The chain power -> research -> outposts -> district producer -> district -> bus of one run.

    One row per segment: ``segment``, ``start``/``end`` (game minutes, None when not reached),
    ``minutes``, ``binding`` (the constraint, rules above) and ``evidence``.
    """
    root = Path(root)
    run = run or cp.Run(root)
    techs = run.catalog.get("technologies", {})
    m = lambda tick: None if tick is None else round(tick / 3600, 1)  # noqa: E731
    power = _job_end(run, "power")
    research_ticks = {}
    labs = 0
    for tick, _key, a in sorted(run.actions, key=lambda r: r[0]):
        if a.get("type") == "build" and a.get("name") == "lab":
            labs += 1
        rows = a.get("actions") if a.get("type") == "build_many" else []
        labs += sum(1 for b in rows or () if b.get("name") == "lab")
        if a.get("type") == "research" and a.get("technology") in CHAIN_RESEARCH:
            research_ticks.setdefault(a["technology"], (tick, labs))
    drill_start = research_ticks.get(CHAIN_RESEARCH[0])
    drill = _job_end(run, "bootstrap_research", technology=CHAIN_RESEARCH[0]) or _job_end(
        run, "starter_research", technology=CHAIN_RESEARCH[0]
    )
    logistics = _job_end(run, "bootstrap_logistics")
    iron = _job_end(run, "mining_outpost", resource="iron-ore")
    district = _job_end(run, "smelting_district")
    bus = _job_end(run, "planned_main_bus")
    rows = [{"segment": "power", "start": 0.0, "end": m(power), "binding": "starter plates", "evidence": {}}]
    lab_count = drill_start[1] if drill_start else None
    work = sum(
        (techs.get(t) or {}).get("unit_count", 0) * (techs.get(t) or {}).get("unit_energy", 0) / 60
        for t in CHAIN_RESEARCH
    )
    research = {"segment": "research", "start": m(drill_start[0]) if drill_start else None, "end": m(logistics)}
    if drill_start and logistics and lab_count:
        elapsed = (logistics - drill_start[0]) / 60
        bound = work / lab_count
        if lab_count < 2:
            binding = f"labs: {lab_count} lab"
        elif elapsed <= LAB_BOUND_SLACK * bound:
            binding = f"lab time ({lab_count} labs)"
        else:
            binding = "pack supply / funding"
        research["binding"] = binding
        research["evidence"] = {"labs": lab_count, "lab_bound_s": round(bound), "elapsed_s": round(elapsed)}
    rows.append(research)
    rows.append({"segment": "outposts", "start": m(drill), "end": m(iron), "binding": "drill research + build"})
    producer = {"segment": "producer", "start": None, "end": None}
    found = _first_bank_prefix(root)
    built = _built_ticks(run)
    prefix_end = None
    if found:
        prefix, every = found
        district_builds = [t for k, t in built.items() if k in every]
        producer["start"] = m(min(district_builds)) if district_builds else None
        if prefix and all(k in built for k in prefix):
            last = max(prefix, key=lambda k: built[k])
            prefix_end = built[last]
            producer["end"] = m(prefix_end)
            gated = (
                last[0] in ("splitter", "underground-belt")
                and logistics is not None
                and prefix_end - logistics <= RESEARCH_GATE_MINUTES * 3600
            )
            admitted = min(
                (
                    run.start[k]
                    for k, j in run.jobs.items()
                    if j.get("capability") == "smelting_district" and k in run.start
                ),
                default=None,
            )
            late = sum(1 for k in prefix if logistics is not None and built[k] > logistics)
            if gated:
                producer["binding"] = "logistics research"
            elif logistics is not None and admitted is not None and admitted >= logistics - ADMITTED_SLACK_TICKS:
                # The job got its slot only when logistics ended; the prefix was built after it.
                producer["binding"] = "admitted after logistics"
            elif _plate_wait_share(run, "smelting_district", logistics, prefix_end) >= PLATE_SHORT_SHARE:
                producer["binding"] = "plates"
            else:
                producer["binding"] = f"waits for {last[0]}"
            producer["evidence"] = {
                "last_entity": last[0],
                "prefix_entities": len(prefix),
                "built_after_logistics": late,
                "admitted": m(admitted),
            }
    rows.append(producer)
    for name, lo, hi in (("district", prefix_end, district), ("bus", district, bus)):
        row = {"segment": name, "start": m(lo), "end": m(hi)}
        if lo is not None and hi is not None:
            share = _iron_short_share(run, lo, hi)
            row["binding"] = "plates" if share is not None and share >= PLATE_SHORT_SHARE else "not plates"
            row["evidence"] = {"short_take_share": share}
        rows.append(row)
    for row in rows:
        row.setdefault("binding", "not reached")
        row.setdefault("evidence", {})
        start, end = row.get("start"), row.get("end")
        row["minutes"] = round(end - start, 1) if start is not None and end is not None else None
    return rows


# --- plate-supply bound -------------------------------------------------------------------
# The earliest minute the run's own iron supply could have paid what the run spent on iron up to
# a step (district, bus): actual near the bound = plate-bound, far above it = rule-bound (plates
# stood ready while the step waited on something else). Supply capacity, an upper bound (so the
# minute is a lower bound): every iron starter cell from its burner drill's build (the build reason
# names "iron-ore"), STARTER_CELL_RATE plates/min, plus every district/smelting_capacity iron
# furnace from its build, but not before the district's producer prefix is complete (its feed head
# carries the outposts' ore), DISTRICT_FURNACE_RATE plates/min. The bill: iron-plate taken
# (moved) by any job up to the step (game production counters are not kept for the whole run).
STARTER_CELL_RATE = 15.0  # burner mining drill 0.25 ore/s
DISTRICT_FURNACE_RATE = 18.75  # stone furnace 0.3125 plates/s
PLATE_BOUND_STEPS = (("district", "smelting_district"), ("bus", "planned_main_bus"))


def _iron_supply_starts(run, root, prefix_end):
    """``[(tick, plates per minute)]``: each iron starter cell and each iron district furnace coming online."""
    starts = []
    for tick, _key, a in sorted(run.actions, key=lambda r: r[0]):
        if a.get("type") == "build" and a.get("name") == "burner-mining-drill" and "iron-ore" in str(a.get("reason")):
            starts.append((tick, STARTER_CELL_RATE))
    try:
        state = json.loads((Path(root) / "smelting-district.json").read_text(encoding="utf-8"))
        banks = state.get("banks") or []
    except (OSError, ValueError, TypeError):
        banks = []
    iron = {
        (e["name"], e["position"]["x"], e["position"]["y"])
        for b in banks
        if b.get("resource") == "iron-ore"
        for e in b.get("entities") or ()
        if e.get("name") in ("stone-furnace", "steel-furnace")
    }
    if prefix_end is not None:
        for key, tick in _built_ticks(run).items():
            if key in iron:
                starts.append((max(tick, prefix_end), DISTRICT_FURNACE_RATE))
    return sorted(starts)


def _iron_taken(run):
    """``[(tick, plates moved)]`` of every iron-plate take/take_many."""
    pending, rows = {}, []
    for e in run.events:
        if e.get("event") == "action_intent":
            pending[e.get("key")] = e
        elif e.get("event") == "action_result" and e.get("key") in pending:
            intent = pending.pop(e.get("key"))
            a, r = intent.get("action") or {}, e.get("result") or {}
            if a.get("type") in ("take", "take_many") and a.get("item") == "iron-plate":
                moved = r.get("moved")
                if isinstance(intent.get("tick"), int) and isinstance(moved, (int, float)) and moved > 0:
                    rows.append((intent["tick"], moved))
    return sorted(rows)


def _bound_tick(starts, bill):
    """First tick at which the cumulative supply of ``starts`` reaches ``bill`` plates (None: never)."""
    supplied, rate, last = 0.0, 0.0, 0
    for tick, add in starts:
        if rate > 0 and supplied + rate * (tick - last) / 3600 >= bill:
            break
        supplied += rate * (tick - last) / 3600
        rate, last = rate + add, tick
    if rate <= 0:
        return None if bill > 0 else 0
    return int(last + max(0.0, bill - supplied) / rate * 3600)


def plate_supply_bound(root, run=None) -> dict:
    """Per PLATE_BOUND_STEPS segment: ``bound`` and ``actual`` game minutes and the iron ``bill`` (rules above)."""
    root = Path(root)
    run = run or cp.Run(root)
    found = _first_bank_prefix(root)
    built = _built_ticks(run)
    prefix_end = None
    if found and found[0] and all(k in built for k in found[0]):
        prefix_end = max(built[k] for k in found[0])
    starts = _iron_supply_starts(run, root, prefix_end)
    taken = _iron_taken(run)
    out = {}
    for name, capability in PLATE_BOUND_STEPS:
        end = _job_end(run, capability)
        if end is None:
            continue
        bill = sum(moved for tick, moved in taken if tick <= end)
        bound = _bound_tick(starts, bill)
        out[name] = {
            "actual": round(end / 3600, 1),
            "bound": None if bound is None else round(bound / 3600, 1),
            "bill": round(bill),
        }
    return out


def _text(value) -> str:
    return "-" if value is None else str(value)


def chain_totals(results) -> list[dict]:
    """Fleet view of ``critical_chain``: per segment median end, median minutes, binding counts."""
    by = defaultdict(list)
    for result in results:
        for row in result.get("critical_chain") or ():
            by[row["segment"]].append(row)
    out = []
    for segment, rows in by.items():
        ends = [r["end"] for r in rows if r.get("end") is not None]
        spans = [r["minutes"] for r in rows if r.get("minutes") is not None]
        counts = defaultdict(int)
        for r in rows:
            counts[r["binding"]] += 1
        out.append(
            {
                "segment": segment,
                "median_end": round(statistics.median(ends), 1) if ends else None,
                "median_minutes": round(statistics.median(spans), 1) if spans else None,
                "reached": len(ends),
                "runs": len(rows),
                "binding": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            }
        )
    return out


def idle_with_work(root) -> list[dict]:
    """Idle episodes of the supervisor's ``idle_with_work`` events (``bottleneck_audit.idle_episodes``)."""
    try:
        from factorio_agent.bottleneck_audit import _events, idle_episodes
    except ImportError:
        return []
    return idle_episodes(_events(root))


def remedy_summary(events, end, w=None, cap=str) -> dict:
    """Blocked seconds per ``needs`` kind (``blocked_by_needs`` waste rows) and per remedy rung how often it
    was applied and how often the job's next terminal after it was a success (``resolved``).

    A block with needs lasts from its terminal event to the job's next retry, or to ``end``. Rungs come
    from ``remedy_applied`` events (``jobs.control`` retry with a remedy), checks from ``remedy_checked``.
    """
    by_key = defaultdict(list)
    for e in events:
        if e.get("event") in ("terminal", "retry", "remedy_applied", "remedy_checked"):
            by_key[e.get("key")].append(e)
    rungs = defaultdict(lambda: {"checked": 0, "fitted": 0, "applied": 0, "resolved": 0})
    kinds = defaultdict(lambda: {"seconds": 0.0, "blocks": 0})
    for key, rows in by_key.items():
        open_block = None
        pending = []
        for e in rows:
            tick = e.get("tick") if isinstance(e.get("tick"), int) else None
            if e["event"] == "remedy_checked":
                row = rungs[str(e.get("rung"))]
                row["checked"] += 1
                row["fitted"] += bool(e.get("fits"))
            elif e["event"] == "remedy_applied":
                rungs[str(e.get("rung"))]["applied"] += 1
                pending.append(str(e.get("rung")))
            elif e["event"] == "retry" and open_block is not None:
                kind, start, reason = open_block
                if tick is not None and start is not None:
                    kinds[kind]["seconds"] += max(0, tick - start) / TICKS
                open_block = None
            elif e["event"] == "terminal":
                if e.get("status") == "succeeded":
                    for rung in pending:
                        rungs[rung]["resolved"] += 1
                    pending = []
                needs = e.get("needs") if isinstance(e.get("needs"), dict) else None
                if needs and e.get("status") in ("blocked", "failed"):
                    kinds[str(needs.get("kind"))]["blocks"] += 1
                    start = tick if tick is not None else (e.get("evidence") or {}).get("tick")
                    open_block = (
                        str(needs.get("kind")),
                        start if isinstance(start, int) else None,
                        str(e.get("reason")),
                    )
        if open_block is not None and open_block[1] is not None:
            kind, start, reason = open_block
            kinds[kind]["seconds"] += max(0, end - start) / TICKS
            if w is not None:
                w.add(
                    "blocked_by_needs",
                    (end - start) / TICKS,
                    {"tick": start, "job": cap(key), "reason": f"{kind}: {reason[:100]}"},
                )
    return {
        "blocked_by_needs_kind": {
            k: {"seconds": round(v["seconds"], 1), "blocks": v["blocks"]} for k, v in kinds.items()
        },
        "rungs": dict(rungs),
    }


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


def remedy_totals(results) -> tuple[dict, dict]:
    """Fleet sums of ``remedy_summary``: (needs kind -> seconds/blocks, rung -> counters)."""
    kinds = defaultdict(lambda: {"seconds": 0.0, "blocks": 0})
    rungs = defaultdict(lambda: {"checked": 0, "fitted": 0, "applied": 0, "resolved": 0})
    for result in results:
        found = result.get("remedies") or {}
        for kind, row in (found.get("blocked_by_needs_kind") or {}).items():
            kinds[kind]["seconds"] += row.get("seconds", 0)
            kinds[kind]["blocks"] += row.get("blocks", 0)
        for rung, row in (found.get("rungs") or {}).items():
            for k in rungs[rung]:
                rungs[rung][k] += row.get(k, 0)
    return dict(kinds), dict(rungs)


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
        if ex.get("cause"):
            print(f"{'':34s}cause (post-mortem): {ex['cause'][:120]}")
        for cause in row["causes"][:3]:
            print(
                f"{'':34s}{cause['seconds']:>6}s {cause['runs']:>2} runs x{cause['count']:<4} {cause['job']}: {cause['reason']}"
            )
    kinds, rungs = remedy_totals(results)
    if kinds or rungs:
        print("remedy ladder (blocked seconds per needs kind; rung checked/fitted/applied/resolved):")
        for kind, row in sorted(kinds.items(), key=lambda kv: -kv[1]["seconds"]):
            print(f"  needs {kind:12s} {row['seconds']:>7.0f}s in {row['blocks']} block(s)")
        for rung, row in sorted(rungs.items()):
            print(
                f"  rung  {rung:28s} checked {row['checked']:>3} fitted {row['fitted']:>3} "
                f"applied {row['applied']:>3} resolved {row['resolved']:>3}"
            )
    print(
        "critical chain to the 20-min target (median end min, median segment min, reached; binding constraint x runs):"
    )
    for row in chain_totals(results):
        binding = ", ".join(f"{k} x{v}" for k, v in row["binding"].items())
        print(
            f"  {row['segment']:9s} end {_text(row['median_end']):>5} span {_text(row['median_minutes']):>5} "
            f"{row['reached']:>2}/{row['runs']}  {binding}"
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
            if item.get("cause"):
                print(f"             cause (post-mortem): {item['cause'][:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
