"""End-to-end deterministic cell pipeline: compile -> place -> pre-check -> encode -> store.

This is the deterministic core the user asked for (steps 1-5), independent of the LLM and the
sandbox: given a target item + rate (and the available machines/modules/box), it produces a
concrete, pre-checked blueprint and (optionally) saves it to the layout library for the dashboard.
The LLM (site location + box) and the real sandbox are the OPTIONAL outer layers wired in D4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import blueprints, cell_compiler, cell_flow_check, cell_library, cell_placer


def design_cell(
    item: str,
    rate: float,
    *,
    available_machines: list[str] | None = None,
    available_modules: list[str] | None = None,
    belt_tiers_available: list[str] | None = None,
    power_situation: cell_compiler.PowerSituation | None = None,
    box: cell_placer.BoundingBox | None = None,
    pole: str = "small-electric-pole",
    long_inserter_available: bool = True,
) -> dict[str, Any]:
    """Compile + place + pre-check a cell. Returns objects + a blueprint string + the pre-check."""
    spec = cell_compiler.compile_cell(
        item, rate,
        available_machines=available_machines,
        available_modules=available_modules,
        belt_tiers_available=belt_tiers_available,
        power_situation=power_situation,
    )
    if not spec.ok:
        return {"ok": False, "spec": spec, "placed": None, "precheck": None,
                "blueprint": None, "reason": "; ".join(spec.warnings) or "compile failed"}

    placed = cell_placer.place_cell(spec, box, pole=pole, long_inserter_available=long_inserter_available)
    precheck = cell_flow_check.precheck_cell(spec, placed, power_situation=power_situation)
    blueprint = (
        blueprints.encode_blueprint_entities(f"{item}@{rate:g}", placed.entities)
        if placed.entities else None
    )
    return {
        "ok": precheck["status"] != "fail",
        "spec": spec,
        "placed": placed,
        "precheck": precheck,
        "blueprint": blueprint,
        "reason": "; ".join(precheck.get("reasons", [])) or "",
    }


def build_and_store(
    runtime_dir: Path,
    item: str,
    rate: float,
    *,
    sandbox_status: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run design_cell and, if a blueprint was produced, persist it to the cell library.
    Returns ``{ok, record?, precheck, reason}``."""
    result = design_cell(item, rate, **kwargs)
    if not result["blueprint"]:
        return {"ok": False, "record": None, "precheck": result.get("precheck"), "reason": result.get("reason")}
    status = sandbox_status or f"precheck:{result['precheck']['status']}"
    record = cell_library.save_design(
        runtime_dir, result["spec"],
        blueprint_string=result["blueprint"],
        sandbox_status=status,
        placed=result["placed"],
    )
    return {"ok": result["ok"], "record": record, "precheck": result["precheck"], "reason": result.get("reason")}
