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
from .cell_placer import PlacedCell, _entity_tiles

# Footprint-aware geometry helpers — these model REAL entity tiles (so a 2x2 furnace is handled
# correctly), unlike planner._point_inside_machine_footprint which hardcodes a +/-1.5 box and so
# silently accepts an inserter that doesn't actually touch a 2x2 furnace.
_BELT_NAMES = {"transport-belt", "fast-transport-belt", "express-transport-belt"}
_CHEST_NAMES = {"wooden-chest", "iron-chest", "steel-chest"}
_INSERTER_NAMES = {
    "inserter", "long-handed-inserter", "fast-inserter", "stack-inserter", "bulk-inserter", "burner-inserter",
}
_ASSEMBLER_NAMES = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}
_DIR_VEC = {0: (0.0, -1.0), 4: (1.0, 0.0), 8: (0.0, 1.0), 12: (-1.0, 0.0)}


def _tile(x: float, y: float) -> tuple[int, int]:
    return (int(round(x)), int(round(y)))


def _is_machine_entity(e: dict[str, Any]) -> bool:
    n = str(e.get("name") or "")
    return n in _ASSEMBLER_NAMES or "furnace" in n


def _inserter_endpoints(e: dict[str, Any]) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """(pickup_tile, drop_tile) for an inserter — same model as planner._inserter_endpoints."""
    pos = e.get("position") if isinstance(e.get("position"), dict) else None
    vec = _DIR_VEC.get(int(e.get("direction") or 0))
    if pos is None or vec is None:
        return None
    reach = 2.0 if str(e.get("name") or "") == "long-handed-inserter" else 1.0
    x = float(pos.get("x") or 0.0)
    y = float(pos.get("y") or 0.0)
    pickup = _tile(x + vec[0] * reach, y + vec[1] * reach)
    drop = _tile(x - vec[0] * reach, y - vec[1] * reach)
    return pickup, drop


def _machine_tiles(e: dict[str, Any]) -> set[tuple[int, int]]:
    pos = e.get("position") if isinstance(e.get("position"), dict) else {}
    return _entity_tiles(str(e.get("name") or ""), pos.get("x", 0), pos.get("y", 0))


def _inserter_reach_errors(entities: list[dict[str, Any]]) -> list[str]:
    """Every machine (assembler or furnace) must have >=1 input inserter whose DROP lands on its
    real tiles (picking from a belt/chest/producer) and >=1 output inserter whose PICKUP is on its
    real tiles (dropping to a belt/chest). Catches the 2x2-furnace inserter miss."""
    belt_tiles = {_tile(e["position"]["x"], e["position"]["y"]) for e in entities
                  if str(e.get("name") or "") in _BELT_NAMES and isinstance(e.get("position"), dict)}
    chest_tiles = {_tile(e["position"]["x"], e["position"]["y"]) for e in entities
                   if str(e.get("name") or "") in _CHEST_NAMES and isinstance(e.get("position"), dict)}
    machines = [(e, _machine_tiles(e)) for e in entities if _is_machine_entity(e)]
    inserters = [e for e in entities if str(e.get("name") or "") in _INSERTER_NAMES]
    errors: list[str] = []
    for e, tiles in machines:
        inbound = outbound = 0
        for ins in inserters:
            ep = _inserter_endpoints(ins)
            if ep is None:
                continue
            pickup, drop = ep
            source_at = lambda t: t in belt_tiles or t in chest_tiles or any(t in t2 for o, t2 in machines if o is not e)
            sink_at = lambda t: t in belt_tiles or t in chest_tiles or any(t in t2 for o, t2 in machines if o is not e)
            if drop in tiles and source_at(pickup):
                inbound += 1
            if pickup in tiles and sink_at(drop):
                outbound += 1
        name = str(e.get("name") or "")
        pos = e.get("position") or {}
        where = f"({pos.get('x')},{pos.get('y')})"
        if inbound < 1:
            errors.append(f"{name} at {where} has no input inserter reaching it")
        if outbound < 1:
            errors.append(f"{name} at {where} has no output inserter reaching it")
    return errors


def _fuel_supply_errors(spec: CellSpec, placed: PlacedCell) -> list[str]:
    """A burner furnace needs coal: if the cell consumes coal, a coal source/lane must be present."""
    if not any(getattr(i, "item", None) == "coal" for i in spec.inputs):
        return []
    source_items = {str(s.get("item")) for s in (placed.sources or []) if isinstance(s, dict)}
    if "coal" not in source_items:
        return ["burner machine needs coal but no coal source/lane is present in the layout"]
    return []


def _flow_reachability_errors(spec: CellSpec, entities: list[dict[str, Any]]) -> list[str]:
    """Each co-located intermediate must actually reach its consumer: either direct insertion
    (an inserter picks inside a producer and drops inside a consumer) or a forward belt path from a
    producer-output belt tile to a consumer-input belt tile (following belt flow direction). Catches
    the 'intermediate belt flows away from its consumer' bug."""
    inter_items = {s.item for s in spec.substages}
    if not inter_items:
        return []
    inserters = [e for e in entities if str(e.get("name") or "") in _INSERTER_NAMES]
    machine_ents = [e for e in entities if _is_machine_entity(e)]

    def produces(e: dict[str, Any], item: str) -> bool:
        r = knowledge.recipe_for_product(item)
        return bool(r) and str(e.get("recipe") or "") == (r.name if r else "")

    def consumes(e: dict[str, Any], item: str) -> bool:
        rname = str(e.get("recipe") or "")
        rec = knowledge.RECIPES.get(rname) if hasattr(knowledge, "RECIPES") else None
        rec = rec or (knowledge.recipe_for_product(spec.target_item) if rname else None)
        return bool(rec) and item in (rec.ingredients or {})

    # belt flow map: tile -> next tile (one step along its direction)
    belt_next: dict[tuple[int, int], tuple[int, int]] = {}
    belt_tiles: set[tuple[int, int]] = set()
    for e in entities:
        if str(e.get("name") or "") in _BELT_NAMES and isinstance(e.get("position"), dict):
            t = _tile(e["position"]["x"], e["position"]["y"])
            belt_tiles.add(t)
            vec = _DIR_VEC.get(int(e.get("direction") or 0))
            if vec is not None:
                belt_next[t] = (t[0] + int(vec[0]), t[1] + int(vec[1]))

    errors: list[str] = []
    for item in inter_items:
        producers = [(e, _machine_tiles(e)) for e in machine_ents if produces(e, item)]
        consumers = [(e, _machine_tiles(e)) for e in machine_ents if consumes(e, item)]
        if not producers or not consumers:
            continue
        cons_tiles = set().union(*[t for _, t in consumers])
        cons_in_belt = {
            ep[0] for ins in inserters if (ep := _inserter_endpoints(ins))
            and ep[1] in cons_tiles and ep[0] in belt_tiles
        }
        # EVERY producer must reach a consumer (else the far machine's output is wasted — the user's
        # 'rightmost cable assembler can't feed the EC' bug).
        for pe, ptiles in producers:
            # direct insertion: an inserter picks inside this producer and drops inside a consumer
            served = any(
                (ep := _inserter_endpoints(ins)) and ep[0] in ptiles and ep[1] in cons_tiles
                for ins in inserters
            )
            if served:
                continue
            # belt path: this producer's output-belt drops -> walk flow to a consumer-input belt tile
            prod_out = {
                ep[1] for ins in inserters if (ep := _inserter_endpoints(ins))
                and ep[0] in ptiles and ep[1] in belt_tiles
            }
            for start in prod_out:
                cur = start
                seen: set[tuple[int, int]] = set()
                for _ in range(400):
                    if cur in cons_in_belt:
                        served = True
                        break
                    if cur in seen or cur not in belt_tiles:
                        break
                    seen.add(cur)
                    nxt = belt_next.get(cur)
                    if nxt is None:
                        break
                    cur = nxt
                if served:
                    break
            if not served:
                pos = pe.get("position") or {}
                errors.append(
                    f"intermediate '{item}': producer at ({pos.get('x')},{pos.get('y')}) cannot reach "
                    f"a consumer (belt flows away / no direct insertion)"
                )
                break  # one error per item is enough
    return errors


def _collision_errors(entities: list[dict[str, Any]]) -> list[str]:
    occupied: dict[tuple[int, int], str] = {}
    clashes: list[str] = []
    for e in entities:
        pos = e.get("position")
        if not isinstance(pos, dict):
            continue
        for t in _entity_tiles(str(e.get("name") or ""), pos.get("x", 0), pos.get("y", 0)):
            if t in occupied and occupied[t] != "_":
                clashes.append(f"overlap at {t}: {occupied[t]} + {e.get('name')}")
            occupied[t] = str(e.get("name") or "")
    return clashes[:6]


def _layout_metrics(placed: PlacedCell) -> dict[str, Any]:
    ents = placed.entities or []
    occupied: set[tuple[int, int]] = set()
    for e in ents:
        pos = e.get("position")
        if isinstance(pos, dict):
            occupied |= _entity_tiles(str(e.get("name") or ""), pos.get("x", 0), pos.get("y", 0))
    rb = placed.required_box or {}
    w = float(rb.get("width") or 0.0)
    h = float(rb.get("height") or 0.0)
    area = w * h
    inserter_count = sum(1 for e in ents if str(e.get("name") or "") in _INSERTER_NAMES)
    return {
        "entity_count": len(ents),
        "inserter_count": inserter_count,
        "occupied_tiles": len(occupied),
        "rect_fill": round(len(occupied) / area, 3) if area > 0 else 0.0,
        "archetype": getattr(placed, "archetype", "") or "",
    }


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

    # 3b. footprint-aware geometry (catches what the +/-1.5 operability box misses):
    #     inserter reach against REAL tiles (2x2 furnaces), burner fuel supply, intermediate flow
    #     reachability, and entity collisions.
    reach_errors = _inserter_reach_errors(placed.entities)
    checks["inserter_reach"] = "fail" if reach_errors else "pass"
    reasons.extend(reach_errors[:4])

    fuel_errors = _fuel_supply_errors(spec, placed)
    checks["fuel_supply"] = "fail" if fuel_errors else "pass"
    reasons.extend(fuel_errors)

    flow_errors = _flow_reachability_errors(spec, placed.entities)
    checks["flow_reachability"] = "fail" if flow_errors else "pass"
    reasons.extend(flow_errors[:4])

    collision_errors = _collision_errors(placed.entities)
    checks["collision"] = "fail" if collision_errors else "pass"
    reasons.extend(collision_errors[:4])

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
    return {"status": status, "reasons": reasons, "warnings": warns, "checks": checks,
            "metrics": _layout_metrics(placed)}
