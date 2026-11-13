"""Growth providers: one interface from a diagnosed bottleneck to a growth package (PLANNERS.md "Bottleneck auditor").

Every producer block type answers the same question through
``growth_options(bottleneck, observation, state) -> list[option]``:

    bottleneck   one row of the ``bottlenecks`` receipt (``bottleneck_audit.diagnose``):
                 group, block, item, kind (at_capacity | transport | no_power | no_producer |
                 consumer_capacity | partly_working), consumers, chain, lost_seconds_per_minute
    observation  the observe receipt (raw or the supervisor's summary; providers read receipts only)
    state        {"jobs": full ledger jobs, "queue_nodes": work queue nodes, "capabilities": CAPABILITIES}

    option       {"provider", "kind": in_place | copy | rebuild | upstream | follow_up,
                  "capability", "parameters", "nodes": [queue nodes to merge],
                  "gain_per_minute", "cost": {item: count} | None, "seconds_saved",
                  "feasible": bool, "reason"}

A provider that wraps a specialised planner keeps that planner's rule (wait
vs spend, build ahead, critical claims): its receipt holds the decision and its
``*_nodes`` function the queue nodes, so there is one comparison and one
receipt. Providers (``PROVIDERS``, first match wins):

- ``science``: packs and labs. Hook: ``science_growth.growth_options`` when the
  module defines it (every pack type); until then the staged red block's
  ``science_growth`` receipt and ``progression.science_growth_nodes``.
- ``plates``: iron/copper plates and their ores (``throughput_growth``; transport at
  the district: ``head_rework``).
- ``mine``: coal, stone and patch trunks (``mine_trunk`` receipt and nodes).
- ``steel``: the planned steel block (``progression.steel_capacity_nodes``); transport at
  a furnace block: the ``belt_bandwidth`` rework (no capability yet).
- ``produce_row``: generic, every ``production_row_*`` block and an intermediate
  without a producer: a ``produce`` row at a higher rate (the row extends in place).
- ``power``: no power (``steam_expansion`` is a roadmap step, follow-up only).

``assess`` turns the options into one proposal with its gate (receipt
``bottlenecks[].proposal``); ``growth_nodes`` (supervisor growth pass) merges
the proposed nodes every cycle, independent of the roadmap queue gate.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, Mapping

SETTLE_TICKS = 3600  # throughput_growth.SETTLE_TICKS: a landed growth settles before the next one
MIN_LOST_SECONDS = 10.0  # generic provider: grow only above this consumer loss per minute
GENERIC_STEP_MAX = 2.0  # generic produce row: at most double its rate per package
OPEN = frozenset({"pending", "ready", "running", "waiting"})
TERMINAL = frozenset({"succeeded", "blocked", "failed"})
PLATES = {"iron-plate": "iron-ore", "copper-plate": "copper-ore"}
LAB_BLOCKS = frozenset({"laboratories", "automation_science"})
RED = "automation-science-pack"


def _option(
    provider, *, kind="in_place", capability=None, parameters=None, nodes=(), feasible=True, reason="", **extra
):
    return {
        "provider": provider,
        "kind": kind,
        "capability": capability,
        "parameters": dict(parameters) if isinstance(parameters, Mapping) else parameters,
        "nodes": list(nodes),
        "gain_per_minute": extra.get("gain_per_minute"),
        "cost": extra.get("cost"),
        "seconds_saved": extra.get("seconds_saved"),
        "feasible": bool(feasible),
        "reason": str(reason)[:240],
    }


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ----- science (packs, labs) -----------------------------------------------------------------
def _science_matches(b) -> bool:
    item = str(b.get("item") or "")
    return item.endswith("science-pack") or b.get("kind") == "consumer_capacity" or b.get("block") in LAB_BLOCKS


def science_options(b, observation, state) -> list[dict]:
    """Every pack type through ``science_growth.growth_options`` once defined; red from its receipt until then."""
    hook = getattr(import_module(__package__ + ".science_growth"), "growth_options", None)
    if callable(hook):
        return list(hook(b, observation, state) or [])
    pack = b.get("item")
    red = pack == RED or b.get("block") in LAB_BLOCKS  # the staged red block's own rows and labs
    if not red:
        return [
            _option(
                "science",
                feasible=False,
                reason=f"no growth rule for {pack} yet: science_growth.growth_options(bottleneck, observation, state)"
                " is not defined",
            )
        ]
    row = observation.get("science_growth")
    if not isinstance(row, Mapping):
        return [_option("science", feasible=False, reason="no science_growth receipt (no staged red block)")]
    if not row.get("grow"):
        step = row.get("next_step") if isinstance(row.get("next_step"), Mapping) else None
        why = str(row.get("reason") or "science_growth waits")
        if step:
            why += f"; beyond the frame: {step.get('choice') or step.get('reason')}"
        return [_option("science", feasible=False, reason=why)]
    from .progression import science_growth_nodes

    nodes = science_growth_nodes(state["jobs"], state["queue_nodes"], observation, state["capabilities"])
    return [
        _option(
            "science",
            capability="science_growth",
            parameters={"labs": row.get("target_labs"), "machines": row.get("target_machines")},
            nodes=nodes,
            reason=str(row.get("reason") or ""),
            seconds_saved=_number((row.get("decision") or {}).get("saving_seconds"))
            if isinstance(row.get("decision"), Mapping)
            else None,
        )
    ]


# ----- plates (throughput_growth, head_rework) -------------------------------------------------
def _plate(b) -> str | None:
    item = b.get("item")
    if item in PLATES:
        return item
    for plate, ore in PLATES.items():
        if item == ore:
            return plate
    return None


def _plates_matches(b) -> bool:
    return _plate(b) is not None


def plates_options(b, observation, state) -> list[dict]:
    plate = _plate(b)
    resource = PLATES[plate]
    options = []
    if b.get("kind") == "transport" and b.get("block") == "smelting_district":
        rows = (
            (observation.get("feed_head_rework") or {}).get("banks")
            if isinstance(observation.get("feed_head_rework"), Mapping)
            else None
        )
        row = rows.get(resource) if isinstance(rows, Mapping) else None
        if isinstance(row, Mapping) and row.get("ready"):
            from .head_rework import feed_head_rework_nodes

            nodes = [
                n
                for n in feed_head_rework_nodes(state["jobs"], state["queue_nodes"], observation, state["capabilities"])
                if (n.get("parameters") or {}).get("resource") == resource
            ]
            options.append(
                _option(
                    "plates",
                    kind="rebuild",
                    capability="feed_head_rework",
                    parameters={"block": "smelting_district", "resource": resource},
                    nodes=nodes,
                    reason=f"bank head rework pays: {row.get('reason')}",
                )
            )
        elif isinstance(row, Mapping):
            options.append(
                _option("plates", kind="rebuild", feasible=False, reason=f"feed head rework: {row.get('reason')}")
            )
    receipt = observation.get("throughput_growth")
    rows = receipt.get("plates") if isinstance(receipt, Mapping) else None
    row = next((r for r in rows or () if isinstance(r, Mapping) and r.get("plate") == plate), None)
    if row is None:
        options.append(
            _option("plates", feasible=False, reason=f"no throughput_growth row for {plate} (no paired district bank)")
        )
        return options
    if not row.get("grow"):
        options.append(_option("plates", feasible=False, reason=str(row.get("reason") or "throughput_growth waits")))
        return options
    from .throughput_growth import throughput_growth_nodes

    nodes = []
    for critical in (True, False):
        nodes += [
            n
            for n in throughput_growth_nodes(
                state["jobs"], state["queue_nodes"], observation, state["capabilities"], critical=critical
            )
            if (n.get("parameters") or {}).get("resource") == resource
        ]
    times = row.get("times") if isinstance(row.get("times"), Mapping) else {}
    package = row.get("package") if isinstance(row.get("package"), Mapping) else {}
    options.append(
        _option(
            "plates",
            kind="in_place",
            capability=row.get("capability"),
            parameters=row.get("parameters"),
            nodes=nodes,
            reason=str(row.get("reason") or ""),
            seconds_saved=_number(times.get("saving_seconds")),
            gain_per_minute=_number(package.get("gain_per_minute")),
            cost=(row.get("step") or {}).get("cost") if isinstance(row.get("step"), Mapping) else None,
        )
    )
    return options


# ----- mines (mine_trunk) ------------------------------------------------------------------------
def _mine_matches(b) -> bool:
    item = str(b.get("item") or "")
    return item in ("coal", "stone") or item.endswith("-ore") or "_mining" in str(b.get("block") or "")


def mine_options(b, observation, state) -> list[dict]:
    resource = b.get("item")
    receipt = observation.get("mine_trunks")
    rows = receipt.get("trunks") if isinstance(receipt, Mapping) else None
    row = next((r for r in rows or () if isinstance(r, Mapping) and r.get("resource") == resource), None)
    if row is None:
        return [_option("mine", feasible=False, reason=f"no mine trunk ledger row for {resource}")]
    if not row.get("grow") and not row.get("second_lane_required"):
        return [
            _option(
                "mine",
                feasible=False,
                reason=f"trunk declared demand {row.get('demand_per_minute')}/min within supply {row.get('supply_per_minute')}/min",
            )
        ]
    from .mine_trunk import mine_trunk_nodes

    nodes = [
        n
        for n in mine_trunk_nodes(state["jobs"], state["queue_nodes"], observation, state["capabilities"])
        if (n.get("parameters") or {}).get("resource") == resource
    ]
    return [
        _option(
            "mine",
            capability=nodes[0]["capability"] if nodes else "mining_outpost",
            parameters=nodes[0]["parameters"] if nodes else None,
            nodes=nodes,
            reason=f"trunk demand {row.get('demand_per_minute')}/min above supply {row.get('supply_per_minute')}/min",
        )
    ]


# ----- steel / brick furnace blocks ----------------------------------------------------------------
def _steel_matches(b) -> bool:
    return b.get("item") in ("steel-plate", "stone-brick") or b.get("block") in ("steel_production", "brick_production")


def steel_options(b, observation, state) -> list[dict]:
    if b.get("kind") == "transport":
        rework = (
            (observation.get("belt_bandwidth") or {}).get("rework")
            if isinstance(observation.get("belt_bandwidth"), Mapping)
            else None
        )
        row = next((r for r in rework or () if isinstance(r, Mapping) and r.get("block") == b.get("block")), None)
        why = f"feed head delivers less than the rows need ({row.get('cause')}); " if row else ""
        return [
            _option("steel", kind="rebuild", feasible=False, reason=why + "rework_feed_head is not a capability yet")
        ]
    if b.get("item") != "steel-plate":
        return [_option("steel", feasible=False, reason=f"no growth capability for {b.get('item')} blocks yet")]
    from .progression import steel_capacity_nodes

    nodes = steel_capacity_nodes(state["jobs"], state["queue_nodes"], observation, state["capabilities"])
    if not nodes:
        return [
            _option(
                "steel", feasible=False, reason="steel_capacity: declared claims below the grow threshold or block full"
            )
        ]
    return [
        _option(
            "steel",
            capability="steel_capacity",
            parameters=nodes[0]["parameters"],
            nodes=nodes,
            reason=nodes[0]["reason"],
        )
    ]


# ----- power -----------------------------------------------------------------------------------
def _power_matches(b) -> bool:
    return b.get("kind") == "no_power"


def power_options(b, observation, state) -> list[dict]:
    return [
        _option("power", kind="follow_up", feasible=False, reason="power growth is the roadmap's steam_expansion step")
    ]


PROVIDERS: list[tuple[str, Callable[[Mapping], bool], Callable[..., list]]] = [
    ("power", _power_matches, power_options),
    ("science", _science_matches, science_options),
    ("steel", _steel_matches, steel_options),  # before plates: a furnace block's iron feed is its own
    ("plates", _plates_matches, plates_options),
    ("mine", _mine_matches, mine_options),
]


def register(
    name: str, matches: Callable[[Mapping], bool], provider: Callable[..., list], *, first: bool = False
) -> None:
    """Add a provider (a block type's ``growth_options``); ``first`` puts it before the built-in ones."""
    global PROVIDERS
    rows = [p for p in PROVIDERS if p[0] != name]
    PROVIDERS = ([(name, matches, provider)] + rows) if first else rows + [(name, matches, provider)]


def provider_for(b: Mapping):
    return next(((name, fn) for name, matches, fn in PROVIDERS if matches(b)), (None, None))


# ----- gating ----------------------------------------------------------------------------------
def _lane(row: Mapping) -> str:
    from .jobs import admission_lane

    return admission_lane(str(row.get("capability")), row.get("parameters"))


def _gate(option: Mapping, state: Mapping, tick) -> str | None:
    """Why a feasible option may not be proposed now: in flight, settling, or nothing new to merge."""
    capability = option.get("capability")
    if not capability:
        return None
    lane = _lane(option)
    for node in state.get("queue_nodes") or ():
        if node.get("status", "pending") in OPEN and _lane(node) == lane and node.get("capability") == capability:
            return "growth_in_flight"
    for job in state.get("jobs") or ():
        if job.get("capability") != capability or _lane(job) != lane:
            continue
        if job.get("status") in OPEN or job.get("status") == "paused":
            return "growth_in_flight"
        last = job.get("last_tick")
        if (
            job.get("status") == "succeeded"
            and isinstance(tick, int)
            and isinstance(last, int)
            and tick - last < SETTLE_TICKS
        ):
            return "settling"
    if not option.get("nodes"):
        return "provider_rule_holds"  # the provider's node rule refuses now (requires, stalled before, same id)
    return None


def _transitional(b) -> str | None:
    from .bottleneck_audit import TRANSITIONAL_BLOCKS

    if b.get("block") in TRANSITIONAL_BLOCKS:
        return "transitional_block"
    consumers = [c for c in b.get("consumers") or () if c.get("block") != "job"]
    if (
        consumers
        and all(c.get("block") in TRANSITIONAL_BLOCKS for c in consumers)
        and not any(c.get("block") == "job" for c in b.get("consumers") or ())
    ):
        return "consumer_transitional"
    return None


def options(b: Mapping, observation: Mapping, state: Mapping) -> tuple[str | None, list[dict]]:
    name, fn = provider_for(b)
    if fn is None:
        return None, []
    return name, list(fn(b, observation, state) or [])


def assess(b: Mapping, observation: Mapping, state: Mapping) -> dict[str, Any]:
    """One proposal per bottleneck: the chosen package (most seconds saved, then first) or the gate that holds it."""
    transitional = _transitional(b)
    if transitional:
        return {
            "status": "wait",
            "gate": transitional,
            "reason": f"{b.get('block')} is temporary; its replacement grows",
        }
    name, found = options(b, observation, state)
    if name is None:
        return {
            "status": "wait",
            "gate": "no_provider",
            "reason": f"no growth provider for {b.get('block')} ({b.get('item')}, {b.get('kind')})",
        }
    feasible = [o for o in found if o.get("feasible")]
    if not feasible:
        best = found[0] if found else {}
        return {
            "status": "wait",
            "provider": name,
            "gate": f"{name}_wait",
            "reason": best.get("reason") or "no option",
            "options": len(found),
        }
    chosen = sorted(feasible, key=lambda o: -(o.get("seconds_saved") or 0.0))[0]
    gate = _gate(chosen, state, observation.get("tick"))
    row = {
        "provider": name,
        "option": {
            k: chosen.get(k) for k in ("kind", "capability", "parameters", "gain_per_minute", "seconds_saved", "cost")
        },
        "reason": chosen.get("reason"),
        "seconds_saved": chosen.get("seconds_saved"),
        "options": len(found),
    }
    if gate:
        return dict(row, status="wait", gate=gate)
    return dict(row, status="propose", gate=None, nodes=[n.get("id") for n in chosen["nodes"]])


def growth_nodes(bottlenecks, jobs, queue_nodes, observation, capabilities, *, limit: int = 2) -> list[dict]:
    """Queue nodes for the ranked bottlenecks: one package per bottleneck, at most ``limit`` packages."""
    state = {"jobs": list(jobs), "queue_nodes": list(queue_nodes), "capabilities": capabilities}
    known = {n.get("id") for n in queue_nodes}
    out: list[dict] = []
    packages = 0
    for b in bottlenecks or ():
        if packages >= limit or not isinstance(b, Mapping) or not b.get("lost_seconds_per_minute"):
            continue
        if _transitional(b):
            continue
        name, found = options(b, observation, state)
        feasible = sorted((o for o in found if o.get("feasible")), key=lambda o: -(o.get("seconds_saved") or 0.0))
        if not feasible or _gate(feasible[0], state, observation.get("tick")):
            continue
        nodes = [n for n in feasible[0]["nodes"] if n.get("id") not in known]
        if not nodes:
            continue
        out += nodes
        known |= {n.get("id") for n in nodes}
        state["queue_nodes"] = list(state["queue_nodes"]) + [dict(n, status="pending") for n in nodes]
        packages += 1
    return out
