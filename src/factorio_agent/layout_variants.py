"""Candidate layouts per production block: genome -> plan, descriptor, niche archive, library.

``docs/v2/LAYOUT_PRINCIPLES.md`` ("Candidate layouts"). A layout is never stored as
entities: a :class:`Genome` holds the few choices the existing generators take as
parameters, and :func:`generate` rebuilds the canonical line from it, pure and
deterministic (``production_layout._design_row_line`` / ``cluster_line``). The
audits are the hard constraints (:func:`audit`): a genome that does not generate or
fails them is dropped, never repaired.

Genome fields (JSON):

* ``form``: ``row`` (``blocks.row``: every intermediate on an internal belt lane, a
  sub-row) or ``cluster`` (``blocks.cluster``: every intermediate by direct insertion);
  the per-edge transport mode follows from the form and is listed in the descriptor;
* ``machine``: the assembler of the block (the main row for a row form; sub-rows keep
  ``size_row``'s choice);
* ``spare`` (row): growth provision, ``none`` | ``default`` (as many slots again) |
  ``max`` (up to ``production_layout.MAX_SPARE_FACTOR`` x the built count, lane budget permitting);
* ``grid`` (cluster): ``(columns, rows)``; ``turned``: the input belt continues down the
  right edge (inputs on two sides); ``cells``: recipe -> ``(c, r)``; ``output_side``:
  the product machine's free side (``left`` = the input edge, ``bottom``, ``right``).

Search (quality-diversity, bounded): blocks here have at most ``cluster.MAX_NODES``
machines, so :func:`genomes` enumerates the whole bounded space (row machines x spare
policies; cluster grids x ``EMBEDDINGS_PER_GRID`` arrangements x free output sides),
at most ``MAX_GENOMES``. :func:`archive` keeps the best genome per *niche* (MAP-Elites
style): ``port relation / aspect / rate class / growth`` (:func:`niche`; the rate class is
relative to the request: short | meets | double); inside a niche
the lower :func:`archive_cost` wins (raw plates, belts, footprint, entities).

Descriptor fields (:func:`describe`): ``id`` (genome key), ``form``, ``machine``,
``footprint`` [w, h] and ``area`` (reserved tiles, canonical frame), ``counts``
(machines, belts, undergrounds, splitters, inserters, poles, chests, entities),
``raw_cost`` (raw items via the catalog's assembler recipes) and ``raw_total``,
``ports`` (item, role, side N/E/S/W of the canonical box, lane), ``input_sides``,
``output_side``, ``port_relation`` (same | opposite | adjacent), ``aspect`` (flat:
long along the input edge | square | tall: deep), ``edges`` (item -> direct | belt |
port), ``rate_now`` (declared items/min), ``growth`` (``kind`` none | in_place |
tileable, ``slots``, ``max_rate_in_place``, ``limit``: what caps it, ``beyond``: what a
larger rate requires), ``power_kw`` and ``pollution_per_min`` (:func:`energy_metrics`, from
the catalog prototypes; module / beacon effects enter only through :func:`effects`).

Reserved genome fields ``modules`` and ``beacons`` are empty today; adding module slots or
beacons later is a genome extension (new values), not a schema change.

Library (``data/layouts/<item>.json``, :func:`build_library`): header = generator
version + the catalog fingerprint the entries were validated against + the request;
entries = niche -> {genome, compact descriptor, scores, evidence}. At runtime every
entry is regenerated and re-audited under the current catalog and request; one that
fails is skipped and reported, never trusted, and niches the library lacks are filled
by generating on the fly (:func:`candidates`). ``evidence`` (runs, median build
seconds, proofs passed) is optional and only a tie-breaker (``layout_selection``).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .blocks.cluster import embeddings, grow_decision, output_sides, tree
from .blocks.cluster_audit import layout_findings
from .blocks.params import inserter_rate
from .production_layout import (
    CLUSTER_MACHINES,
    MAIN_ORIGIN,
    _assembler_recipe,
    _design_row_line,
    _row_recipe,
    audit_line,
    cluster_line,
    is_cluster,
    rows,
)

GENERATOR_VERSION = "layout-variants-v1"
LIBRARY_DIR = Path(__file__).resolve().parents[2] / "data" / "layouts"
ROW_SPARES = ("none", "default", "max")
LIBRARY_MACHINES = ("assembling-machine-1", "assembling-machine-2")  # the offline library's early-game machines
MAX_GENOMES = 48  # generated genomes per request; the space is enumerated, never searched open-ended
FLAT_RATIO = 1.4  # along / depth at or above which a block counts as flat (tall: the inverse)
DOUBLE = 2.0  # a candidate at twice the requested rate is its own rate class
SIDE_OPPOSITE = {"N": "S", "S": "N", "E": "W", "W": "E"}


@dataclass(frozen=True)
class Genome:
    """The choices a block is rebuilt from (module doc); JSON via ``to_dict`` / ``from_dict``."""

    form: str
    machine: str
    spare: str = "default"
    grid: tuple = ()
    turned: bool = False
    cells: tuple = ()  # ((recipe, (c, r)), ...) sorted by recipe
    output_side: str = ""
    # Reserved for modules and beacons (empty today; ``generate`` refuses non-empty ones):
    # modules = ((recipe, (module, ...)), ...), beacons = ((beacon, (module, ...)), ...).
    # ``effects`` is the one place their speed / consumption / pollution effects enter the metrics.
    modules: tuple = ()
    beacons: tuple = ()

    def key(self) -> str:
        short = self.machine.replace("assembling-machine-", "am")
        if self.form == "row":
            return f"row-{short}-{self.spare}"
        order = "".join(f"{c}{r}" for _, (c, r) in self.cells)
        bend = "t" if self.turned else "s"
        return f"cl-{short}-{self.grid[0]}x{self.grid[1]}{bend}-{self.output_side[:1]}-{order}"

    def to_dict(self) -> dict:
        out = {
            "form": self.form,
            "machine": self.machine,
            "modules": {recipe: list(names) for recipe, names in self.modules},
            "beacons": [[name, list(names)] for name, names in self.beacons],
        }
        if self.form == "row":
            out["spare"] = self.spare
        else:
            out.update(
                grid=list(self.grid),
                turned=self.turned,
                cells={recipe: list(cell) for recipe, cell in self.cells},
                output_side=self.output_side,
            )
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Genome":
        extra = dict(
            modules=tuple(sorted((r, tuple(names)) for r, names in (data.get("modules") or {}).items())),
            beacons=tuple((name, tuple(names)) for name, names in (data.get("beacons") or [])),
        )
        if data.get("form") == "row":
            return cls("row", data["machine"], spare=data.get("spare", "default"), **extra)
        cells = tuple(sorted((recipe, tuple(cell)) for recipe, cell in (data.get("cells") or {}).items()))
        return cls(
            "cluster",
            data["machine"],
            grid=tuple(data["grid"]),
            turned=bool(data.get("turned")),
            cells=cells,
            output_side=data.get("output_side", ""),
            **extra,
        )


def generate(
    catalog: Any,
    genome: Genome,
    *,
    product: str,
    rate: float,
    inserter: str,
    enabled: list[str],
    sourced: Callable[[str], bool],
    chest: str,
    origin: tuple[float, float] = MAIN_ORIGIN,
) -> dict:
    """The canonical line of ``genome``; ValueError when it does not generate for this request."""
    if genome.modules or genome.beacons:
        raise ValueError("module slots and beacons are reserved genome fields; placing them is not implemented")
    common = dict(inserter=inserter, enabled=enabled, sourced=sourced, chest=chest, origin=origin)
    if genome.form == "row":
        line = _design_row_line(catalog, product, rate, machine=genome.machine, spare=genome.spare, **common)
    elif genome.form == "cluster":
        line = cluster_line(
            catalog,
            product,
            rate,
            machine=genome.machine,
            grid=genome.grid,
            turned=genome.turned,
            cells=dict(genome.cells),
            output_side=genome.output_side,
            **common,
        )
    else:
        raise ValueError(f"unknown layout form {genome.form}")
    line["genome"] = genome.to_dict()
    return line


def audit(catalog: Any, line: dict) -> list[str]:
    """Every existing audit: the line audit (row / cluster / tail / joint) and the shape findings."""
    findings = list(audit_line(catalog, line))
    if not is_cluster(line):  # audit_cluster already runs layout_findings
        for row in rows(line):
            slots = row["count"] + (row.get("growth") or {}).get("remaining_slots", 0)
            findings += [f"shape:{f}" for f in layout_findings(catalog, row, slots=slots)]
    return sorted(set(findings))


def _row_machines(catalog: Any, recipe: str, enabled: list[str]) -> list[str]:
    categories = set((catalog.recipes.get(recipe) or {}).get("categories") or ())
    found = [
        name
        for name, row in catalog.entities.items()
        if row.get("type") == "assembling-machine"
        and row.get("electric")
        and name in enabled
        and categories & set(row.get("crafting_categories") or ())
    ]
    return sorted(found, key=lambda name: (float(catalog.entities[name].get("crafting_speed") or 0), name))


def genomes(catalog: Any, *, product: str, enabled: list[str], sourced: Callable[[str], bool]) -> list[Genome]:
    """The bounded genome space for one request, deterministic order (module doc)."""
    found: list[Genome] = []
    recipe = _row_recipe(catalog, product)
    if recipe is None:
        return found
    for machine in _row_machines(catalog, recipe, enabled):
        found += [Genome("row", machine, spare=spare) for spare in ROW_SPARES]
    for machine in CLUSTER_MACHINES:
        if machine not in enabled or machine not in catalog.entities:
            continue
        try:
            root, nodes = tree(catalog, product, sourced, machine)
        except ValueError:
            continue
        for e in embeddings(root, nodes):
            cells = tuple(sorted(e.cells))
            for side in output_sides(e, root, nodes):
                found.append(
                    Genome("cluster", machine, grid=(e.columns, e.rows), turned=e.turned, cells=cells, output_side=side)
                )
    unique = list(dict.fromkeys(found))
    return unique[:MAX_GENOMES]


# ----- descriptor ---------------------------------------------------------------------
def raw_cost(catalog: Any, items: dict[str, float], depth: int = 0) -> dict[str, float]:
    """Raw items (no assembler recipe: plates, ores) behind ``items`` through the catalog."""
    out: Counter = Counter()

    def add(item: str, count: float, level: int) -> None:
        recipe = catalog.recipe_for_product(item)
        if level > 6 or not isinstance(recipe, dict) or not _assembler_recipe(catalog, recipe["name"]):
            out[item] += count
            return
        made = sum(float(p["amount"]) for p in recipe["products"] if p.get("name") == item) or 1.0
        for row in recipe["ingredients"]:
            if row.get("type", "item") == "item":
                add(row["name"], count * float(row["amount"]) / made, level + 1)

    for item, count in items.items():
        add(item, float(count), depth)
    return {k: round(v, 2) for k, v in sorted(out.items())}


def _kind(catalog: Any, name: str) -> str:
    return (catalog.entities.get(name) or {}).get("type", "")


FED_FROM = {0: "S", 4: "W", 8: "N", 12: "E"}  # an input belt facing east is fed from the west


def _side(port: dict, area) -> str:
    """An input port's side is where its feeder comes from (its facing); else the nearest edge."""
    if port.get("direction") == "input" and port.get("facing") in FED_FROM:
        return FED_FROM[port["facing"]]
    (x0, y0), (x1, y1) = area
    x, y = port["position"]["x"], port["position"]["y"]
    gaps = {"W": x - x0, "E": x1 - x, "N": y - y0, "S": y1 - y}
    return min(gaps, key=lambda s: (gaps[s], "NESW".index(s)))


def _relation(inputs: list[str], output: str) -> str:
    if output in inputs:
        return "same"
    if SIDE_OPPOSITE[output] in inputs:
        return "opposite"
    return "adjacent"


def rate_class(rate: float, requested: float) -> str:
    """``short`` below the request, ``meets`` up to ``DOUBLE`` x the request, else ``double``."""
    if rate + 1e-9 < requested:
        return "short"
    return "meets" if rate < DOUBLE * requested - 1e-9 else "double"


def _growth(catalog: Any, line: dict, genome: Genome) -> dict:
    main = line["main"]
    tail_rate = inserter_rate(catalog, line["tail"]["arm"]["name"])
    if genome.form == "cluster":
        flips = [e["item"] for e in grow_decision(catalog, main, 2) if e["kind"] == "belt"]
        return {
            "kind": "none",
            "slots": 0,
            "max_rate_in_place": round(float(main["rated_per_minute"]), 3),
            "limit": f"ratio_flip:{flips[0]}" if flips else "one_product_machine",
            "beyond": "new_row_block_beside" if flips else "second_cluster_or_rebuild",
        }
    slots = int(main["growth"]["remaining_slots"])
    per = float(main["rated_per_minute"]) / main["count"]
    ceiling = per * (main["count"] + slots)
    limit = "lane_capacity" if genome.spare == "max" else ("no_reserved_slot" if not slots else "reserved_slots")
    if tail_rate < ceiling:
        ceiling, limit = tail_rate, "output_inserter"
    return {
        "kind": "in_place" if slots else "none",
        "slots": slots,
        "max_rate_in_place": round(ceiling, 3),
        "limit": limit,
        "beyond": "partial_rebuild",
    }


NO_EFFECTS = {"speed": 0.0, "consumption": 0.0, "pollution": 0.0, "productivity": 0.0}


def effects(catalog: Any, genome: Genome) -> dict[str, float]:
    """Module / beacon effects of the genome (the one place they enter the metrics; none today)."""
    _ = (catalog, genome)  # module prototypes will be read from the catalog once modules are placed
    return dict(NO_EFFECTS)


def energy_metrics(catalog: Any, genome: Genome, line: dict) -> dict:
    """``power_kw`` and ``pollution_per_min`` from the catalog prototypes at full activity.

    Power: every electric entity's ``energy_usage`` (J/tick, the prototype's max energy
    usage) x 60 / 1000; machines scaled by ``1 + consumption``. Pollution: the prototype's
    ``emissions_per_minute`` (``{"pollution": x}``) when the catalog exports it, machines
    scaled by ``(1 + consumption) x (1 + pollution)``; None when the catalog has no
    emission data (a ``catalog.json`` exported before ``emissions_per_minute`` existed).
    """
    effect = effects(catalog, genome)
    power = 0.0
    pollution = 0.0
    known = False
    for e in line["entities"]:
        proto = catalog.entities.get(e["name"]) or {}
        machine = proto.get("type") == "assembling-machine"
        scale = 1.0 + effect["consumption"] if machine else 1.0
        if proto.get("electric"):
            power += float(proto.get("energy_usage") or 0) * 60 / 1000 * scale
        emissions = proto.get("emissions_per_minute")
        if isinstance(emissions, dict) and isinstance(emissions.get("pollution"), (int, float)):
            known = True
            pollution += float(emissions["pollution"]) * scale * ((1.0 + effect["pollution"]) if machine else 1.0)
    return {"power_kw": round(power, 1), "pollution_per_min": round(pollution, 2) if known else None}


def describe(catalog: Any, genome: Genome, line: dict) -> dict:
    """The comparable descriptor of one generated candidate (module doc)."""
    names = Counter(e["name"] for e in line["entities"])
    kinds = Counter()
    for name, n in names.items():
        kinds[_kind(catalog, name)] += n
    counts = {
        "machines": kinds["assembling-machine"],
        "belts": kinds["transport-belt"],
        "undergrounds": kinds["underground-belt"],
        "splitters": kinds["splitter"],
        "inserters": kinds["inserter"],
        "poles": kinds["electric-pole"],
        "chests": kinds["container"],
        "entities": sum(names.values()),
    }
    area = line["reserved_area"]
    (x0, y0), (x1, y1) = area
    w, h = int(x1 - x0), int(y1 - y0)
    ports = [
        {"item": p["item"], "role": "in", "side": _side(p, area), **({"lane": p["lane"]} if p.get("lane") else {})}
        for p in line["inputs"]
    ]
    out_side = _side(line["output"], area)
    ports.append({"item": line["output"]["item"], "role": "out", "side": out_side})
    input_sides = sorted({p["side"] for p in ports if p["role"] == "in"}, key="NESW".index)
    primary = Counter(p["side"] for p in ports if p["role"] == "in").most_common(1)[0][0] if input_sides else "W"
    along, depth = (w, h) if primary in "NS" else (h, w)
    aspect = "flat" if along >= FLAT_RATIO * depth else ("tall" if depth >= FLAT_RATIO * along else "square")
    spec = line["spec"]
    if genome.form == "cluster":
        edges = {e["item"]: "direct" for e in line["main"]["edges"]}
    else:
        edges = {item: "belt" for item in (spec.get("sub_item"), spec.get("twin_item")) if item}
    edges.update({p["item"]: "port" for p in line["inputs"]})
    raw = raw_cost(catalog, dict(names))
    return {
        "id": genome.key(),
        "form": genome.form,
        "machine": genome.machine,
        "footprint": [w, h],
        "area": w * h,
        "counts": counts,
        "raw_cost": raw,
        "raw_total": round(sum(raw.values()), 1),
        "ports": ports,
        "input_sides": input_sides,
        "output_side": out_side,
        "port_relation": _relation(input_sides, out_side),
        "aspect": aspect,
        "edges": dict(sorted(edges.items())),
        # What reaches the stock chest: the machines' rate, capped by the tail inserter.
        "rate_now": round(min(float(line["rated_per_minute"]), float(line["stock_rate_per_minute"])), 3),
        "rate_requested": float(spec["rate_per_minute"]),
        **energy_metrics(catalog, genome, line),
        "growth": _growth(catalog, line, genome),
    }


def niche(descriptor: dict) -> str:
    """Archive bin: what matters for placement (module doc)."""
    return "/".join(
        (
            descriptor["port_relation"],
            descriptor["aspect"],
            rate_class(descriptor["rate_now"], descriptor["rate_requested"]),
            descriptor["growth"]["kind"],
        )
    )


def archive_cost(descriptor: dict) -> tuple:
    """Lower wins inside a niche: raw plates, belts, footprint, entities, then the id (stable)."""
    return (
        descriptor["raw_total"],
        descriptor["counts"]["belts"],
        descriptor["area"],
        descriptor["counts"]["entities"],
        descriptor["id"],
    )


def compact(descriptor: dict) -> dict:
    """The few-hundred-byte form stored in a library entry and a block's saved state."""
    keep = (
        "id",
        "form",
        "footprint",
        "port_relation",
        "aspect",
        "input_sides",
        "output_side",
        "rate_now",
        "rate_requested",
        "raw_total",
        "power_kw",
        "pollution_per_min",
    )
    out = {k: descriptor[k] for k in keep}
    out["counts"] = {k: descriptor["counts"][k] for k in ("machines", "belts", "inserters", "poles")}
    out["growth"] = {k: descriptor["growth"][k] for k in ("kind", "slots", "max_rate_in_place", "limit")}
    return out


def evaluate(catalog: Any, genome: Genome, request: dict) -> tuple[dict | None, str | None]:
    """``(candidate, None)`` or ``(None, why)`` for one genome under ``request``."""
    try:
        line = generate(catalog, genome, **request_args(request))
    except (ValueError, KeyError) as exc:
        return None, f"does_not_generate: {str(exc)[:120]}"
    findings = audit(catalog, line)
    if findings:
        return None, "audit: " + ",".join(findings[:3])
    descriptor = describe(catalog, genome, line)
    return {"genome": genome, "line": line, "descriptor": descriptor, "niche": niche(descriptor)}, None


def archive(found: list[dict]) -> dict[str, dict]:
    """Best candidate per niche (``archive_cost``); first come wins exact ties."""
    best: dict[str, dict] = {}
    for candidate in found:
        key = candidate["niche"]
        if key not in best or archive_cost(candidate["descriptor"]) < archive_cost(best[key]["descriptor"]):
            best[key] = candidate
    return dict(sorted(best.items()))


# ----- library -----------------------------------------------------------------------------
def library_path(item: str, directory: Path | None = None) -> Path:
    return (directory or LIBRARY_DIR) / f"{item}.json"


def load_library(item: str, directory: Path | None = None) -> dict | None:
    path = library_path(item, directory)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("entries"), dict) else None


def candidates(catalog: Any, request: dict, *, directory: Path | None = None, library: bool = True) -> dict:
    """``{"candidates": [...], "library": path | None, "skipped": [...], "generated": n}``.

    Library entries first (regenerated and re-audited, a failing one is skipped and
    reported), then on-the-fly genomes for the niches the library lacks.
    """
    found, skipped, source = [], [], None
    data = load_library(request["product"], directory) if library else None
    if data is not None:
        source = str(library_path(request["product"], directory).name)
        if data.get("generator") != GENERATOR_VERSION:
            skipped.append({"library": source, "why": f"generator {data.get('generator')} != {GENERATOR_VERSION}"})
        else:
            for key, entry in sorted(data["entries"].items()):
                try:
                    genome = Genome.from_dict(entry["genome"])
                except (KeyError, TypeError, ValueError):
                    skipped.append({"niche": key, "why": "unreadable genome"})
                    continue
                candidate, why = evaluate(catalog, genome, request)
                if candidate is None:
                    skipped.append({"niche": key, "id": genome.key(), "why": why})
                    continue
                candidate["source"] = "library"
                candidate["evidence"] = entry.get("evidence") or {}
                found.append(candidate)
    covered = {c["niche"] for c in found}
    known = {c["genome"] for c in found}
    fresh = []
    pool = genomes(catalog, product=request["product"], enabled=request["enabled"], sourced=request["sourced"])
    for genome in pool:
        if genome in known:
            continue
        candidate, _why = evaluate(catalog, genome, request)
        if candidate is not None and candidate["niche"] not in covered:
            candidate["source"] = "generated"
            candidate["evidence"] = {}
            fresh.append(candidate)
    best = archive(found)  # a library niche keeps its entry
    for key, candidate in archive(fresh).items():
        best.setdefault(key, candidate)
    return {
        "candidates": list(dict(sorted(best.items())).values()),
        "library": source,
        "skipped": skipped,
        "generated": len(pool),
    }


def build_library(catalog: Any, request: dict, *, previous: dict | None = None) -> dict:
    """A library document for ``request`` (offline ``tools/layouts.py build``); evidence is carried over."""
    found = candidates(catalog, request, library=False)["candidates"]
    old = {}
    for entry in ((previous or {}).get("entries") or {}).values():
        old[json.dumps(entry.get("genome"), sort_keys=True)] = entry.get("evidence") or {}
    entries = {}
    for c in found:
        genome = c["genome"].to_dict()
        entries[c["niche"]] = {
            "genome": genome,
            "descriptor": compact(c["descriptor"]),
            "scores": {"archive_cost": list(archive_cost(c["descriptor"])[:4])},
            "evidence": old.get(json.dumps(genome, sort_keys=True), {}),
        }
    return {
        "format": "factorio-agent-layout-library",
        "generator": GENERATOR_VERSION,
        "catalog_fingerprint": catalog.fingerprint,
        "item": request["product"],
        "request": {
            "rate": request["rate"],
            "inserter": request["inserter"],
            "chest": request["chest"],
            "sourced": sorted(request.get("sourced_items") or ()),
        },
        "entries": entries,
    }


def library_request(
    catalog: Any, item: str, rate: float, sourced_items, *, inserter: str = "inserter", chest: str = "iron-chest"
) -> dict:
    """The typical offline request: every recipe and machine enabled, ``sourced_items`` delivered."""
    items = frozenset(sourced_items)

    def allowed(name: str) -> bool:
        kind = (catalog.entities.get(name) or {}).get("type")
        return kind != "assembling-machine" or name in LIBRARY_MACHINES

    enabled = sorted({n for n in catalog.recipes if allowed(n)} | set(LIBRARY_MACHINES))
    return dict(
        product=item,
        rate=rate,
        inserter=inserter,
        enabled=enabled,
        sourced=items.__contains__,
        chest=chest,
        sourced_items=sorted(items),
    )


def request_args(request: dict) -> dict:
    """The ``generate`` keywords of a request (drops bookkeeping keys)."""
    return {k: request[k] for k in ("product", "rate", "inserter", "enabled", "sourced", "chest") if k in request}
