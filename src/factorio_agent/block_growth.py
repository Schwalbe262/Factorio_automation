"""Growth options of built production blocks as data (a ``growth_options`` provider; pure, read-only).

The science-growth and bottleneck machinery ask "how can this block make more, and what does
it cost?" without knowing each controller. Every option is one row::

    {"block", "capability", "item", "kind", "rate_now", "step_rate", "max_rate_in_place",
     "slots", "cost": {"items": {name: count}}, "action": {"capability", "parameters"} | None,
     "limit", "beyond"}

* ``produce`` rows (``production-row-<item>.json``, also the fluid and lane-merge rows): kind
  ``in_place`` while reserved slots remain; one step = one more machine (its machine and arms),
  the action is the same ``produce`` request at the higher rate (``ProductionRows._extend``);
  without a slot the kind is ``rebuild`` (``limit`` says why).
* ``advanced_circuit_production`` / ``chemical_science``: a new block is sized by the job's
  ``rate_per_minute`` (two to four machines); a built block reports its rate, its reserved slots
  (``core.growth_slots``) and the slot unit's cost with kind ``rebuild`` (no in-place extension
  in the controller yet).

Callers rank and choose; this module never acts.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

FIXED_BLOCKS = {
    "advanced_circuit_production": ("advanced-circuits.json", "advanced-circuit"),
    "chemical_science": ("chemical-science.json", "chemical-science-pack"),
}


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _row_option(state: dict) -> dict | None:
    line = state.get("line") or {}
    main = line.get("main") or {}
    count = int(main.get("count") or 0)
    if count < 1 or not isinstance(main.get("rated_per_minute"), (int, float)):
        return None
    per = float(main["rated_per_minute"]) / count
    slots = int((main.get("growth") or {}).get("remaining_slots") or 0)
    layout = main.get("layout") or {}
    tail = line.get("tail") or {}
    arm = (tail.get("arm") or {}).get("name")
    cap = float(tail.get("stock_rate_per_minute") or 0) if arm else math.inf
    rate_now = float(main["rated_per_minute"])
    ceiling = min(per * (count + slots), cap if arm else per * (count + slots))
    items: dict[str, int] = {main.get("machine", "assembling-machine-1"): 1}
    for name, n in (
        (layout.get("inserter"), layout.get("near_arms", 0) + layout.get("output_arms", 0)),
        (layout.get("long_inserter"), layout.get("far_arms", 0)),
    ):
        if name and n:
            items[name] = items.get(name, 0) + int(n)
    if layout.get("fluid"):
        items[layout.get("underground_pipe", "pipe-to-ground")] = 2
    grows = slots > 0 and rate_now + 1e-9 < ceiling
    item = state.get("item")
    parameters = {"item": item, "rate_per_minute": round(min(600.0, rate_now + per), 3)}
    if (tail.get("mode") or "") == "belt":
        parameters["output"] = "belt"
    return {
        "block": "production_row_" + str(item),
        "capability": "produce",
        "item": item,
        "kind": "in_place" if grows else "rebuild",
        "rate_now": round(rate_now, 3),
        "step_rate": round(per, 3),
        "max_rate_in_place": round(ceiling, 3),
        "slots": slots,
        "cost": {"items": items if grows else {}},
        "action": {"capability": "produce", "parameters": parameters} if grows else None,
        "limit": "reserved_slots" if grows else ("output_inserter" if slots and arm else "no_reserved_slot"),
        "beyond": "partial_rebuild",
    }


def growth_options(root, world_id: str | None = None) -> list[dict]:
    """Every built block's growth option under the runtime folder ``root`` (module doc)."""
    root = Path(root)
    found = []
    for path in sorted(root.glob("production-row-*.json")):
        state = _load(path)
        if not isinstance(state, dict) or (world_id is not None and state.get("world_id") != world_id):
            continue
        option = _row_option(state)
        if option is not None:
            found.append(option)
    for capability, (name, item) in FIXED_BLOCKS.items():
        state = _load(root / name)
        if not isinstance(state, dict) or (world_id is not None and state.get("world_id") not in (None, world_id)):
            continue
        found.append(_block_option(capability, item, state))
    return found


def _block_option(capability: str, item: str, state: dict) -> dict:
    """A two-to-four machine block (advanced circuits, chemical science): its rate, its free slots.

    A new block is sized by the job's ``rate_per_minute`` (``count_for_rate``). A built block keeps
    its reserved slots (``core.growth_slots``, the ``pack_growth.block_view`` contract) but its
    controller has no in-place extension yet: kind ``rebuild`` with the slot unit as the cost."""
    core = state.get("core") if isinstance(state.get("core"), dict) else {}
    rated = state.get("rated") if isinstance(state.get("rated"), dict) else {}
    key = "advanced_circuits_per_minute" if capability == "advanced_circuit_production" else "science_per_minute"
    rate = rated.get(key)
    machines = len(core.get("red_machines") or core.get("machines") or ()) or None
    slots = [g for g in core.get("growth_slots") or () if isinstance(g, list)]
    per = rate / machines if isinstance(rate, (int, float)) and machines else None
    unit: dict[str, int] = {}
    for e in slots[0] if slots else ():
        unit[e["name"]] = unit.get(e["name"], 0) + 1
    return {
        "block": capability,
        "capability": capability,
        "item": item,
        "kind": "rebuild" if state.get("plan") else "new",
        "rate_now": round(rate, 3) if isinstance(rate, (int, float)) else None,
        "step_rate": round(per, 3) if per else None,
        "max_rate_in_place": round(per * (machines + len(slots)), 3) if per else None,
        "slots": len(slots),
        "cost": {"items": unit},
        "action": None,
        "limit": "no_in_place_extension" if slots else "no_reserved_slot",
        "beyond": f"extend {capability} into its reserved slots (not implemented) or a second block",
    }
