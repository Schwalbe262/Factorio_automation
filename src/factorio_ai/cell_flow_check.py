"""Cheap deterministic pre-check for a placed cell — the filter that runs BEFORE the expensive
real-Factorio sandbox so obviously-broken layouts never reach it.

It verifies a small set of physical INVARIANTS (not hundreds of placement rules — those are baked
into the placer templates by construction):

  1. box fit         — the placed cell fits its reserved box (C5 feedback if not)
  2. power coverage   — every machine overlaps a pole's supply area + poles are wire-connected
  3. operability      — each recipe machine has its input/output inserters wired to belts/sources
                        (reuses planner._blueprint_operability_report — the proven static check)
  4. belt throughput  — each input/output belt lane's capacity >= the steady-state flow
  5. power budget      — total draw <= available headroom (when a power situation is supplied)

Everything else (collisions, real power propagation, real throughput) is left to the sandbox,
which enforces all of Factorio's actual rules for free. Pure module: no RCON / file I/O.
"""

from __future__ import annotations

from typing import Any

from . import knowledge
from .cell_compiler import CellSpec, PowerSituation
from .cell_placer import PlacedCell


def precheck_cell(
    spec: CellSpec,
    placed: PlacedCell,
    *,
    power_situation: PowerSituation | None = None,
) -> dict[str, Any]:
    """Return ``{status: pass|warn|fail, reasons: [...], checks: {...}}``."""

    reasons: list[str] = []
    warns: list[str] = []
    checks: dict[str, str] = {}

    # 1. box fit
    if placed.fits:
        checks["box_fit"] = "pass"
    else:
        checks["box_fit"] = "fail"
        reasons.append(
            f"cell needs {placed.required_box.get('width')}x{placed.required_box.get('height')} "
            f"but does not fit the reserved box"
        )

    # 2. power-pole coverage + connectivity
    if placed.power_coverage_ok:
        checks["power_coverage"] = "pass"
    else:
        checks["power_coverage"] = "fail"
        reasons.append("power-pole coverage or connectivity failed (a machine is unpowered or poles are not wired)")

    # 3. operability (inserters wired to belts/sources) — reuse the proven static report.
    from . import planner  # lazy import (planner is heavy); no import cycle.

    report = planner._blueprint_operability_report(placed.entities)
    checks["operability"] = report.get("status", "warning")
    if report.get("status") == "fail":
        reasons.extend(str(e) for e in report.get("errors", [])[:4])

    # 4. belt throughput: every solid I/O lane must carry its steady-state flow.
    belt_status = "pass"
    for io in list(spec.inputs) + list(spec.outputs):
        if io.is_fluid or not io.belt_tier:
            continue
        belt = knowledge.belt_profile(io.belt_tier)
        if belt is None:
            continue
        capacity_per_min = belt.items_per_second * max(1, io.belt_lanes_needed) * 60.0
        if capacity_per_min + 1e-6 < io.per_minute:
            belt_status = "fail"
            reasons.append(
                f"{io.item} belt {io.belt_tier}x{io.belt_lanes_needed} carries {capacity_per_min:.0f}/min "
                f"< required {io.per_minute:.0f}/min"
            )
        elif io.belt_lanes_needed > 1:
            warns.append(f"{io.item} needs {io.belt_lanes_needed} belt lanes (underground-mix)")
    checks["belt_throughput"] = belt_status

    # 5. power budget
    if power_situation is not None and power_situation.available_headroom_kw != float("inf"):
        if spec.total_power_kw <= power_situation.available_headroom_kw:
            checks["power_budget"] = "pass"
        else:
            checks["power_budget"] = "warn"
            warns.append(
                f"draw {spec.total_power_kw:.0f}kW exceeds headroom {power_situation.available_headroom_kw:.0f}kW"
            )

    status = "fail" if reasons else ("warn" if warns else "pass")
    return {"status": status, "reasons": reasons, "warnings": warns, "checks": checks}
