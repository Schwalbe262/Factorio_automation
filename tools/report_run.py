"""Summarise how a runtime spent its time. Read-only.

Usage: python -B tools/report_run.py runtime/v2/<name> [--json] [--gap-seconds 30]

The numbers are the yardstick for speed work: actions per minute, the share of
wall time with no action for longer than the gap threshold, framework planning
time against action execution time, how often the model was consulted, and
which incidents opened, what was chosen for them and whether they resolved.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from factorio_agent.incidents import repeat_summary  # noqa: E402


def parse_row(line):
    """One JSONL event row with a numeric ``time``, or None (blank, corrupt or untimed line)."""
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if isinstance(row, dict) and isinstance(row.get("time"), (int, float)):
        return row
    return None


def rows(path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            row = parse_row(line)
            if row is not None:
                yield row


def report(root, gap_seconds=30.0, *, job_events=None, controller=None, jobs=None):
    """The run summary. ``job_events``, ``controller`` (row lists) and ``jobs``
    (the ledger's ``jobs`` mapping) may be passed in already loaded, as the
    dashboard does with its incremental readers; missing ones are read here."""
    root = Path(root)
    if job_events is None:
        job_events = list(rows(root / "job-events.jsonl"))
    if controller is None:
        controller = list(rows(root / "logs" / "controller-events.jsonl"))
    if jobs is None:
        ledger = root / "jobs.json"
        jobs = json.loads(ledger.read_text(encoding="utf-8")).get("jobs", {}) if ledger.exists() else {}

    actions = [r for r in job_events if r.get("event") == "action_result"]
    times = sorted(r["time"] for r in job_events + controller)
    wall = times[-1] - times[0] if len(times) > 1 else 0.0
    ticks = [r["tick"] for r in job_events if isinstance(r.get("tick"), int)]

    # Idle share: wall time between consecutive actions beyond the threshold.
    marks = [times[0], *sorted(r["time"] for r in actions), times[-1]] if times else []
    gaps = [b - a for a, b in zip(marks, marks[1:]) if b - a > gap_seconds]

    planning = sum(j.get("planning_seconds", 0) for j in jobs.values())
    execution = sum(j.get("execution_seconds", 0) for j in jobs.values())
    action_types = Counter(r.get("action", {}).get("type") for r in job_events if r.get("event") == "action_intent")

    # Progress, not activity: when each job finished, and how repetitive the
    # most recent actions are. A run that only repeats one small action is busy
    # but stuck, whatever its actions-per-minute says.
    capability = {key: job.get("capability") for key, job in jobs.items()}
    start = times[0] if times else 0.0
    milestones = [
        {
            "minute": round((r["time"] - start) / 60, 1),
            "capability": capability.get(r.get("key")),
            "reason": str(r.get("reason"))[:60],
        }
        for r in job_events
        if r.get("event") == "terminal" and r.get("status") == "succeeded"
    ]
    intents = [r for r in job_events if r.get("event") == "action_intent"]
    recent = intents[-100:]
    repeated, repeat_share = repeat_summary(recent, capability)
    last_success = milestones[-1]["minute"] if milestones else None

    decisions = Counter(r.get("operation") for r in controller if r.get("event") == "decision")
    slice_reasons = Counter(r.get("reason") for r in controller if r.get("event") == "slice")
    errors = Counter(str(r.get("reason"))[:120] for r in controller if r.get("event") == "error")
    terminal = Counter((r.get("status"), str(r.get("reason"))[:80]) for r in job_events if r.get("event") == "terminal")

    # Child-process bridge logs cli_exit; the in-process bridge logs call with seconds.
    commands = defaultdict(lambda: {"count": 0, "seconds": 0.0})
    kernel = None
    for r in controller:
        if r.get("event") in ("cli_exit", "call"):
            row = commands[r.get("command")]
            row["count"] += 1
            row["seconds"] += r.get("seconds", 0.0)
            kernel = r.get("kernel") or kernel

    per_job = [
        {
            "capability": j.get("capability"),
            "status": j.get("status"),
            "reason": str(j.get("reason"))[:80],
            "actions": j.get("actions", 0),
            "planning_seconds": round(j.get("planning_seconds", 0), 1),
            "execution_seconds": round(j.get("execution_seconds", 0), 1),
        }
        for j in sorted(jobs.values(), key=lambda j: j.get("created_at", 0))
    ]
    minutes = wall / 60 if wall else 0.0
    return {
        "runtime": str(root),
        "wall_minutes": round(minutes, 1),
        "game_minutes": round((max(ticks) - min(ticks)) / 3600, 1) if ticks else 0.0,
        "jobs": len(jobs),
        "actions": len(actions),
        "actions_per_minute": round(len(actions) / minutes, 1) if minutes else 0.0,
        "idle_gaps": len(gaps),
        "idle_gap_minutes": round(sum(gaps) / 60, 1),
        "idle_gap_share": round(sum(gaps) / wall, 3) if wall else 0.0,
        "longest_gaps_seconds": [round(g) for g in sorted(gaps, reverse=True)[:5]],
        "jobs_succeeded": len(milestones),
        "minutes_since_last_success": round(minutes - last_success, 1) if last_success is not None else None,
        "milestones": milestones,
        "recent_100_actions_top": repeated,
        "recent_repeat_share": repeat_share,
        "planning_seconds": round(planning, 1),
        "execution_seconds": round(execution, 1),
        "planning_per_execution": round(planning / execution, 2) if execution else None,
        "action_types": dict(action_types.most_common(8)),
        "model_decisions": sum(decisions.values()),
        "model_decision_operations": dict(decisions),
        "slices": sum(slice_reasons.values()),
        "slice_end_reasons": dict(slice_reasons.most_common(8)),
        "terminal_results": [{"status": s, "reason": r, "count": n} for (s, r), n in terminal.most_common(8)],
        "controller_errors": dict(errors.most_common(5)),
        "commands": {k: {"count": v["count"], "seconds": round(v["seconds"], 1)} for k, v in commands.items()},
        "kernel_totals": kernel,
        "query_load": query_load(kernel, len(actions)),
        "per_job": per_job,
        **incident_report(root, controller),
        **test_mode_label(root),
    }


def query_load(kernel, actions):
    """RCON query cost from the latest kernel totals (one supervisor session), or None.

    ``share_percent`` is query seconds over the session's command seconds;
    ``per_slice`` divides by its ``run`` commands; ``ms_per_action`` by the run's
    actions (all actions of the run; the totals cover the latest session only).
    """
    if not isinstance(kernel, dict) or not isinstance(kernel.get("queries"), dict):
        return None
    count = kernel["queries"].get("count", 0)
    seconds = kernel["queries"].get("seconds", 0.0)
    commands = kernel.get("commands") or {}
    busy = sum(row.get("seconds", 0.0) for row in commands.values() if isinstance(row, dict))
    slices = (commands.get("run") or {}).get("count", 0)
    return {
        "queries": count,
        "query_seconds": round(seconds, 1),
        "share_percent": round(100 * seconds / busy, 1) if busy else None,
        "queries_per_minute": round(count / (busy / 60), 1) if busy else None,
        "per_slice": round(count / slices, 1) if slices else None,
        "ms_per_action": round(1000 * seconds / actions) if actions else None,
    }


def test_mode_label(root):
    """Client-free test runs (actor_client mode none): every claim is a test-mode claim."""
    from factorio_agent.actor_client import test_mode

    label = test_mode(root)
    if not label:
        return {}
    return dict(
        label,
        claims_scope="TEST MODE client_free: hand-craft trigger technologies were script-credited after the "
        "real craft; milestones are test-mode results, not proof-run results",
    )


def incident_report(root, controller):
    """Planner mode, model cost and incident outcomes from controller events.

    One episode per ``incident_opened``; its remedy is the last accepted
    ``incident_decision`` and its outcome the next ``incident_outcome`` for the
    same incident id (``open`` while none was logged).
    """
    started = [r for r in controller if r.get("event") == "supervisor_started"]
    planner = started[-1].get("planner") if started else None
    if planner is None:
        state = Path(root) / "logs" / "controller-state.json"
        try:
            planner = json.loads(state.read_text(encoding="utf-8")).get("planner") if state.exists() else None
        except (OSError, ValueError, AttributeError):
            planner = None
    episodes = []
    current = {}
    consultations = [r for r in controller if r.get("event") == "incident_decision"]
    for r in controller:
        event = r.get("event")
        key = r.get("incident_id")
        if event == "incident_opened":
            current[key] = dict(
                kind=r.get("incident_kind"),
                capability=r.get("capability"),
                remedy=None,
                outcome="open",
                seconds=None,
            )
            episodes.append(current[key])
        elif event == "incident_decision" and key in current and r.get("accepted"):
            current[key]["remedy"] = r.get("remedy")
        elif event == "incident_outcome" and key in current:
            current.pop(key).update(outcome=r.get("outcome"), seconds=r.get("seconds"))
    resolved = [e["seconds"] for e in episodes if e["outcome"] == "resolved" and e["seconds"] is not None]
    return {
        "planner_mode": planner or "luna (legacy run)",
        "model_consultations": len(consultations),
        "model_seconds": round(sum(r.get("latency_seconds") or 0 for r in consultations), 1),
        "model_input_tokens": sum(r.get("input_tokens") or 0 for r in consultations),
        "incidents_by_kind": dict(Counter(e["kind"] for e in episodes)),
        "incidents_resolved": sum(e["outcome"] == "resolved" for e in episodes),
        "incidents_expired": sum(e["outcome"] == "expired" for e in episodes),
        "median_seconds_to_resolution": round(statistics.median(resolved), 1) if resolved else None,
        "incidents": episodes,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--gap-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    data = report(args.runtime, args.gap_seconds)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return
    if data.get("test_mode"):
        print("TEST MODE " + data["test_mode"] + ": " + json.dumps(data.get("script_credited_triggers")))
    for key, value in data.items():
        if key == "milestones":
            print("milestones (TEST MODE):" if data.get("test_mode") else "milestones:")
            for row in value:
                print("  {minute:6.1f} min  {capability}  ({reason})".format(**row))
        elif key == "per_job":
            print("per_job:")
            for job in value:
                print(
                    "  {capability:24s} {status:10s} actions={actions:4d} plan={planning_seconds:7.1f}s "
                    "exec={execution_seconds:6.1f}s  {reason}".format(**job)
                )
        elif key == "incidents":
            print("incidents:")
            for row in value:
                print("  {kind:14s} {capability!s:24s} remedy={remedy!s:10s} {outcome}".format(**row))
        else:
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
