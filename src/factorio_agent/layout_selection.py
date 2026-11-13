"""Choose one candidate layout for a concrete site; opt-in exploration; the record a block keeps.

``docs/v2/LAYOUT_PRINCIPLES.md`` ("Candidate layouts"). One scoring function,
:func:`score`, ranks candidates (``layout_variants.candidates``) x orientations:
lower is better, every term in "tile-equivalents" with a named weight. The site
part (``port_cost``) is the ``site.fit.port_score`` sum of the best orientation on
the allocation (``production_site.place_line``) or on a synthetic context
(:func:`probe_context`, used by ``tools/layouts.py`` and the tests), so both
paths use the same terms.

Exploration (:func:`choose`) is off by default. With ``explore`` on (runtime file
``<runtime>/layout-settings.json`` ``{"explore": true}`` or the environment variable
``FACTORIO_LAYOUT_EXPLORE=1`` inherited by a fleet's supervisors), a seed-derived
choice picks among the best ``top_k`` feasible candidates whose score is within
``margin`` of the best. Only audited candidates that got an allocation and an
orientation are ever options, so exploration never picks one that fails the audit
or does not fit.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .layout_variants import compact, request_args

# Score weights (lower total wins). Unit: one tile of port distance.
W_PORT = 1.0  # per tile of port distance / misfacing (site.fit.port_score): routes and taps cost belts and crossings
W_SHORTFALL = 40.0  # per item/min below the requested rate: a block that cannot serve its request now is nearly useless
W_CEILING = 8.0  # per item/min of horizon demand above the in-place growth ceiling: a partial rebuild (dismantle, rebuild, lost output) later
W_PLATE_SCARCE = 0.2  # per raw plate while plates are scarce (early game): a cheaper block is built sooner
W_PLATE = 0.02  # per raw plate later, when plates are plentiful
W_ENTITY = 0.5  # per entity: every placement is a paid actor action plus walking (build time now)
W_AREA = 0.05  # per reserved tile: space left for neighbours
W_POWER = 0.0  # per kW: off by default; a caller with a tight grid may raise it
W_POLLUTION = 0.0  # per pollution/min: off by default (owner: later, with evolution pressure)
W_OVER_BUDGET = 10.0  # per kW above ``Demand.power_budget_kw``: the grid would brown out
W_REBUILD_AHEAD = 1.0  # per tile-equivalent of a rebuild the descriptor already knows is coming (``rebuild_ahead``)
HORIZON_GROWTH = 2.0  # without a demand model, a produce request is expected to double over the horizon
HORIZON_MINUTES = 30.0  # upcoming roadmap units (supply_demand.item_demand) are needed within about this long
EARLY_TICKS = 2 * 60 * 60 * 60  # plates count as scarce during the first two game hours
PROBE_LIMIT = 8  # candidates probed against the site (allocation + orientations) per block
DEFAULT_TOP_K = 3  # exploration pool size
DEFAULT_MARGIN = 0.15  # exploration: within 15% of the best score ...
MARGIN_FLOOR = 10.0  # ... or within 10 tile-equivalents, whichever is larger
SETTINGS_FILE = "layout-settings.json"
ENV_EXPLORE = "FACTORIO_LAYOUT_EXPLORE"


@dataclass(frozen=True)
class Demand:
    """Items/minute now (the request) and over the horizon; plates scarce early."""

    now: float
    horizon: float
    plates_scarce: bool = True
    power_budget_kw: float | None = None  # grid headroom; None = no limit (today)
    w_power: float = W_POWER  # per kW drawn (0 by default: today's choice unchanged)
    w_pollution: float = W_POLLUTION  # per pollution unit/min (0 by default)


def demand_for(rate: float, *, upcoming_units: float | None = None, tick: int | None = None) -> Demand:
    """Demand from the request, the roadmap's upcoming units and the game tick.

    The horizon is at least ``HORIZON_GROWTH`` x the request (throughput growth raises
    produce rates later), more when the next roadmap steps place many units.
    """
    horizon = rate * HORIZON_GROWTH
    if isinstance(upcoming_units, (int, float)) and upcoming_units > 0:
        horizon = max(horizon, rate + float(upcoming_units) / HORIZON_MINUTES)
    scarce = tick is None or tick < EARLY_TICKS
    return Demand(float(rate), float(horizon), scarce)


def score(descriptor: dict, demand: Demand, port_cost: float = 0.0) -> tuple[float, dict]:
    """``(total, terms)`` for one candidate (+ the site's port cost); see the weights above."""
    rate = float(descriptor["rate_now"])
    ceiling = max(rate, float(descriptor["growth"]["max_rate_in_place"]))
    terms = {
        "port": W_PORT * float(port_cost),
        "shortfall": W_SHORTFALL * max(0.0, demand.now - rate),
        "ceiling": W_CEILING * max(0.0, demand.horizon - ceiling),
        "plates": (W_PLATE_SCARCE if demand.plates_scarce else W_PLATE) * float(descriptor["raw_total"]),
        "build": W_ENTITY * float(descriptor["counts"]["entities"]),
        "area": W_AREA * float(descriptor["area"]),
    }
    power = float(descriptor.get("power_kw") or 0.0)
    pollution = float(descriptor.get("pollution_per_min") or 0.0)
    if descriptor.get("rebuild_ahead"):
        # A known later rebuild (a lab part the blue extension cannot use), already in tile-equivalents.
        terms["rebuild_ahead"] = W_REBUILD_AHEAD * float(descriptor["rebuild_ahead"])
    if demand.w_power:
        terms["power"] = demand.w_power * power
    if demand.w_pollution:
        terms["pollution"] = demand.w_pollution * pollution
    if demand.power_budget_kw is not None and power > demand.power_budget_kw:
        terms["over_budget"] = W_OVER_BUDGET * (power - demand.power_budget_kw)
    terms = {k: round(v, 3) for k, v in terms.items()}
    return round(sum(terms.values()), 3), terms


def _evidence_rank(evidence: dict) -> float:
    """Tie-breaker only: share of measured runs whose proof passed (0 without evidence)."""
    runs = (evidence or {}).get("runs") or 0
    return -float((evidence or {}).get("proof_passed") or 0) / runs if runs else 0.0


def ranked(options: list[dict]) -> list[dict]:
    """Feasible options best first: score, then measured evidence, then the id (stable)."""
    return sorted(options, key=lambda o: (o["score"], _evidence_rank(o.get("evidence")), o["id"]))


# ----- exploration -----------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    explore: bool = False
    top_k: int = DEFAULT_TOP_K
    margin: float = DEFAULT_MARGIN


def settings(root: Path | None = None, env: dict | None = None) -> Settings:
    """Runtime file ``<root>/layout-settings.json`` first, then ``FACTORIO_LAYOUT_EXPLORE``; default off."""
    env = os.environ if env is None else env
    data: dict = {}
    if root is not None:
        try:
            data = json.loads((Path(root) / SETTINGS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    explore = data.get("explore")
    if not isinstance(explore, bool):
        explore = str(env.get(ENV_EXPLORE, "")).strip().lower() in ("1", "true", "on", "yes")
    top_k = data.get("top_k", DEFAULT_TOP_K)
    margin = data.get("margin", DEFAULT_MARGIN)
    top_k = top_k if type(top_k) is int and 1 <= top_k <= 8 else DEFAULT_TOP_K
    margin = float(margin) if type(margin) in (int, float) and 0 <= margin <= 1 else DEFAULT_MARGIN
    return Settings(explore, top_k, margin)


def seed_for(*parts: Any) -> int:
    """A stable 64-bit seed from the run identity (world id, block)."""
    text = "|".join(str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def pool(scores: list[float], config: Settings) -> int:
    """How many of the best (sorted) scores are eligible for exploration."""
    if not scores or not config.explore:
        return 1 if scores else 0
    best = scores[0]
    limit = best + max(MARGIN_FLOOR, config.margin * abs(best))
    return max(1, sum(1 for s in scores[: config.top_k] if s <= limit + 1e-9))


def choose(scores: list[float], config: Settings, seed: int) -> tuple[int, int]:
    """``(index, pool size)`` into ascending ``scores``: 0 unless exploring (module doc)."""
    size = pool(scores, config)
    if size <= 1:
        return 0, size
    return seed % size, size


# ----- records ---------------------------------------------------------------------------
def option(candidate: dict, demand: Demand, port_cost: float, **extra) -> dict:
    """One feasible (candidate, site) option with its score."""
    total, terms = score(candidate["descriptor"], demand, port_cost)
    return {
        "id": candidate["descriptor"]["id"],
        "niche": candidate["niche"],
        "source": candidate.get("source", "generated"),
        "score": total,
        "terms": terms,
        "evidence": candidate.get("evidence") or {},
        "candidate": candidate,
        **extra,
    }


def record(options: list[dict], chosen: dict, *, config: Settings, pool_size: int, tick=None, **extra) -> dict:
    """What the block's saved state keeps: the genome, descriptor, scores and the runner-ups."""
    candidate = chosen["candidate"]
    return {
        "variant": chosen["id"],
        "niche": chosen["niche"],
        "source": chosen["source"],
        "genome": candidate["genome"].to_dict(),
        "descriptor": candidate.get("compact") or compact(candidate["descriptor"]),
        "score": chosen["score"],
        "terms": chosen["terms"],
        "explore": asdict(config),
        "explored": bool(config.explore and chosen["id"] != options[0]["id"]),
        "pool": pool_size,
        "runner_ups": [
            {"variant": o["id"], "niche": o["niche"], "score": o["score"]} for o in options if o is not chosen
        ][:6],
        "chosen_tick": tick,
        **extra,
    }


# ----- synthetic sites (tools/layouts.py, tests) ------------------------------------------
@dataclass(frozen=True)
class SiteContext:
    """An allocation ``along`` x ``depth`` tiles with its bus on side ``bus`` and the consumer on ``consumer``."""

    name: str
    along: int
    depth: int
    bus: str = "W"
    consumer: str = "N"


TYPICAL_CONTEXTS = (
    SiteContext("bus W, consumer N", 40, 40, "W", "N"),
    SiteContext("bus W, consumer E (opposite)", 40, 40, "W", "E"),
    SiteContext("bus W, back to the bus", 40, 40, "W", "W"),
    SiteContext("narrow: bus W, 12 deep", 40, 12, "W", "E"),
)


def _site_rects(ctx: SiteContext, size: tuple[int, int]):
    """``(allocation size (w, h), target rect)`` per turned footprint flush to the bus edge."""
    from .site.geometry import Rect

    across = ctx.bus in "WE"
    width, height = (ctx.depth, ctx.along) if across else (ctx.along, ctx.depth)
    for tw, th in dict.fromkeys(((size[0], size[1]), (size[1], size[0]))):
        if tw > width or th > height:
            continue
        x0 = {"W": 0, "E": width - tw}.get(ctx.bus, (width - tw) // 2)
        y0 = {"N": 0, "S": height - th}.get(ctx.bus, (height - th) // 2)
        yield (width, height), Rect(x0, y0, x0 + tw, y0 + th)


def _edge_point(side: str, width: int, height: int) -> tuple[float, float]:
    return {
        "W": (-1.0, height / 2),
        "E": (width + 1.0, height / 2),
        "N": (width / 2, -1.0),
        "S": (width / 2, height + 1.0),
    }[side]


def probe_context(catalog: Any, candidate: dict, request: dict, ctx: SiteContext) -> float | None:
    """Best ``site.fit`` port cost of ``candidate`` on the synthetic site, None when it does not fit."""
    from .production_site import _output_facing, builder_for, footprint_size
    from .site.fit import orientations

    line = candidate["line"]
    size = footprint_size(line)
    design = {
        "reserved_area": line["reserved_area"],
        "arguments": dict(request_args(request), genome=candidate["genome"].to_dict()),
    }
    build = builder_for(catalog, design)
    best = None
    for (width, height), target in _site_rects(ctx, size):
        tap, sink = _edge_point(ctx.bus, width, height), _edge_point(ctx.consumer, width, height)

        def wants(plan, tap=tap, sink=sink):
            rows = [(p, tap, "in", ctx.bus) for p in plan["inputs"]]
            return rows + [(_output_facing(plan), sink, "out", ctx.consumer)]

        found = orientations(build, size, target, wants, allowed=lambda t: t.mirror is None)
        if found and (best is None or found[0].score < best):
            best = found[0].score
    return best


def rank_for_context(catalog: Any, found: list[dict], request: dict, demand: Demand, ctx: SiteContext) -> list[dict]:
    """Feasible options for a synthetic context, best first."""
    options = []
    for candidate in found:
        cost = probe_context(catalog, candidate, request, ctx)
        if cost is not None:
            options.append(option(candidate, demand, cost))
    return ranked(options)


# ----- fleet evidence ---------------------------------------------------------------------
TICKS_PER_SECOND = 60


def run_layouts(root: Path) -> list[dict]:
    """The layout records of one run: ``production-row-*.json`` and ``logistic-science.json``."""
    root = Path(root)
    found = []
    for path in sorted(root.glob("production-row-*.json")) + [root / "logistic-science.json"]:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        layout = state.get("layout") if isinstance(state, dict) else None
        if not isinstance(layout, dict) or not layout.get("variant"):
            continue
        block = "logistic_science" if path.name == "logistic-science.json" else f"produce:{state.get('item')}"
        chosen, verified = layout.get("chosen_tick"), layout.get("verified_tick")
        seconds = None
        if isinstance(chosen, int) and isinstance(verified, int) and verified >= chosen:
            seconds = round((verified - chosen) / TICKS_PER_SECOND)
        descriptor = layout.get("descriptor") or {}
        found.append(
            {
                "block": block,
                "variant": layout["variant"],
                "genome": layout.get("genome"),
                "explored": bool(layout.get("explored")),
                "build_seconds": seconds,
                "proof": state.get("phase") == "verified",
                "power_kw": descriptor.get("power_kw"),
                "pollution_per_min": descriptor.get("pollution_per_min"),
            }
        )
    return found


def variant_summary(rows: list[dict]) -> dict:
    """``(block, variant) -> runs, median build seconds, proofs, explored, power, pollution, genome``."""
    import statistics

    out: dict = {}
    for row in rows:
        entry = out.setdefault(
            (row["block"], row["variant"]),
            {
                "runs": 0,
                "seconds": [],
                "proofs": 0,
                "explored": 0,
                "genome": row.get("genome"),
                "power_kw": row.get("power_kw"),
                "pollution_per_min": row.get("pollution_per_min"),
            },
        )
        entry["runs"] += 1
        entry["proofs"] += int(row["proof"])
        entry["explored"] += int(row["explored"])
        if row.get("build_seconds") is not None:
            entry["seconds"].append(row["build_seconds"])
    for entry in out.values():
        seconds = entry.pop("seconds")
        entry["median_build_seconds"] = round(statistics.median(seconds)) if seconds else None
    return dict(sorted(out.items()))


def format_variants(rows: list[dict]) -> list[str]:
    """The compact ``fleet.py report`` section "layout variants used"."""
    summary = variant_summary(rows)
    lines = ["layout variants used (block, variant, runs, median build game-s, proofs, explored, kW, pollution/min):"]
    for (block, variant), e in summary.items():
        build = "-" if e["median_build_seconds"] is None else str(e["median_build_seconds"])
        power = "-" if e["power_kw"] is None else f"{e['power_kw']:g}"
        pollution = "-" if e["pollution_per_min"] is None else f"{e['pollution_per_min']:g}"
        lines.append(
            f"  {block[:28].ljust(28)} {variant[:30].ljust(30)} {e['runs']:3d} {build:>6} "
            f"{e['proofs']:3d}/{e['runs']:<3d} {e['explored']:3d} {power:>7} {pollution:>6}"
        )
    return lines if summary else lines + ["  none recorded"]
