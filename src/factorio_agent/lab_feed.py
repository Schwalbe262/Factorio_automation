"""Lab feed audit: does every built lab get every pack, one pack per lane (``belt_flow``; pure).

The lab row's belt carries red on one lane and each other pack on the other (green
side-loads onto the free lane at the head, ``manufacturing.lab_green_entry``). The audit
pushes every pack block's built pack machines (inserters dropping packs on their output
belts) through the plan of the red block (built stage), its lab row and the pack blocks'
plans (routes and the lab entry), with every built lab's inserter a consumer of every pack
at ``per_lab`` packs/minute (``LAB_UNIT_SECONDS`` reference unit). Causes per pack:

* ``source_short`` - the line delivers everything the pack machines make and that is below
  what the labs draw: the pack block is the limit (grow it: ``pack_growth``), not the feed;
* ``lane_starvation`` - the pack never reaches a lab: a broken lane, inserter chain or join;
* ``shared_lane`` - two packs on one lane at a lab: the one backing up blocks the other (a
  lane merge or a pack on its own lane first; ``rework`` names it);
* ``capacity_shortfall`` / ``single_lane_feed`` - belt or lane capacity, not the producers.

eval-1575138435 (~185 min, 8 labs, research engine): red 4 machines = 24/min reach every
lab on the right lane, green 1 machine = 5/min on the left lane where 8 labs draw 16/min:
``source_short`` at the first lab of the row, the labs further down starve for green while
red backs up. Nothing in the feed is wrong; the green block is.
"""

from __future__ import annotations

from collections.abc import Mapping

from .belt_flow import LANES

# Reference lab research unit (s): logistics-era two-pack technologies take 15-30 s per unit;
# the slowest common unit gives the declared draw a lab must at least get of every pack.
LAB_UNIT_SECONDS = 30.0
# Packs/minute of one assembling-machine-1 when no catalog is given (60 x 0.5 x yield / energy).
FALLBACK_RATES = {
    "automation-science-pack": 6.0,
    "logistic-science-pack": 5.0,
    "chemical-science-pack": 2.5,
}
STRUCTURAL = ("lane_starvation", "shared_lane", "capacity_shortfall", "single_lane_feed")


def _key(e):
    return (e["name"], e["position"]["x"], e["position"]["y"])


def pack_rates(catalog=None, packs=()) -> dict:
    """Packs/minute of one pack machine per pack: the catalog's recipe, else ``FALLBACK_RATES``."""
    out = dict(FALLBACK_RATES)
    if catalog is not None:
        from .research_capacity import pack_rate_per_assembler

        for pack in packs:
            try:
                out[pack] = pack_rate_per_assembler(catalog, pack)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    return out


def audit(science_state, pack_states=(), *, catalog=None, per_lab=None) -> dict | None:
    """Flow findings, per-pack causes and lane use at every built lab (module doc); None without a lab row."""
    from .bandwidth_audit import audit_plan, inserter_rows
    from .science_growth import build_plan, built_labs

    if not isinstance(science_state, Mapping) or not isinstance(science_state.get("labs"), Mapping):
        return None
    entities, seen = [], set()
    plans = [build_plan(science_state)] + [s["plan"] for s in pack_states if isinstance(s.get("plan"), Mapping)]
    for plan in plans:
        for e in plan.get("entities") or ():
            if isinstance(e, Mapping) and isinstance(e.get("position"), Mapping) and _key(e) not in seen:
                seen.add(_key(e))
                entities.append(e)
    packs = sorted({e["recipe"] for e in entities if str(e.get("recipe", "")).endswith("science-pack")})
    if not packs:
        return None
    rates = pack_rates(catalog, packs)
    per_lab = float(per_lab if per_lab is not None else 60.0 / LAB_UNIT_SECONDS)
    labs = {_key(e) for e in built_labs(science_state)}
    lab_tiles = [
        pickup
        for _e, pickup, _drop, at_drop, _at_pickup in inserter_rows(entities)
        if at_drop is not None and at_drop.get("name") == "lab" and _key(at_drop) in labs
    ]

    def demand(_inserter, machine):
        if machine is not None and machine.get("name") == "lab" and _key(machine) in labs:
            return {pack: per_lab for pack in packs}
        return None

    def made(_inserter, machine):
        recipe = (machine or {}).get("recipe")
        return (recipe, rates.get(recipe, 0.0)) if recipe in packs else None

    result = audit_plan(entities, [], demand_for=demand, product_for=made, probe=lab_tiles)
    findings = list(result["findings"])
    for row in result.get("lanes") or ():
        for lane in LANES:
            carried = sorted(p for p in row[lane] if p in packs and row[lane][p] > 1e-9)
            if len(carried) > 1:
                findings.append({"kind": "shared_lane", "tile": row["tile"], "lane": lane, "items": carried})
    made_by = {p: sum(rates.get(p, 0.0) for e in entities if e.get("recipe") == p) for p in packs}
    summary = {}
    for pack in packs:
        kinds = sorted({f["kind"] for f in findings if f.get("item") == pack or pack in (f.get("items") or ())})
        cause = next((k for k in ("lane_starvation", "shared_lane") if k in kinds), None) or (
            kinds[0] if kinds else "ok"
        )
        summary[pack] = {
            "made_per_minute": round(made_by[pack], 2),
            "lab_demand_per_minute": round(per_lab * len(labs), 2),
            "cause": cause,
        }
    counts = {}
    for f in findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return {
        "findings": findings,
        "counts": counts,
        "ports": result.get("ports") or [],
        "packs": summary,
        "labs": len(labs),
        "lab_unit_seconds": LAB_UNIT_SECONDS,
    }


def rework(row) -> dict | None:
    """A lab feed rework proposal for a structural finding (never for ``source_short``: grow the pack block)."""
    bad = [f for f in row.get("findings") or () if f.get("kind") in STRUCTURAL]
    if not bad:
        return None
    return {
        "block": row.get("block", "laboratories"),
        "status": "proposed",
        "action": "rework_lab_feed",
        "cause": bad[0]["kind"],
        "findings": [{k: f.get(k) for k in ("kind", "tile", "item", "items", "lane")} for f in bad[:4]],
        "rule": "plan republished first; the new join or lane merge built beside the running feed, the old "
        "part mined only after every lab gets every pack from the new one",
        "next": "not built by any capability yet",
    }
