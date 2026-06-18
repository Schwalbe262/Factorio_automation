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
    """Machines the factory can actually use right now.

    A machine qualifies if it is (a) an always-available starter, (b) built in the world, (c) in
    inventory, or (d) its CRAFT RECIPE is enabled (i.e. the unlocking tech is researched), read
    from the live recipe-unlock snapshot ``observation['recipe_unlocks']``.

    The previous heuristic added ``assembling-machine-2`` whenever *any* technology was researched,
    which is essentially always true on a running base, so unresearched upgrade machines leaked into
    designs (the user saw AM2 used before it was unlocked). Gating on the real per-recipe enabled
    flag fixes that generally for AM2/AM3/steel-furnace/electric-furnace without hardcoding tech."""
    names: set[str] = {"assembling-machine-1", "stone-furnace"}
    machines_tbl = knowledge._load_profiles()[0]  # name -> MachineProfile
    known = set(machines_tbl.keys())
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for e in entities:
        if isinstance(e, dict) and str(e.get("name") or "") in known:
            names.add(str(e["name"]))
    inv = observation.get("inventory") if isinstance(observation.get("inventory"), dict) else {}
    for name in inv:
        if str(name) in known:
            names.add(str(name))
    # Upgrade machines: include ONLY when their craft recipe is actually enabled (researched).
    unlocks = observation.get("recipe_unlocks") if isinstance(observation.get("recipe_unlocks"), dict) else {}
    for name, state in unlocks.items():
        if str(name) in known and isinstance(state, dict) and state.get("enabled"):
            names.add(str(name))
    return sorted(names)


def _long_inserter_available(observation: dict[str, Any]) -> bool:
    """True when the long-handed inserter is craftable (needed for clean multi-input belt routing)."""
    unlocks = observation.get("recipe_unlocks") if isinstance(observation.get("recipe_unlocks"), dict) else {}
    state = unlocks.get("long-handed-inserter")
    if isinstance(state, dict) and state.get("enabled"):
        return True
    # built/in inventory also counts
    inv = observation.get("inventory") if isinstance(observation.get("inventory"), dict) else {}
    if "long-handed-inserter" in inv:
        return True
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    return any(isinstance(e, dict) and str(e.get("name") or "") == "long-handed-inserter" for e in entities)


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
    long_inserter = _long_inserter_available(observation)
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
            long_inserter_available=long_inserter,
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
