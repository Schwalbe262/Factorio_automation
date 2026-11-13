"""What a verified rocket launch still needs on a run, from its catalog. Read-only.

Usage: python -B tools/rocket_plan.py runtime/v2/<name> [--minutes 60] [--rockets 1] [--all-research] [--json]

Reads ``<run>/catalog.json`` and its ``site-plan.json`` bus lanes. Researched
technologies are the catalog snapshot's own flags (a snapshot from map start
lists the whole closure; ``--all-research`` forces that). Prints the
remaining technologies with packs, the science pack totals, the silo, part and
payload recipes, every item of the closure with amounts (silo / per rocket /
total), rate over the window, machines, belts and its producer (capability and
status: roadmap, wired, unwired, missing), the shared bus items, the gaps, and
the generated roadmap tail (``factorio_agent.rocket_plan``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from factorio_agent.core.world_catalog import WorldCatalog  # noqa: E402
from factorio_agent.progression import ROADMAP  # noqa: E402
from factorio_agent.rocket_plan import DEFAULT_MINUTES, roadmap_research, rocket_bill, tail  # noqa: E402


def _plan_lanes(run: Path):
    """Bus lanes the run's site plan reserves (``site-plan.json`` config), or None for the default."""
    path = run / "site-plan.json"
    if not path.exists():
        return None
    try:
        config = (json.loads(path.read_text(encoding="utf-8")).get("plan") or {}).get("config") or {}
    except (OSError, ValueError):
        return None
    lanes = config.get("lane_order")
    return lanes if isinstance(lanes, list) else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--minutes", type=float, default=DEFAULT_MINUTES)
    parser.add_argument("--rockets", type=int, default=1)
    parser.add_argument("--all-research", action="store_true", help="count the whole technology closure")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    catalog = WorldCatalog(json.loads((args.runtime / "catalog.json").read_text(encoding="utf-8")))
    bill = rocket_bill(
        catalog,
        researched=() if args.all_research else None,
        minutes=args.minutes,
        rockets=args.rockets,
        bus_lanes=_plan_lanes(args.runtime),
    )
    rows = tail(bill, existing=[step.id for step in ROADMAP], researched_by=roadmap_research())
    if args.json:
        print(json.dumps({"bill": bill, "tail": rows}, indent=1, sort_keys=True))
        return 0
    print(f"catalog {catalog.version}, mods {', '.join(sorted(catalog.active_mods))}")
    print(
        f"target technology {bill['target_technology']}; silo {bill['silo_entity']} needs {bill['parts_per_rocket']} parts"
    )
    print(f"part recipe {bill['part_recipe']['name']}: {bill['part_recipe']['ingredients']}")
    print(f"silo recipe {bill['silo_recipe']}")
    print(f"payload {bill['payload']}: {bill['payload_recipe']}")
    print(f"\nremaining technologies ({len(bill['technologies'])}), research order:")
    for tech in bill["technologies"]:
        packs = ", ".join(f"{k.replace('-science-pack', '')} {v:g}" for k, v in tech["packs"].items())
        trigger = f" trigger {tech['trigger']}" if tech["trigger"] else ""
        print(f"  {tech['name']:<34} {packs}{trigger}")
    print("science packs: " + ", ".join(f"{k} {v:g}" for k, v in bill["science_packs"].items()))
    print(f"\nitems over {bill['minutes']:g} min for {bill['rockets']} rocket(s) (ingredients first):")
    print(f"  {'item':<30} {'silo':>8} {'rocket':>8} {'total':>9} {'/min':>8} {'mach':>6} belts  producer")
    for row in bill["items"]:
        producer = row["producer"]
        params = f" {producer['parameters']}" if producer["parameters"] else ""
        machines = f"{row['machines']:g}" if "machines" in row else "-"
        name = row["item"] if row["type"] == "item" else f"{row['item']} (fluid)"
        print(
            f"  {name:<30} {row['silo']:>8g} {row['per_rocket']:>8g} {row['total']:>9g} {row['rate_per_minute']:>8g}"
            f" {machines:>6} {row.get('belts', '-'):>5}  {producer['capability']}{params} [{producer['status']}]"
        )
    print("raw: " + ", ".join(f"{k} {v:g}" for k, v in bill["raw"].items()))
    if bill["surplus"]:
        print("surplus: " + ", ".join(f"{k} {v:g}" for k, v in bill["surplus"].items()))
    print("\nshared belt items (bus candidates):")
    for row in bill["bus"]:
        print(
            f"  {row['item']:<24} {row['rate_per_minute']:>8g}/min  belts {row['belts']}  reserved lanes {row['reserved']}"
        )
    print("\ngaps (capability: status: items):")
    for gap in bill["gaps"]:
        print(f"  {gap['capability']}: {gap['status']}: {', '.join(gap['items'])}")
    print(f"\ngenerated roadmap tail ({len(rows)} steps after {ROADMAP[-1].id}):")
    for row in rows:
        print(f"  {row['id']:<46} {row['capability']:<20} {row['parameters']} after {row['after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
