"""Estimate local-LLM serving (GPU) utilization from the Slurm scheduler's task history.

We cannot read nvidia-smi without shelling into the GPU node, but for the 1-vs-N GPU decision what
matters is how much of the time the GPU is actually serving OUR requests. Each client request task
(strategy / layout / foundry / code_agent / circuit) has started_at/finished_at; summing their active
wall-time over a recent window gives a busy-fraction (requests are serial under COUNT=1, so they do
not overlap and the sum is a fair estimate). A low busy-fraction => the GPU is mostly idle => 1 GPU is
enough.

Usage:
    python tools/serving_utilization.py [--minutes 30] [--url http://100.112.168.31:8000]

`serving_utilization(tasks, now, window_minutes)` is a PURE function (no network) so it is unit-tested.
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timedelta
from typing import Any

CLIENT_KINDS = ("strategy", "layout", "foundry", "code_agent", "circuit", "improvement")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _task_kind(name: str) -> str:
    low = name.lower()
    for kind in CLIENT_KINDS:
        if kind in low:
            return kind
    return "other"


def serving_utilization(
    tasks: list[dict[str, Any]],
    now: datetime,
    window_minutes: int = 30,
) -> dict[str, Any]:
    """Compute serving busy-fraction over the last ``window_minutes`` from client request tasks.

    Pure: pass the scheduler ``/api/tasks`` list, the current time, and a window. Returns
    {window_minutes, requests, busy_seconds, busy_fraction, idle_fraction, avg_latency_seconds,
    by_kind, service_running}. Only counts requests that STARTED within the window (so a stale run's
    old timed-out tasks do not pollute the estimate)."""
    window_start = now - timedelta(minutes=window_minutes)
    window_seconds = float(window_minutes * 60) or 1.0
    busy = 0.0
    reqs: list[float] = []
    by_kind: dict[str, int] = {}
    service_running = False
    for task in tasks:
        if not isinstance(task, dict):
            continue
        name = str(task.get("name") or "")
        if "vllm-service" in name.lower():
            if str(task.get("state") or "").lower() == "running":
                service_running = True
            continue
        kind = _task_kind(name)
        if kind == "other":
            continue
        started = _parse_ts(task.get("started_at"))
        finished = _parse_ts(task.get("finished_at")) or now
        if started is None or started < window_start:
            continue
        dur = max(0.0, (finished - started).total_seconds())
        busy += dur
        reqs.append(dur)
        by_kind[kind] = by_kind.get(kind, 0) + 1
    busy_capped = min(busy, window_seconds)  # serial requests can't exceed wall-clock
    return {
        "window_minutes": window_minutes,
        "requests": len(reqs),
        "busy_seconds": round(busy, 1),
        "busy_fraction": round(busy_capped / window_seconds, 3),
        "idle_fraction": round(1.0 - busy_capped / window_seconds, 3),
        "avg_latency_seconds": round(busy / len(reqs), 1) if reqs else 0.0,
        "by_kind": by_kind,
        "service_running": service_running,
    }


def fetch_and_report(url: str, minutes: int) -> dict[str, Any]:
    raw = urllib.request.urlopen(url.rstrip("/") + "/api/tasks", timeout=15).read()
    tasks = json.loads(raw)
    tasks = tasks if isinstance(tasks, list) else tasks.get("tasks", [])
    # Use the newest finished_at as "now" so it works regardless of local-vs-cluster clock skew.
    latest = None
    for t in tasks:
        if isinstance(t, dict):
            ts = _parse_ts(t.get("finished_at")) or _parse_ts(t.get("started_at"))
            if ts and (latest is None or ts > latest):
                latest = ts
    now = latest or datetime(2000, 1, 1)
    return serving_utilization(tasks, now, minutes)


def main() -> None:
    ap = argparse.ArgumentParser(description="Estimate local-LLM serving GPU utilization from scheduler history")
    ap.add_argument("--minutes", type=int, default=30, help="rolling window (minutes)")
    ap.add_argument("--url", default="http://100.112.168.31:8000", help="scheduler base URL")
    args = ap.parse_args()
    try:
        rep = fetch_and_report(args.url, args.minutes)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return
    pct = round(100 * rep["busy_fraction"])
    print(f"serving GPU utilization (last {rep['window_minutes']}min): ~{pct}% busy / ~{100 - pct}% idle")
    print(f"  requests: {rep['requests']} | avg latency: {rep['avg_latency_seconds']}s | by kind: {rep['by_kind']}")
    print(f"  vllm service running: {rep['service_running']}")
    print(f"  verdict: {'1 GPU is plenty (mostly idle)' if pct < 60 else 'high utilization - more GPUs / fewer concurrent loops would help'}")
    print(json.dumps(rep))


if __name__ == "__main__":
    main()
