"""The common failure contract of spatial planners: what failed and what would make it fit.

A planner that finds no plan returns a blocked status. ``from_result`` turns
that result into one ``needs`` record (stored on the job as ``job["needs"]``,
docs/v2/ARCHITECTURE.md "When a plan does not fit: the remedy ladder")::

    {"kind": "route", "reason": "no_clean_trunk_corridor", "block": "logistic_science",
     "item": "iron-plate", "from": [80.5, -95.5], "to": [77.5, -90.5],
     "blockers": [{"entity": "transport-belt", "owner": "planned_main_bus", "position": [..]}],
     "limit": "turns 7>6", "tried": 3, "rejected_by": {"route": 3},
     "change": "a clean corridor iron-plate (80.5,-95.5)->(77.5,-90.5)"}

Kinds (``KINDS``): ``route`` (a belt corridor), ``tap`` (a bus tap row/branch),
``site`` (no block/allocation fits), ``bank`` (no mining bank candidate),
``preflight`` (the whole plan meets something), ``power`` (no pole chain),
``plan`` (any other planning failure). ``change`` names the single change that
would unblock it (the retry condition when every remedy is spent).

A planner may return the record itself as ``result["plan_needs"]`` (built with
``make``); otherwise it is derived from the evidence shapes the planners
already produce: ``failures`` (route rows with ``source``/``destination``),
``candidate_failures`` / ``exit_sides_tried`` / ``attempts`` / ``attempted_banks``
(mining banks), ``route_failures`` (bus feeds), ``corridor_failures_by_item``
(smelting district), ``failed_check`` / ``blocked`` (preflight), ``first_blocker``
and an existing string ``needs`` (``outposts._needs``). A blocked result that is
not a planning failure (waiting for items, research, a prerequisite) has no needs.

Not to be confused with ``evidence["needs"]`` (item -> units an item wait
claims, ``critical_path.claims``): this record lives on the job, not in evidence.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any, Mapping

KINDS = ("route", "tap", "site", "bank", "preflight", "power", "plan")
MAX_BLOCKERS = 5
MAX_TEXT = 200
# Reasons that are planning failures although they do not start with "no_".
PLANNING_MARKERS = (
    "_route_failed",
    "_preflight_failed",
    "corridor",
    "_site",
    "site_",
    "allocation",
    "_district_block",
    "no_legal_site",
    "tap_unavailable",
    "reserved_by_unbuilt_plan",
    "obstructed",
    "power_bridge",
    "unroutable",  # steam_expansion_layout_unroutable
    "_no_buildable",  # steam_expansion_no_buildable_bank_slot
    "trunk_blocked",  # planned_bus_trunk_blocked
    "meets_planned_hardware",  # planned furnaces, mining banks
    "no_coal_stub_variant",  # steam expansion coal stub
    "board_reservation_overlap",
    "planning_budget_exhausted",  # industrial.PlanningBudget.failure: a site search out of time
)
# Reasons that start with "no_" but wait for something other than space.
NOT_SPATIAL = ("no_minable", "no_progress", "no_science", "no_fuel", "no_research")


def _point(value) -> list | None:
    if (
        isinstance(value, Mapping)
        and isinstance(value.get("x"), (int, float))
        and isinstance(value.get("y"), (int, float))
    ):
        return [round(float(value["x"]), 1), round(float(value["y"]), 1)]
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
        return [round(float(value[0]), 1), round(float(value[1]), 1)]
    return None


def _text(value, limit=MAX_TEXT) -> str:
    return str(value)[:limit]


def _blocker(row) -> dict | None:
    if not isinstance(row, Mapping):
        return None
    out = {}
    name = row.get("entity") or row.get("name")
    if name:
        out["entity"] = _text(name, 60)
    owner = row.get("owner") or row.get("block") or row.get("plan")
    if owner:
        out["owner"] = _text(owner, 60)
    position = _point(row.get("position") or row.get("at"))
    if position:
        out["position"] = position
    reason = row.get("reason") or row.get("check")
    if reason:
        out["reason"] = _text(reason, 80)
    return out or None


def make(kind: str, reason: str = "", **fields) -> dict:
    """One bounded needs record (``KINDS``); unknown kinds become ``plan``."""
    record: dict[str, Any] = {"kind": kind if kind in KINDS else "plan", "reason": _text(reason or "")}
    for key, value in fields.items():
        if value is None or value == [] or value == {}:
            continue
        if key in ("from", "to", "at"):
            point = _point(value)
            if point:
                record[key] = point
        elif key == "blockers":
            rows = [b for b in (_blocker(v) for v in value) if b]
            if rows:
                record["blockers"] = rows[:MAX_BLOCKERS]
        elif key == "rejected_by" and isinstance(value, Mapping):
            top = sorted(
                ((str(k)[:80], int(v)) for k, v in value.items() if isinstance(v, (int, float))), key=lambda r: -r[1]
            )
            record["rejected_by"] = dict(top[:8])
        elif isinstance(value, bool) or isinstance(value, (int, float)):
            record[key] = value
        elif isinstance(value, str):
            record[key] = _text(value)
        elif isinstance(value, (list, tuple)):
            record[key] = [v if isinstance(v, (int, float, bool)) else _text(v, 60) for v in list(value)[:8]]
        elif isinstance(value, Mapping):
            text = json.dumps(value, default=str)
            record[key] = json.loads(text) if len(text) <= 600 else _text(text, 300)
    if "change" not in record:
        record["change"] = default_change(record)
    return record


def default_change(record: Mapping) -> str:
    """The single change that would unblock ``record`` (derived when the planner names none)."""
    kind = record.get("kind")
    item = record.get("item")
    if kind == "route" and record.get("from") and record.get("to"):
        a, b = record["from"], record["to"]
        return _text(f"a clean corridor {item or 'belt'} ({a[0]},{a[1]})->({b[0]},{b[1]})")
    if kind == "tap":
        return _text(f"a free bus tap row for {item or 'the input'} on side {record.get('side', '?')}")
    if kind == "bank":
        return _text(
            f"a {record.get('resource', 'resource')} bank anchor with an off-ore exit (another patch or a smaller row)"
        )
    if kind == "site":
        return _text(f"a free block for {record.get('block', 'the block')} with clean connections")
    if kind == "preflight":
        return "the entities standing in the plan's footprint moved or removed"
    if kind == "power":
        return "a pole chain from the grid to the block"
    return _text("a planner fact change: " + str(record.get("reason") or "unknown"))


def _counts(rows) -> dict:
    found = Counter()
    for row in rows or ():
        if isinstance(row, Mapping):
            label = row.get("reason") or row.get("check") or row.get("stage")
        else:
            label = str(row).split(":", 1)[0]
        if label:
            found[str(label)[:80]] += 1
    return dict(found)


def is_planning_failure(reason: str) -> bool:
    reason = str(reason or "")
    if reason.startswith("no_") and not reason.startswith(NOT_SPATIAL):
        return True
    return any(marker in reason for marker in PLANNING_MARKERS)


def _route_row(row) -> bool:
    return (
        isinstance(row, Mapping)
        and (row.get("source") is not None or row.get("from") is not None)
        and (row.get("destination") is not None or row.get("to") is not None)
    )


EXECUTOR_FAILURE = "repeated_executor_failure:"


def _action_position(action: Mapping) -> list | None:
    point = _point(action.get("position"))
    if point is None and isinstance(action.get("actions"), list):
        point = next(
            (p for p in (_point(a.get("position")) for a in action["actions"] if isinstance(a, Mapping)) if p), None
        )
    return point


def from_executor_failure(capability: str, reason: str, action: Mapping | None = None) -> dict:
    """Needs of a job that the same executor failure stopped (``jobs``: three in a row).

    The planned action keeps failing at a position (a move onto water, a build the world
    refuses, a target gone): kind ``preflight`` when the action names a position (the
    position is the blocker; the plan must avoid or clear it), else ``plan``. The ladder's
    ``replan`` then re-plans with the world as it is (eval-1575138435, 320 min: the oil
    scout's move onto deep water failed position_blocked until the job stayed blocked).
    """
    failure = reason[len(EXECUTOR_FAILURE) :] if reason.startswith(EXECUTOR_FAILURE) else reason
    action = action if isinstance(action, Mapping) else {}
    kind_of = str(action.get("type") or "action")
    position = _action_position(action)
    name = action.get("name") or action.get("item")
    where = f" at ({position[0]},{position[1]})" if position else ""
    return make(
        "preflight" if position else "plan",
        reason,
        check="executor:" + kind_of,
        action=kind_of,
        at=position,
        entity=name,
        blockers=[{"entity": name or kind_of, "position": position, "reason": failure}] if position else None,
        change=_text(f"a plan whose {kind_of}{' ' + str(name) if name else ''}{where} does not fail {failure}"),
        block=capability,
    )


def from_result(capability: str, result: Mapping, parameters: Mapping | None = None) -> dict | None:
    """The needs of a blocked/failed planner result, or None when it is no planning failure.

    ``parameters`` (the job's) fill ``resource`` / ``item`` when the result does not name them.
    A ``repeated_executor_failure:<reason>`` block (``executor_action``: the failing action)
    is a planning failure too (:func:`from_executor_failure`).
    """
    if not isinstance(result, Mapping):
        return None
    if str(result.get("reason") or "").startswith(EXECUTOR_FAILURE):
        return from_executor_failure(capability, str(result["reason"]), result.get("executor_action"))
    given = result.get("plan_needs")
    if isinstance(given, Mapping) and given.get("kind"):
        fields = {k: v for k, v in given.items() if k not in ("kind", "reason")}
        fields.setdefault("block", capability)
        return make(str(given["kind"]), str(given.get("reason") or result.get("reason") or ""), **fields)
    reason = str(result.get("reason") or "")
    evidence = result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {}
    data = {**evidence, **{k: v for k, v in result.items() if k != "evidence"}}
    for name in ("resource", "item", "product"):
        if isinstance((parameters or {}).get(name), str):
            data.setdefault("resource" if name == "resource" else "request_item", parameters[name])
    if not is_planning_failure(reason) and not any(
        k in data for k in ("candidate_failures", "route_failures", "corridor_failures_by_item", "plan_needs")
    ):
        return None
    common = {"block": capability}
    string_needs = data.get("needs") if isinstance(data.get("needs"), str) else None
    failures = data.get("failures") if isinstance(data.get("failures"), list) else []
    if not failures:
        # Layout candidates that did not fit (production_site.place_line) count like site failures.
        for name in ("layout_failures", "infeasible"):
            if isinstance(data.get(name), list) and data[name]:
                failures = data[name]
                break
    trunk_tap = data.get("trunk_tap") if isinstance(data.get("trunk_tap"), Mapping) else None
    if trunk_tap and not string_needs:
        # dedicated_materials: tapping the patch's bank trunk failed first, then a new bank found no anchor.
        string_needs = _text(f"a tap on the patch trunk ({trunk_tap.get('reason')}) or a new bank anchor")
    # Tap rows: the bus tap could not be placed or routed.
    tap = next((f for f in failures if isinstance(f, Mapping) and "tap" in str(f.get("reason", ""))), None)
    if tap is None and "planned_bus_tap" in reason:
        tap = data
    if tap is not None:
        inner = tap.get("failures") if isinstance(tap.get("failures"), list) else []
        return make(
            "tap",
            str(tap.get("reason") or reason),
            item=tap.get("item"),
            side=tap.get("side"),
            tried=len(inner) or None,
            rejected_by=_counts(inner),
            sites_tried=len(failures) or None,
            **common,
        )
    routes = [f for f in failures if _route_row(f)]
    if routes:
        first = routes[0]
        return make(
            "route",
            str(first.get("reason") or reason),
            item=first.get("item"),
            **{"from": first.get("source") or first.get("from"), "to": first.get("destination") or first.get("to")},
            tried=len(failures),
            rejected_by=_counts(failures),
            limit=first.get("limit"),
            outer_reason=reason,
            **common,
        )
    route_failures = data.get("route_failures")
    if isinstance(route_failures, list) and route_failures:
        rows = [r for r in route_failures if isinstance(r, Mapping)]
        first = rows[0] if rows else {}
        return make(
            "route",
            str(first.get("reason") or reason),
            item=first.get("item"),
            **{"from": first.get("source") or first.get("from"), "to": first.get("destination") or first.get("to")},
            tried=len(route_failures),
            rejected_by=_counts(route_failures),
            outer_reason=reason,
            **common,
        )
    by_item = data.get("corridor_failures_by_item")
    if isinstance(by_item, Mapping) and by_item:
        item, count = max(
            ((k, v) for k, v in by_item.items() if isinstance(v, (int, float))), key=lambda r: r[1], default=(None, 0)
        )
        return make(
            "site",
            reason,
            item=item,
            rejected_by={f"corridor:{k}": v for k, v in by_item.items() if isinstance(v, (int, float))},
            tried=data.get("legal_sites"),
            change=string_needs or (f"a clean feeder corridor for {item}" if item else None),
            **common,
        )
    if any(k in data for k in ("candidate_failures", "exit_sides_tried", "attempted_banks", "attempts")) and (
        "bank" in reason or "mining" in reason or capability in ("mining_outpost", "dedicated_materials")
    ):
        candidates = data.get("candidate_failures") if isinstance(data.get("candidate_failures"), list) else []
        attempts = data.get("attempts") if isinstance(data.get("attempts"), list) else []
        return make(
            "bank",
            reason,
            resource=data.get("resource"),
            sides=data.get("exit_sides_tried"),
            tried=data.get("candidate_count") if isinstance(data.get("candidate_count"), int) else len(candidates),
            attempts=len(attempts) or data.get("attempted_banks"),
            rejected_by=_counts(candidates) or _counts([a.get("reason") for a in attempts if isinstance(a, Mapping)]),
            change=string_needs,
            **common,
        )
    blocked = data.get("blocked") if isinstance(data.get("blocked"), list) else []
    first_blocker = data.get("first_blocker")
    if data.get("failed_check") or blocked or "preflight" in reason:
        return make(
            "preflight",
            reason,
            check=data.get("failed_check"),
            blockers=[first_blocker, *blocked] if isinstance(first_blocker, Mapping) else blocked,
            change=string_needs,
            **common,
        )
    if "power" in reason or "pole" in reason:
        return make("power", reason, change=string_needs, **common)
    if any(m in reason for m in ("site", "allocation", "district_block")):
        return make(
            "site", reason, tried=len(failures) or None, rejected_by=_counts(failures), change=string_needs, **common
        )
    return make(
        "plan", reason, tried=len(failures) or None, rejected_by=_counts(failures), change=string_needs, **common
    )


def signature(needs: Mapping | None) -> str:
    """Stable short id of a needs record: each remedy is tried at most once per distinct signature."""
    if not needs:
        return ""
    key = [needs.get(k) for k in ("kind", "reason", "block", "item", "from", "to", "side", "resource")]
    return hashlib.sha1(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:12]
