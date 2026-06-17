"""Capacity planner: decide whether new demand for an item should EXPAND an existing same-product
site or build a NEW site (constraint C7 — prefer expansion, build new only when expansion can't
fit). Deterministic; pairs with the rate-carrying strategy (a recommendation says
"+<rate>/min of <item>") and feeds the cell compiler/placer/territory.

Pure module: no RCON / file I/O.
"""

from __future__ import annotations

from typing import Any

from . import cell_compiler, knowledge, territory


def plan_capacity(
    item: str,
    additional_rate: float,
    sites: list[dict[str, Any]] | None,
    *,
    available_machines: list[str] | None = None,
    available_modules: list[str] | None = None,
    power_situation: cell_compiler.PowerSituation | None = None,
) -> dict[str, Any]:
    """Plan how to add ``additional_rate``/min of ``item``.

    Returns ``{mode: "expand"|"new", item, additional_rate, spec, site_id?, headroom_area?,
    needed_area, reason}``. Prefers expanding an existing same-item site that still has growth
    headroom in its reserved box; otherwise recommends a new site.
    """

    spec = cell_compiler.compile_cell(
        item, additional_rate,
        available_machines=available_machines,
        available_modules=available_modules,
        power_situation=power_situation,
    )
    needed_area = float(spec.footprint.get("area") or 0.0)

    same = [
        s for s in (sites or [])
        if isinstance(s, dict)
        and (s.get("target_item") == item or s.get("item") == item)
        and str(s.get("status") or "") not in {"blocked", "removed"}
    ]

    # Prefer the same-item site with the MOST free growth headroom that fits the new machines.
    best: tuple[float, dict[str, Any]] | None = None
    for s in same:
        reserved = territory.rect_from_bounds(s.get("reserved_box"))
        if reserved is None:
            continue
        bounds = territory.rect_from_bounds(s.get("bounds"))
        used_area = bounds.area if bounds is not None else 0.0
        headroom = max(0.0, reserved.area - used_area)
        if headroom >= needed_area and (best is None or headroom > best[0]):
            best = (headroom, s)

    if best is not None:
        headroom, site = best
        return {
            "mode": "expand",
            "item": item,
            "additional_rate": round(additional_rate, 3),
            "site_id": site.get("site_id"),
            "headroom_area": round(headroom, 1),
            "needed_area": round(needed_area, 1),
            "spec": spec.to_dict(),
            "reason": f"existing {item} site has {headroom:.0f} free tiles >= {needed_area:.0f} needed",
        }

    reason = (
        f"no existing {item} site with >= {needed_area:.0f} free tiles"
        if same else f"no existing {item} site"
    )
    return {
        "mode": "new",
        "item": item,
        "additional_rate": round(additional_rate, 3),
        "needed_area": round(needed_area, 1),
        "spec": spec.to_dict(),
        "reason": reason,
    }
