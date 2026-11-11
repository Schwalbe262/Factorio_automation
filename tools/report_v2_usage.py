"""Sample one explicit Codex thread's real counters, never estimate quota share."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from factorio_ai.token_usage import summarize_codex_session_usage
from factorio_ai.deterministic_state import _atomic_json


def sample(thread_id: str) -> dict:
    if not thread_id or any(c in thread_id for c in "/*?[]\\"):
        raise ValueError("an exact thread ID is required")
    root = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    paths = list(root.rglob("*" + thread_id + ".jsonl"))
    if len(paths) != 1:
        raise ValueError("exactly one session must match the requested thread")
    value = summarize_codex_session_usage(paths[0])
    if value["thread_id"] != thread_id:
        raise ValueError("session identity mismatch")
    return {"sampled_at": time.time(), "thread_id": thread_id, "tokens": value["tokens_used"],
            "weekly_account_percent": value["weekly_used_percent"],
            "weekly_resets_at": value["weekly_resets_at"], "source": value["source"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("part")
    parser.add_argument("--thread-id", default=os.getenv("CODEX_THREAD_ID"))
    parser.add_argument("--baseline", type=int, help="An earlier measured counter, including cached input")
    args = parser.parse_args()
    if not args.part or any(c in args.part for c in "/\\."):
        raise ValueError("part label must be a simple filename component")
    path = Path(__file__).resolve().parents[1] / "runtime" / "usage" / (args.part + ".json")
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    current = sample(args.thread_id)
    if previous and previous["first"]["thread_id"] != args.thread_id:
        raise ValueError("a part counter cannot silently switch threads")
    first = previous["first"] if previous else dict(current)
    if not previous and args.baseline is not None:
        if not 0 <= args.baseline <= current["tokens"]:
            raise ValueError("invalid baseline counter")
        first["tokens"] = args.baseline
        first["baseline_time_unknown"] = True
        first["weekly_account_percent"] = None
    delta = current["tokens"] - first["tokens"]
    if delta < 0:
        raise ValueError("counter reset; start another interval")
    weekly_delta = None
    if (current["weekly_resets_at"] == first["weekly_resets_at"]
            and first["weekly_account_percent"] is not None and current["weekly_account_percent"] is not None):
        weekly_delta = current["weekly_account_percent"] - first["weekly_account_percent"]
    result = {"part": args.part, "first": first, "latest": current, "tokens_including_cached_input": delta,
              "account_weekly_delta_percentage_points": weekly_delta,
              "exact_part_weekly_share": None,
              "limitation": "Account meter includes concurrent activity; absolute weekly token quota is unavailable."}
    _atomic_json(path, result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
