"""Layout candidate catalogue: show the candidate set of an item, (re)build the library files.

Usage:
    python -B tools/layouts.py show <item> [--rate R] [--catalog PATH] [--no-pictures]
    python -B tools/layouts.py build [<item> ...] [--catalog PATH]
    python -B tools/layouts.py evidence <runtime dir> [...]   (merge fleet results into the library)

``show`` prints every niche winner (``layout_variants.candidates``) as an ASCII
picture (``blocks.render``: recipe letters, ``@`` input port, ``O`` output, ``:``
reserved slot) with its descriptor, then the ``layout_selection.score`` ranking in a
few typical sites (``layout_selection.TYPICAL_CONTEXTS``) at a low and a high demand.
``build`` writes ``data/layouts/<item>.json`` for the typical requests below (evidence
already in a file is kept). The catalog defaults to the newest
``runtime/v2/*/catalog.json`` of this checkout or, in a worktree, of the main checkout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from factorio_agent import layout_selection as selection  # noqa: E402
from factorio_agent import layout_variants as variants  # noqa: E402
from factorio_agent.blocks.render import render_ascii  # noqa: E402
from factorio_agent.core.world_catalog import WorldCatalog  # noqa: E402
from factorio_agent.production_layout import rows  # noqa: E402

PLATES = ("iron-plate", "copper-plate", "steel-plate")
# Typical requests: the rates the roadmap asks for first (items/min).
TYPICAL_RATES = {
    "logistic-science-pack": 5,
    "automation-science-pack": 6,
    "inserter": 30,
    "transport-belt": 30,
    "firearm-magazine": 10,
    "engine-unit": 6,
    "electronic-circuit": 30,
    "iron-gear-wheel": 30,
}


def find_catalog(path: str | None) -> Path:
    if path:
        return Path(path)
    for base in (REPO, *REPO.parents):
        found = sorted((base / "runtime" / "v2").glob("*/catalog.json"), key=lambda p: p.stat().st_mtime)
        if found:
            return found[-1]
    raise SystemExit("no runtime/v2/*/catalog.json found; pass --catalog PATH")


def load(path: Path) -> WorldCatalog:
    return WorldCatalog.from_dict(json.loads(path.read_text(encoding="utf-8")))


PREFERRED = {
    "iron-gear-wheel": "G",
    "copper-cable": "K",
    "electronic-circuit": "C",
    "inserter": "N",
    "transport-belt": "B",
    "logistic-science-pack": "S",
    "automation-science-pack": "R",
    "firearm-magazine": "A",
    "engine-unit": "E",
    "pipe": "P",
}


def letters(catalog, line) -> dict:
    out = {}
    for row in rows(line):
        for e in row["entities"]:
            if e.get("recipe") in PREFERRED:
                out[e["recipe"]] = PREFERRED[e["recipe"]]
    used = set(out.values())
    for row in rows(line):
        for e in row["entities"]:
            recipe = e.get("recipe")
            if not recipe or recipe in out:
                continue
            for ch in recipe.replace("-", "").upper() + "XYZQWJ":
                if ch.isalpha() and ch not in used and ch not in "OIL":
                    out[recipe] = ch
                    used.add(ch)
                    break
    return out


def picture(catalog, line) -> str:
    slots = [s for row in rows(line) for s in row.get("expansion_slots", [])]
    ports = [dict(p, direction="input") for p in line["inputs"]] + [dict(line["output"], direction="output")]
    plan = {"entities": line["entities"], "ports": ports, "expansion_slots": slots}
    return render_ascii(catalog, plan, letters(catalog, line))


def describe_line(d: dict) -> str:
    c, g = d["counts"], d["growth"]
    pollution = "-" if d["pollution_per_min"] is None else f"{d['pollution_per_min']:g}"
    return (
        f"{d['footprint'][0]}x{d['footprint'][1]} tiles, {c['machines']} machines, {c['belts']} belts, "
        f"{c['inserters']} inserters, {c['poles']} poles; raw {d['raw_total']:g} plates; "
        f"rate {d['rate_now']:g}/min (asked {d['rate_requested']:g}); growth {g['kind']} "
        f"(+{g['slots']} slots, max {g['max_rate_in_place']:g}/min, limit {g['limit']}, beyond: {g['beyond']}); "
        f"ports in {','.join(d['input_sides'])} out {d['output_side']} ({d['port_relation']}); "
        f"power {d['power_kw']:g} kW; pollution {pollution}/min"
    )


def show(catalog, item: str, rate: float, *, pictures: bool = True, out=print) -> list[dict]:
    request = variants.library_request(catalog, item, rate, PLATES)
    found = variants.candidates(catalog, request)
    cands = found["candidates"]
    out(
        f"{item} at {rate:g}/min: {len(cands)} candidates (one per niche) from {found['generated']} genomes"
        f"; library {found['library'] or '-'}; skipped {len(found['skipped'])}"
    )
    for c in cands:
        out(f"\n[{c['descriptor']['id']}] niche {c['niche']} ({c['source']})")
        out("  " + describe_line(c["descriptor"]))
        if pictures:
            out("\n".join("    " + line for line in picture(catalog, c["line"]).splitlines()))
    for label, demand in (
        ("low demand (the request, no growth expected)", selection.Demand(rate, rate)),
        ("high demand (4x over the horizon)", selection.Demand(rate, 4 * rate, plates_scarce=False)),
    ):
        out(f"\nselection, {label}:")
        for ctx in selection.TYPICAL_CONTEXTS:
            ranked = selection.rank_for_context(catalog, cands, request, demand, ctx)
            top = ", ".join(f"{o['id']} {o['score']:g}" for o in ranked[:3]) or "nothing fits"
            out(f"  {ctx.name.ljust(30)} {top}")
    return cands


def build(catalog, items: list[str], directory: Path | None = None, out=print) -> None:
    for item in items:
        rate = TYPICAL_RATES.get(item, 10)
        request = variants.library_request(catalog, item, rate, PLATES)
        path = variants.library_path(item, directory)
        previous = variants.load_library(item, directory)
        doc = variants.build_library(catalog, request, previous=previous)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        out(f"{path.relative_to(REPO) if path.is_relative_to(REPO) else path}: {len(doc['entries'])} entries")


def evidence(runs: list[str], directory: Path | None = None, out=print) -> None:
    """Merge fleet runs' layout records (runs, median build seconds, proofs) into library entries."""
    rows_ = [row for run in runs for row in selection.run_layouts(Path(run))]
    summary = selection.variant_summary(rows_)
    by_item: dict = {}
    for (block, _variant), entry in summary.items():
        if block.startswith("produce:"):
            by_item.setdefault(block.split(":", 1)[1], []).append(entry)
    for item, entries in by_item.items():
        doc = variants.load_library(item, directory)
        if doc is None:
            continue
        changed = 0
        for lib in doc["entries"].values():
            for entry in entries:
                if json.dumps(lib["genome"], sort_keys=True) == json.dumps(entry["genome"], sort_keys=True):
                    old = lib.get("evidence") or {}
                    lib["evidence"] = {
                        "runs": int(old.get("runs") or 0) + entry["runs"],
                        "proof_passed": int(old.get("proof_passed") or 0) + entry["proofs"],
                        "median_build_seconds": entry["median_build_seconds"],
                    }
                    changed += 1
        variants.library_path(item, directory).write_text(
            json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        out(f"{item}: evidence merged into {changed} entries")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("show")
    one.add_argument("item")
    one.add_argument("--rate", type=float)
    one.add_argument("--catalog")
    one.add_argument("--no-pictures", action="store_true")
    many = sub.add_parser("build")
    many.add_argument("items", nargs="*")
    many.add_argument("--catalog")
    merge = sub.add_parser("evidence")
    merge.add_argument("runs", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "evidence":
        evidence(args.runs)
        return 0
    catalog = load(find_catalog(args.catalog))
    if args.command == "show":
        show(catalog, args.item, args.rate or TYPICAL_RATES.get(args.item, 10), pictures=not args.no_pictures)
    else:
        build(catalog, args.items or sorted(TYPICAL_RATES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
