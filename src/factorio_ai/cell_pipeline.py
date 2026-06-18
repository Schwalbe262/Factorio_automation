"""End-to-end deterministic cell pipeline: compile -> place -> pre-check -> encode -> store.

This is the deterministic core the user asked for (steps 1-5), independent of the LLM and the
sandbox: given a target item + rate (and the available machines/modules/box), it produces a
concrete, pre-checked blueprint and (optionally) saves it to the layout library for the dashboard.
The LLM (site location + box) and the real sandbox are the OPTIONAL outer layers wired in D4.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import blueprints, cell_compiler, cell_flow_check, cell_library, cell_placer


@dataclass
class SandboxConfirm:
    """Opt-in sandbox selection: validate the top-K candidate archetypes in the real Factorio
    sandbox and pick the one that actually builds + produces the most output (the user's
    'sandbox judges the top few' choice)."""
    cfg: Any
    observation: dict[str, Any]
    ticks: int = 600
    top_k: int = 3
    cleanup: bool = True


def _rank_key(precheck: dict[str, Any]) -> tuple:
    """Rank candidates: valid first (no fail), then pass over warn, then most compact (rect_fill),
    then fewest entities/inserters. Higher tuple = better."""
    m = precheck.get("metrics", {}) if isinstance(precheck.get("metrics"), dict) else {}
    status = precheck.get("status")
    return (
        1 if status != "fail" else 0,
        1 if status == "pass" else 0,
        float(m.get("rect_fill", 0.0)),
        -int(m.get("entity_count", 0)),
        -int(m.get("inserter_count", 0)),
    )


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
    available_inserters: set[str] | None = None,
    sandbox: SandboxConfirm | None = None,
) -> dict[str, Any]:
    """Compile -> generate candidate archetypes -> precheck + rank -> (optional) sandbox-pick the best.
    Returns the winning placement + blueprint + precheck, plus the full candidate list."""
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

    candidates = cell_placer.place_cell_candidates(
        spec, box, pole=pole, long_inserter_available=long_inserter_available,
        available_inserters=available_inserters,
    )
    scored = []
    for placed in candidates:
        if not placed.entities:
            continue
        pc = cell_flow_check.precheck_cell(spec, placed, power_situation=power_situation)
        scored.append({"placed": placed, "precheck": pc})
    if not scored:
        return {"ok": False, "spec": spec, "placed": None, "precheck": None,
                "blueprint": None, "reason": "no placeable candidate", "candidates": []}

    scored.sort(key=lambda s: _rank_key(s["precheck"]), reverse=True)
    sandbox_attempts: list[dict[str, Any]] = []
    chosen = scored[0]
    if sandbox is not None:
        chosen, sandbox_attempts = _sandbox_pick(spec, item, rate, scored, sandbox)

    placed = chosen["placed"]
    precheck = chosen["precheck"]
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
        "chosen_archetype": getattr(placed, "archetype", ""),
        "candidates": [
            {"archetype": getattr(s["placed"], "archetype", ""), "status": s["precheck"]["status"],
             "metrics": s["precheck"].get("metrics", {})}
            for s in scored
        ],
        "sandbox_attempts": sandbox_attempts,
        "reason": "; ".join(precheck.get("reasons", [])) or "",
    }


def _sandbox_pick(spec, item, rate, scored, sandbox: SandboxConfirm):
    """Validate the top-K precheck-ranked candidates in the sandbox; pick the one that builds with no
    failures and produces the most of the target item. Falls back to the precheck-best on error."""
    from . import layout_validation  # lazy: RCON-heavy
    attempts: list[dict[str, Any]] = []
    best = scored[0]
    best_score: tuple = (-1, -1.0)
    for s in scored[: max(1, sandbox.top_k)]:
        placed = s["placed"]
        bp = blueprints.encode_blueprint_entities(f"{item}@{rate:g}", placed.entities)
        cid = f"cell:{item}:{rate:g}:{getattr(placed, 'archetype', '')}"
        try:
            res = layout_validation.validate_layout_candidate(
                sandbox.cfg, sandbox.observation or {}, candidate_id=cid,
                candidates=[{"candidate_id": cid, "after_blueprint": {"exchange_string": bp}}],
                ticks=sandbox.ticks, cleanup=sandbox.cleanup,
            )
            sv = res.get("sandbox_validation", {}) if isinstance(res, dict) else {}
        except Exception as exc:  # noqa: BLE001 - sandbox/RCON may be unavailable; keep precheck order
            attempts.append({"archetype": getattr(placed, "archetype", ""), "error": f"{type(exc).__name__}"})
            continue
        out = sv.get("observed_outputs") or {}
        builds = sv.get("status") == "pass" and not (sv.get("build_failures") or [])
        score = (1 if builds else 0, float(out.get(item, 0)))
        attempts.append({"archetype": getattr(placed, "archetype", ""), "status": sv.get("status"),
                         "builds": builds, "output": out.get(item, 0)})
        if score > best_score:
            best_score = score
            best = s
    return best, attempts


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
    archetype = result.get("chosen_archetype") or ""
    status = sandbox_status or f"precheck:{result['precheck']['status']}"
    if archetype:
        status = f"{status}:{archetype}"
    record = cell_library.save_design(
        runtime_dir, result["spec"],
        blueprint_string=result["blueprint"],
        sandbox_status=status,
        placed=result["placed"],
    )
    return {"ok": result["ok"], "record": record, "precheck": result["precheck"],
            "chosen_archetype": archetype, "reason": result.get("reason")}
