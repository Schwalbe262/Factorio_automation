"""Replay recorded incident documents to measure model decision quality offline.

Usage: python -B tools/luna_incident_replay.py runtime/v2/<name> [--limit N] [--dry-run | --send]

Reads ``logs/incidents.jsonl`` (the exact documents sent, or that would have
been sent with planner none) and ``logs/controller-events.jsonl``. For each
incident it prints what later happened in that run: the outcome, seconds to
it, and what resolved it (the model's applied remedy, a framework action such
as auto-retry or autopilot between opening and resolution, or nothing).

``--dry-run`` (default) prints each document with that outcome; it starts no
model. ``--send`` gives each document to ``LunaDecisionClient`` (pinned model
and effort) and prints the chosen remedy, whether it is on the incident's
menu, latency and token usage next to what actually resolved the incident.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from factorio_agent import incidents as rules  # noqa: E402

# Framework events that can clear an incident without the model.
FRAMEWORK_EVENTS = ("auto_retry", "autopilot", "resume_after_safe_start", "dispatch_blocked")


def rows(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def later(record, events):
    """Outcome of the recorded incident episode and what resolved it, from later controller events."""
    start = record.get("time", 0)
    key = record.get("incident_id")
    outcome = next(
        (
            e
            for e in events
            if e.get("event") == "incident_outcome" and e.get("incident_id") == key and e.get("time", 0) >= start
        ),
        None,
    )
    end = outcome.get("time", float("inf")) if outcome else float("inf")
    if outcome is None:
        return dict(outcome="open", seconds=None, resolved_by=None)
    if outcome.get("outcome") != "resolved":
        return dict(outcome=outcome.get("outcome"), seconds=outcome.get("seconds"), resolved_by=None)
    if outcome.get("remedy") not in (None, "wait"):
        resolved_by = "model:" + str(outcome["remedy"])
    else:
        subject = record.get("subject")
        actions = [
            e
            for e in events
            if start <= e.get("time", 0) <= end
            and e.get("event") in FRAMEWORK_EVENTS
            and (e.get("event") != "auto_retry" or e.get("key") == subject)
        ]
        resolved_by = "framework:" + actions[0]["event"] if actions else "condition_cleared"
    return dict(outcome="resolved", seconds=outcome.get("seconds"), resolved_by=resolved_by)


def menu_incident(record):
    """An Incident carrying the recorded menu, so a replayed decision is checked like a live one."""
    document = record.get("document") or {}
    remedies = []
    for row in document.get("remedies") or []:
        if not isinstance(row, dict) or row.get("remedy") not in rules.REMEDY_OPERATION:
            continue
        parameters = json.loads(row["parameters_json"]) if isinstance(row.get("parameters_json"), str) else None
        remedies.append(
            rules.Remedy(
                row["remedy"],
                row.get("target_task_id"),
                row.get("capability"),
                capabilities=row.get("capabilities"),
                parameters=parameters,
            )
        )
    head = document.get("incident") or {}
    return rules.Incident(
        record.get("incident_id"), head.get("kind"), head.get("subject"), head.get("since") or 0, {}, remedies
    )


def replay(root, *, limit=None, send=False, client=None):
    root = Path(root)
    records = rows(root / "logs" / "incidents.jsonl")
    events = rows(root / "logs" / "controller-events.jsonl")
    if limit is not None:
        records = records[:limit]
    if send and client is None:
        from factorio_agent.luna_client import LunaDecisionClient

        client = LunaDecisionClient(model="gpt-6-luna", reasoning_effort="medium", timeout_seconds=120)
    results = []
    for record in records:
        document = record.get("document") or {}
        row = dict(
            incident_id=record.get("incident_id"),
            kind=record.get("incident_kind"),
            subject=record.get("subject"),
            capability=record.get("capability"),
            handled_by=record.get("handled_by"),
            document_chars=len(json.dumps(document, ensure_ascii=False, separators=(",", ":"))),
            **later(record, events),
        )
        if send:
            started = time.monotonic()
            try:
                decision = client.decide(document)
            except Exception as exc:
                row.update(chosen_remedy=None, error=str(exc)[:300])
            else:
                row.update(
                    chosen_remedy=rules.remedy_of(decision),
                    target_task_id=decision.get("target_task_id"),
                    rejected=rules.check_decision(menu_incident(record), decision),
                    reason=decision.get("reason", "")[:300],
                )
            row.update(latency_seconds=round(time.monotonic() - started, 1), usage=getattr(client, "last_usage", None))
        else:
            row["document"] = document
        results.append(row)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--limit", type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print documents and outcomes (default)")
    mode.add_argument("--send", action="store_true", help="ask the pinned model for each document")
    args = parser.parse_args(argv)
    for row in replay(args.runtime, limit=args.limit, send=args.send):
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
