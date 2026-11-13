"""Smelting bank SHAPE as a layout family around ``smelting.paired_furnace_bank`` (the generator itself unchanged).

``docs/v2/LAYOUT_PRINCIPLES.md`` ("Candidate layouts", "Smelting bank shapes"). The feed head and
the row arrangement inside the generator belong to the district feed-head work; this module only
chooses the generator's parameters and stacks its output, so a new head lands in every candidate.

Reference (owner, a saturated early-game stone furnace setup, principles only): two rows of
furnaces facing each other across ONE shared centre output belt, each row filling its near lane,
inputs on the two outer belts (ore on one lane, coal on the other), the unit stacked side by side
for more. 24 furnaces per row x 18.75 plates/min = 450/min = one lane, so 2 x 24 saturate one
yellow belt. ``paired_furnace_bank`` is exactly that shape (outer input rows, shared centre output).

Genome (:class:`BankGenome`):

* ``slots``: furnace slots per row (the row length frame, reserved; the district builds a prefix).
* ``units``: paired banks stacked side by side across the flow (``2`` = two shorter banks instead of
  one long one; each unit's output is its own plate port).
* ``input_side`` / ``output``: ``outer`` / ``shared_centre`` (the generator's form). ``centre`` input
  with ``outer`` outputs is a genome value the generator has no head for: enumerated, reported as
  not generated, never chosen.
* ``furnace``: ``stone-furnace`` now; ``steel-furnace`` uses the same 2 x 2 slots (twice the rate,
  buildable once researched); ``electric-furnace`` (3 x 3) is enumerated and fails the slot audit
  until the generator has a 3 x 3 slot.

Descriptor: rate now (the start furnaces) and at the full frame, capped per row by its output lane
and per unit by the belt, furnaces / belts / inserters / poles / splitters now and full, raw cost,
kW (electric inserters; furnaces burn coal: ``coal_per_min_full``) and pollution, footprint, ports,
bandwidth of the full frame through the lane flow model (``bandwidth_audit.furnace_bank``:
findings are reported, the shared head's known shortfall included, not hidden). Audit (hard): no
overlapping tiles, every inserter between a belt and a furnace, every inserter inside a pole's
supply area, each unit's poles one chain.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENERATOR_VERSION = "smelting-bank-v1"
LIBRARY_ITEM = "smelting-bank"  # data/layouts/smelting-bank.json
SLOT_CHOICES = (6, 12, 24)
UNIT_CHOICES = (1, 2)
INPUTS = ("outer", "centre")
OUTPUTS = ("shared_centre", "outer")
FURNACES = ("stone-furnace", "steel-furnace", "electric-furnace")
FURNACE_SIZE = {"stone-furnace": 2, "steel-furnace": 2, "electric-furnace": 3}
FURNACE_SPEED = {"stone-furnace": 1.0, "steel-furnace": 2.0, "electric-furnace": 2.0}
FURNACE_WATTS = {"stone-furnace": 90000.0, "steel-furnace": 90000.0, "electric-furnace": 180000.0}
FURNACE_RESEARCH = {
    "steel-furnace": "advanced-material-processing",
    "electric-furnace": "advanced-material-processing-2",
}
PLATE_SECONDS = 3.2
COAL_JOULES = 4.0e6
LANE_PER_MINUTE = 450.0  # yellow belt lane
POLE_SUPPLY = 2.5
POLE_WIRE = 7.5


@dataclass(frozen=True)
class BankGenome:
    slots: int = 12
    units: int = 1
    input_side: str = "outer"
    output: str = "shared_centre"
    furnace: str = "stone-furnace"
    modules: tuple = ()  # reserved, as ``layout_variants.Genome``
    beacons: tuple = ()

    def key(self) -> str:
        form = "" if (self.input_side, self.output) == ("outer", "shared_centre") else f"-{self.input_side}in"
        return f"smelt-{self.slots}x{self.units}{form}-{self.furnace.split('-')[0]}"

    def to_dict(self) -> dict:
        return {
            "form": "smelting_bank",
            "slots": self.slots,
            "units": self.units,
            "input_side": self.input_side,
            "output": self.output,
            "furnace": self.furnace,
            "modules": {},
            "beacons": [],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BankGenome":
        if data.get("form", "smelting_bank") != "smelting_bank":
            raise ValueError("not a smelting bank genome")
        genome = cls(
            int(data["slots"]),
            int(data.get("units", 1)),
            str(data.get("input_side", "outer")),
            str(data.get("output", "shared_centre")),
            str(data.get("furnace", "stone-furnace")),
        )
        if genome.input_side not in INPUTS or genome.output not in OUTPUTS or genome.furnace not in FURNACES:
            raise ValueError("unknown smelting bank genome value")
        if not 1 <= genome.slots <= 32 or not 1 <= genome.units <= 4:
            raise ValueError("smelting bank slots or units out of range")
        return genome


def genomes(*, slots=SLOT_CHOICES, units=UNIT_CHOICES, furnaces=FURNACES) -> list[BankGenome]:
    out = []
    for s in slots:
        for u in units:
            for f in furnaces:
                for i in INPUTS:
                    o = "shared_centre" if i == "outer" else "outer"
                    out.append(BankGenome(int(s), int(u), i, o, f))
    return out


def length_for(slots: int) -> int:
    """The bank length ``paired_furnace_bank`` needs for ``slots`` (its head, slots, port clearance)."""
    from .smelting import SLOT_PITCH

    return 9 + SLOT_PITCH * (slots - 1) + 2 + 4


def generate(genome: BankGenome, resource: str = "iron-ore", per_row: int = 2, *, length: int | None = None) -> dict:
    """The units of ``genome`` stacked across the flow (canonical: ore enters W, plates leave E)."""
    from .smelting import BANK_DEPTH, paired_furnace_bank

    if (genome.input_side, genome.output) != ("outer", "shared_centre"):
        raise ValueError("the bank generator has no centre-input head (outer outputs)")
    length = length or length_for(genome.slots)
    banks = []
    start = max(1, min(-(-per_row // genome.units), genome.slots))  # the start split over the units
    for unit in range(genome.units):
        bank = paired_furnace_bank(
            0, unit * BANK_DEPTH, start, resource, slots=genome.slots, length=length, furnace_name=genome.furnace
        )
        for e in bank["entities"] + bank["growth_entities"]:
            if e["name"] == "stone-furnace" and genome.furnace != "stone-furnace":
                e["name"] = genome.furnace
        bank["furnace_name"] = genome.furnace
        banks.append(bank)
    return {
        "genome": genome,
        "banks": banks,
        "reserved_area": [[0, 0], [length, BANK_DEPTH * genome.units]],
        "start_per_row": start,
    }


def _footprint(e) -> list[tuple[float, float]]:
    size = FURNACE_SIZE.get(e["name"])
    x, y = e["position"]["x"], e["position"]["y"]
    if size:
        half = size / 2
        return [(x - half + 0.5 + i, y - half + 0.5 + j) for i in range(size) for j in range(size)]
    if e["name"] == "splitter":
        w, h = e.get("_width", 2), e.get("_height", 1)
        return [(x - w / 2 + 0.5 + i, y - h / 2 + 0.5 + j) for i in range(w) for j in range(h)]
    return [(x, y)]


def _all(bank) -> list[dict]:
    seen, out = set(), []
    for e in bank["entities"] + bank["growth_entities"]:
        key = (e["name"], e["position"]["x"], e["position"]["y"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def audit(block: dict) -> list[str]:
    """Hard findings of every unit at its full frame (module doc)."""
    findings = []
    owner = {}
    for bank in block["banks"]:
        es = _all(bank)
        for e in es:
            for t in _footprint(e):
                if t in owner:
                    findings.append("tile_overlap")
                owner[t] = e
        belts = {(e["position"]["x"], e["position"]["y"]) for e in es if "belt" in e["name"]}
        furnaces = {t for e in es if e["name"] in FURNACE_SIZE for t in _footprint(e)}
        poles = [e for e in es if e["name"] == "small-electric-pole"]
        for e in es:
            if e["name"] != "inserter":
                continue
            x, y = e["position"]["x"], e["position"]["y"]
            ends = {(x, y - 1), (x, y + 1)} if e.get("direction", 0) in (0, 8) else {(x - 1, y), (x + 1, y)}
            if not (ends & belts and ends & furnaces):
                findings.append("inserter_not_between_belt_and_furnace")
            if not any(
                abs(p["position"]["x"] - x) <= POLE_SUPPLY and abs(p["position"]["y"] - y) <= POLE_SUPPLY for p in poles
            ):
                findings.append("inserter_unpowered")
        if not _chained(poles):
            findings.append("poles_not_one_chain")
    return sorted(set(findings))


def _chained(poles) -> bool:
    if len(poles) <= 1:
        return True
    reached, todo = {0}, [0]
    while todo:
        i = todo.pop()
        for j, p in enumerate(poles):
            a, b = p["position"], poles[i]["position"]
            if j not in reached and math.dist((a["x"], a["y"]), (b["x"], b["y"])) <= POLE_WIRE:
                reached.add(j)
                todo.append(j)
    return len(reached) == len(poles)


def furnace_rate(genome: BankGenome) -> float:
    return FURNACE_SPEED[genome.furnace] * 60 / PLATE_SECONDS


def describe(catalog: Any, block: dict) -> dict:
    from .bandwidth_audit import furnace_bank as flow
    from .layout_variants import Genome, energy_metrics, raw_cost

    genome = block["genome"]
    per = furnace_rate(genome)
    coal = FURNACE_WATTS[genome.furnace] * 60 / COAL_JOULES
    start = block["start_per_row"]
    now = [e for b in block["banks"] for e in b["entities"]]
    full = [e for b in block["banks"] for e in _all(b)]

    def capped(per_row_count, units):
        row = min(LANE_PER_MINUTE, per_row_count * per)  # each row fills its near lane of the centre belt
        return round(2 * row * units, 2)

    def raw(names):
        try:
            return raw_cost(catalog, dict(names))
        except (AttributeError, KeyError, TypeError, ValueError):
            return {k: float(v) for k, v in names.items()}

    def count(es):
        names = Counter(e["name"] for e in es)
        return {
            "furnaces": names[genome.furnace],
            "belts": names["transport-belt"],
            "splitters": names["splitter"],
            "inserters": names["inserter"],
            "poles": names["small-electric-pole"],
            "entities": sum(names.values()),
        }

    findings = Counter()
    for bank in block["banks"]:
        result = flow(
            _all(bank),
            bank["ports"],
            raw=bank["resource"],
            raw_per_furnace=per,
            coal_per_furnace=coal,
            product=bank["product"],
            product_per_furnace=per,
        )
        findings.update(result.get("counts") or {})
    names_now, names_full = Counter(e["name"] for e in now), Counter(e["name"] for e in full)
    raw_now, raw_full = raw(names_now), raw(names_full)
    energy_now = energy_metrics(catalog, Genome("row", "-"), {"entities": now})
    energy_full = energy_metrics(catalog, Genome("row", "-"), {"entities": full})
    (x0, y0), (x1, y1) = block["reserved_area"]
    w, h = int(x1 - x0), int(y1 - y0)
    return {
        "id": genome.key(),
        "form": "smelting_bank",
        "slots": genome.slots,
        "units": genome.units,
        "input_side": genome.input_side,
        "output": genome.output,
        "furnace": genome.furnace,
        "buildable": genome.furnace == "stone-furnace",
        "research": FURNACE_RESEARCH.get(genome.furnace),
        "footprint": [w, h],
        "area": w * h,
        "length": w,
        "depth": h,
        "counts": count(now),
        "counts_full": count(full),
        "raw_cost": raw_full,
        "raw_total": round(sum(raw_now.values()), 1),  # paid now; the slots are a reserved frame
        "raw_full": round(sum(raw_full.values()), 1),
        "rate_now": capped(start, genome.units),
        "rate_full": capped(genome.slots, genome.units),
        "rate_per_furnace": round(per, 3),
        "coal_per_min_full": round(coal * names_full[genome.furnace], 2),
        "power_kw": energy_now["power_kw"],
        "power_kw_full": energy_full["power_kw"],
        "pollution_per_min": energy_now["pollution_per_min"],
        "pollution_per_min_full": energy_full["pollution_per_min"],
        "ports": {"inputs": "W (ore and coal per unit)", "outputs": f"E x {genome.units}"},
        "port_relation": "opposite",
        "bandwidth_findings": dict(sorted(findings.items())),
        "saturates_belt": capped(genome.slots, 1) >= 2 * LANE_PER_MINUTE - 1e-6,
        "growth": {
            "kind": "in_place",
            "slots": genome.slots * 2 * genome.units - names_now[genome.furnace],
            "max_rate_in_place": capped(genome.slots, genome.units),
            "limit": "belt_lanes" if genome.slots * per > LANE_PER_MINUTE else "reserved_slots",
            "beyond": "another_unit_beside",
        },
    }


def compact(descriptor: dict) -> dict:
    keep = (
        "id",
        "form",
        "slots",
        "units",
        "input_side",
        "output",
        "furnace",
        "buildable",
        "footprint",
        "rate_now",
        "rate_full",
        "raw_total",
        "raw_full",
        "power_kw",
        "power_kw_full",
        "pollution_per_min",
        "coal_per_min_full",
        "saturates_belt",
        "bandwidth_findings",
    )
    out = {k: descriptor[k] for k in keep}
    out["counts"] = {k: descriptor["counts"][k] for k in ("furnaces", "belts", "inserters", "poles")}
    out["growth"] = {k: descriptor["growth"][k] for k in ("kind", "slots", "max_rate_in_place", "limit")}
    return out


def niche(descriptor: dict) -> str:
    return "/".join((f"{descriptor['slots']}x{descriptor['units']}", descriptor["input_side"], descriptor["furnace"]))


def evaluate(catalog: Any, genome: BankGenome, request: dict) -> tuple[dict | None, str | None]:
    try:
        block = generate(genome, request.get("resource", "iron-ore"), request.get("start_per_row", 2))
    except (ValueError, KeyError) as exc:
        return None, f"does_not_generate: {str(exc)[:120]}"
    findings = audit(block)
    if findings:
        return None, "audit: " + ",".join(findings[:3])
    descriptor = describe(catalog, block)
    return {
        "genome": genome,
        "line": block,
        "descriptor": descriptor,
        "compact": compact(descriptor),
        "niche": niche(descriptor),
    }, None


def library_path(directory: Path | None = None) -> Path:
    from .layout_variants import library_path as base

    return base(LIBRARY_ITEM, directory)


def load_library(directory: Path | None = None) -> dict | None:
    try:
        data = json.loads(library_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("entries"), dict) else None


def candidates(catalog: Any, request: dict, *, directory: Path | None = None, library: bool = True) -> dict:
    """``{"candidates", "library", "skipped", "generated"}`` like ``layout_variants.candidates``."""
    found, skipped, source = [], [], None
    data = load_library(directory) if library else None
    if data is not None:
        source = library_path(directory).name
        if data.get("generator") != GENERATOR_VERSION:
            skipped.append({"library": source, "why": f"generator {data.get('generator')} != {GENERATOR_VERSION}"})
        else:
            for key, entry in sorted(data["entries"].items()):
                try:
                    genome = BankGenome.from_dict(entry["genome"])
                except (KeyError, TypeError, ValueError):
                    skipped.append({"niche": key, "why": "unreadable genome"})
                    continue
                candidate, why = evaluate(catalog, genome, request)
                if candidate is None:
                    skipped.append({"niche": key, "id": genome.key(), "why": why})
                    continue
                candidate.update(source="library", evidence=entry.get("evidence") or {})
                found.append(candidate)
    known = {c["genome"] for c in found}
    pool = genomes()
    for genome in pool:
        if genome in known:
            continue
        candidate, why = evaluate(catalog, genome, request)
        if candidate is None:
            skipped.append({"id": genome.key(), "why": why})
            continue
        candidate.update(source="generated", evidence={})
        found.append(candidate)
    return {
        "candidates": sorted(found, key=lambda c: c["niche"]),
        "library": source,
        "skipped": skipped,
        "generated": len(pool),
    }


def build_library(catalog: Any, request: dict, *, previous: dict | None = None) -> dict:
    old = {
        json.dumps(e.get("genome"), sort_keys=True): e.get("evidence") or {}
        for e in ((previous or {}).get("entries") or {}).values()
    }
    entries = {}
    for c in candidates(catalog, request, library=False)["candidates"]:
        genome = c["genome"].to_dict()
        entries[c["niche"]] = {
            "genome": genome,
            "descriptor": c["compact"],
            "scores": {
                "raw_full": c["descriptor"]["raw_full"],
                "entities_full": c["descriptor"]["counts_full"]["entities"],
            },
            "evidence": old.get(json.dumps(genome, sort_keys=True), {}),
        }
    return {
        "format": "factorio-agent-layout-library",
        "generator": GENERATOR_VERSION,
        "catalog_fingerprint": catalog.fingerprint,
        "item": LIBRARY_ITEM,
        "request": dict(request),
        "entries": entries,
    }


# ----- selection --------------------------------------------------------------------------------
@dataclass(frozen=True)
class BankContext:
    """Room for one resource's bank: ``length`` along the plate flow, ``depth`` across it."""

    name: str
    length: int
    depth: int
    researched: tuple = ()


def contexts() -> tuple:
    """Typical sites: today's district (48 x 32 for two resources: 16 rows each), a long zone, a deep one."""
    return (
        BankContext("district 48 long, 16 deep per resource", 48, 16),
        BankContext("long zone 96 x 16", 96, 16),
        BankContext("deep zone 48 x 32, steel researched", 48, 32, ("advanced-material-processing",)),
    )


def rank(found: list[dict], demand, ctx: BankContext) -> list[dict]:
    """Feasible options for ``ctx`` best first: the units fit the room and the furnace is researched."""
    from . import layout_selection as selection

    options = []
    for c in found:
        d = c["descriptor"]
        if d["length"] > ctx.length or d["depth"] > ctx.depth:
            continue
        if d["research"] and d["research"] not in ctx.researched:
            continue
        options.append(selection.option(c, demand, 0.0))
    return selection.ranked(options)


def record_for(bank: dict) -> dict:
    """The ``layout`` record of a district bank planned by ``smelting.paired_furnace_bank`` (built variant)."""
    furnace = bank.get("furnace_name") or "stone-furnace"
    genome = BankGenome(int(bank.get("capacity_slots") or 12), 1, "outer", "shared_centre", furnace)
    per = FURNACE_SPEED.get(furnace, 1.0) * 60 / PLATE_SECONDS
    built = int(bank.get("per_side_count") or 1)
    return {
        "variant": genome.key(),
        "niche": "built",
        "source": "built",
        "genome": genome.to_dict(),
        "descriptor": {
            "slots": genome.slots,
            "furnace": furnace,
            "rate_now": round(2 * min(LANE_PER_MINUTE, built * per), 2),
            "power_kw": None,
            "pollution_per_min": None,
        },
        "explored": False,
        "chosen_tick": None,
        "verified_tick": None,
    }
