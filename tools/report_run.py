"""Summarise how a runtime spent its time. Read-only.

Usage: python -B tools/report_run.py runtime/v2/<name> [--json] [--gap-seconds 30]

The numbers are the yardstick for speed work: actions per minute, the share of
wall time with no action for longer than the gap threshold, framework planning
time against action execution time, and how often the model was consulted.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path


def rows(path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("time"), (int, float)):
                yield row


def report(root, gap_seconds=30.0):
    root = Path(root)
    job_events = list(rows(root / "job-events.jsonl"))
    controller = list(rows(root / "logs" / "controller-events.jsonl"))
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
        "per_job": per_job,
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
    for key, value in data.items():
        if key == "per_job":
            print("per_job:")
            for job in value:
                print(
                    "  {capability:24s} {status:10s} actions={actions:4d} plan={planning_seconds:7.1f}s "
                    "exec={execution_seconds:6.1f}s  {reason}".format(**job)
                )
        else:
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
