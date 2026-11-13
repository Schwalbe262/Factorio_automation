"""Planner evidence for post-mortem dry runs (``factorio_agent.postmortem``). Off unless captured.

A planner calls ``note(stage, **facts)`` where it rejects a candidate or a
check fails (``planning.preflight``, ``transport.plan_connection``, the mining
bank and smelting district candidate loops). Outside ``capturing()`` that is a
no-op and costs one context-variable read, so live runs are unchanged. Inside
it every note is kept, made JSON-safe and bounded (``bounded``), and the caller
writes them to ``<run>/postmortem/``. The job ledger keeps only scalar evidence
(``jobs._advance_job``); this is where the nested detail goes instead.
"""

from __future__ import annotations

import contextlib
import contextvars
import math

_ROWS: contextvars.ContextVar[list | None] = contextvars.ContextVar("factorio_agent_evidence_capture", default=None)
MAX_ITEMS = 120  # list items and mapping keys kept per level
MAX_TEXT = 1500
MAX_DEPTH = 7
MAX_NOTES = 4000


def active() -> bool:
    return _ROWS.get() is not None


@contextlib.contextmanager
def capturing():
    """Collect every ``note`` of this thread into the yielded list."""
    rows: list = []
    token = _ROWS.set(rows)
    try:
        yield rows
    finally:
        _ROWS.reset(token)


def note(stage: str, **facts) -> None:
    rows = _ROWS.get()
    if rows is None:
        return
    if len(rows) >= MAX_NOTES:
        if len(rows) == MAX_NOTES:
            rows.append({"stage": "evidence_capture", "reason": "note_limit_reached", "limit": MAX_NOTES})
        return
    rows.append({"stage": str(stage), **{str(k): bounded(v) for k, v in facts.items()}})


def bounded(value, depth: int = 0):
    """A JSON-safe copy: lists/mappings cut to ``MAX_ITEMS`` (with a count), strings to ``MAX_TEXT``."""
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "...(%d chars)" % len(value)
    if depth >= MAX_DEPTH:
        return "<depth %d: %s>" % (depth, type(value).__name__)
    if isinstance(value, dict):
        items = list(value.items())
        out = {str(k): bounded(v, depth + 1) for k, v in items[:MAX_ITEMS]}
        if len(items) > MAX_ITEMS:
            out["_truncated_keys"] = len(items) - MAX_ITEMS
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [bounded(v, depth + 1) for v in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out.append({"_truncated_items": len(items) - MAX_ITEMS})
        return out
    return repr(value)[:MAX_TEXT]


def box(entities) -> list | None:
    """The tile bounding box ``[[x0, y0], [x1, y1]]`` of entity positions, or None."""
    points = [
        (e["position"]["x"], e["position"]["y"])
        for e in entities or ()
        if isinstance(e, dict) and isinstance(e.get("position"), dict) and {"x", "y"} <= set(e["position"])
    ]
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [[min(xs), min(ys)], [max(xs), max(ys)]]


def outcome(result) -> dict:
    """The failure-describing fields of a planner result (everything but bulky plan data)."""
    if not isinstance(result, dict):
        return {"result": bounded(result)}
    bulky = {"entities", "plan", "staged_plan", "future_plan", "underground_pairs", "segments", "route"}
    bulky |= {"clear_obstacles"}
    return {k: bounded(v) for k, v in result.items() if k not in bulky}
