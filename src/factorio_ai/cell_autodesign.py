"""Auto-design optimal cells from the live game state and store them in the cell library.

This is the deterministic layout pipeline driven by the REAL game: it reads the current production
deficits (what the factory is short on), the machines available, and the power headroom, then for
each top-deficit item runs capacity planning + compile + place + pre-check and saves the design to
the library (visible on /factorio/layouts).

Intentionally DECOUPLED from the autopilot/strategy/layout-LLM paths — it only reads an observation
and writes library files, so it cannot break the running autopilot. The LLM site-constraint
refinement (exact anchor + box) layers on top later; here the box is auto-sized and the anchor is
not committed to the live game (designs are simulation/library artifacts).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import capacity_planner, cell_compiler, cell_pipeline, knowledge


def _available_machines(observation: dict[str, Any]) -> list[str]:
    """Machines the factory can use now: any crafting machine built or in inventory, plus a
    baseline of the always-available starter machines."""
    names: set[str] = {"assembling-machine-1", "stone-furnace"}
    known = set()
    machines_tbl = knowledge._load_profiles()[0]  # name -> MachineProfile
    known.update(machines_tbl.keys())
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for e in entities:
        if isinstance(e, dict) and str(e.get("name") or "") in known:
            names.add(str(e["name"]))
    inv = observation.get("inventory") if isinstance(observation.get("inventory"), dict) else {}
    for name in inv:
        if str(name) in known:
            names.add(str(name))
    # research-gated common upgrades: include AM2 once automation-ish tech is present (best-effort).
    research = observation.get("research") if isinstance(observation.get("research"), dict) else {}
    techs = research.get("technologies") if isinstance(research.get("technologies"), dict) else {}
    if any(isinstance(v, dict) and v.get("researched") for v in techs.values()):
        names.add("assembling-machine-2")
    return sorted(names)


def _power_situation(observation: dict[str, Any]) -> cell_compiler.PowerSituation:
    try:
        from .monitor import estimate_power_networks

        nets = estimate_power_networks(observation)
        generation = sum(float(getattr(n, "generation_kw", 0.0) or 0.0) for n in nets)
        demand = sum(float(getattr(n, "demand_kw", 0.0) or 0.0) for n in nets)
        headroom = generation - demand
        sat = min(1.0, generation / demand) if demand > 0 else 1.0
        if headroom <= 0:
            headroom = float("inf")  # unknown / no networks yet -> don't over-constrain
        return cell_compiler.PowerSituation(available_headroom_kw=headroom, satisfaction=sat, size_vs_power_pref=0.5)
    except Exception:  # noqa: BLE001
        return cell_compiler.PowerSituation()


def design_cells(
    cfg: Any,
    observation: dict[str, Any],
    *,
    objective: str = "launch_rocket_program",
    top_n: int = 3,
) -> dict[str, Any]:
    """Design + store cells for the top ``top_n`` production deficits in the live game."""
    from .monitor import estimate_factory_sites, estimate_production, production_target_status
    from .targets import load_targets

    runtime_dir = Path(getattr(cfg, "runtime_dir", "runtime"))
    targets = load_targets(runtime_dir, objective).per_minute
    if not targets:
        return {"ok": False, "reason": "no production targets configured", "designed": []}

    status = production_target_status(targets, estimate_production(observation))
    deficits = sorted(
        (r for r in status.get("items", []) if isinstance(r, dict) and float(r.get("deficit_per_minute") or 0.0) > 0),
        key=lambda r: -float(r.get("deficit_per_minute") or 0.0),
    )[:max(1, top_n)]
    if not deficits:
        return {"ok": True, "reason": "all targets satisfied", "designed": []}

    machines = _available_machines(observation)
    power = _power_situation(observation)
    try:
        sites = [s.to_dict() for s in estimate_factory_sites(observation)]
    except Exception:  # noqa: BLE001
        sites = []

    designed: list[dict[str, Any]] = []
    for row in deficits:
        item = str(row.get("item") or "")
        rate = round(float(row.get("deficit_per_minute") or 0.0), 3)
        if not item or rate <= 0:
            continue
        plan = capacity_planner.plan_capacity(item, rate, sites, available_machines=machines, power_situation=power)
        out = cell_pipeline.build_and_store(
            runtime_dir, item, rate,
            available_machines=machines, power_situation=power,
            sandbox_status=f"autodesign:{plan['mode']}",
        )
        record = out.get("record") or {}
        designed.append({
            "item": item,
            "rate": rate,
            "mode": plan.get("mode"),
            "stored": bool(record),
            "key": record.get("key"),
            "precheck": (out.get("precheck") or {}).get("status"),
            "reason": out.get("reason") or plan.get("reason"),
        })
    return {"ok": True, "designed": designed, "count": len(designed),
            "available_machines": machines}
