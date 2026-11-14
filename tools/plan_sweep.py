"""Offline seed sweep of the pure planners on synthetic worlds (no game). Read-only.

Usage: python -B tools/plan_sweep.py [--seeds 0-299] [--workers N] [--json] [--upto GROUP] [--catalog PATH]
       python -B tools/plan_sweep.py --seed S --render [--out DIR]

For every ``site.fakeworld`` seed the planning pipeline runs as far as it goes without a game
(``factorio_agent.plan_sweep``: master plan, mining banks, smelting district, ore feeds, planned
main bus, red and green science on bus taps, allocation of the later blocks) and one compact
report is printed: failure rate per stage, top reasons with count and an example seed,
tidiness medians, the queries the offline game could not answer and the stages that need the
game. ``--json`` prints the summary and every row instead. ``--seed S --render`` plans one seed
and writes its master plan (``site.planrender``) with every planned entity as an SVG plus the
row as JSON into ``--out`` (default: the system temp folder).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from factorio_agent import plan_sweep as ps  # noqa: E402


MAX_WORKERS = 8  # live fleets and the GUI run share this machine: never take more than a quarter of it


def default_workers() -> int:
    return max(1, min(MAX_WORKERS, (os.cpu_count() or 1) // 4))


def seed_list(text: str) -> list[int]:
    seeds: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            a, b = part.split("-", 1)
            seeds.extend(range(int(a), int(b) + 1))
        else:
            seeds.append(int(part))
    return seeds


def render(seed: int, out: Path, catalog) -> int:
    from factorio_agent.site.planrender import render_plan_svg

    out.mkdir(parents=True, exist_ok=True)
    keep = out / f"plan-sweep-{seed}"
    row = ps.run_seed(seed, catalog, keep=keep)
    sweep = row.pop("_sweep")
    svg = out / f"plan-sweep-{seed}.svg"
    world = sweep.world
    if getattr(sweep, "plan", None) is not None:
        try:
            world.clear_plans()
            world.apply_plan([e for e in sweep.entities if "position" in e], owner="sweep")
        except (KeyError, TypeError, ValueError):
            pass
        svg.write_text(render_plan_svg(world, sweep.plan), encoding="utf-8")
        print(sweep.plan.summary())
    (out / f"plan-sweep-{seed}.json").write_text(json.dumps(row, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in row["stages"].items()}, indent=1))
    print(f"wrote {svg} and {out / f'plan-sweep-{seed}.json'} (runtime files in {keep})")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", default="0-299", help="seed list, e.g. 0-299 or 1,5,9-12")
    parser.add_argument("--seed", type=int, help="one seed (with --render)")
    parser.add_argument("--render", action="store_true", help="write one seed's plan as SVG + JSON")
    parser.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument(
        "--workers",
        type=int,
        default=default_workers(),
        help="worker processes (default min(8, cpus // 4): live games and fleets share the machine)",
    )
    parser.add_argument("--catalog", type=Path, default=None, help="catalog.json(.gz); default: the test fixture")
    parser.add_argument("--json", action="store_true", help="summary and every row as JSON")
    parser.add_argument("--upto", choices=ps.GROUPS, help="stop after this stage group (faster partial sweeps)")
    parser.add_argument("--route-budget", type=int, default=ps.ROUTE_BUDGET, help="router skeleton checks per seed")
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args(argv)
    if args.render:
        if args.seed is None:
            parser.error("--render needs --seed S")
        return render(args.seed, args.out, args.catalog)
    if args.workers > MAX_WORKERS:
        parser.error(f"--workers is capped at {MAX_WORKERS} (live games share the machine)")
    seeds = [args.seed] if args.seed is not None else seed_list(args.seeds)
    started = time.perf_counter()
    rows = ps.sweep(seeds, args.catalog, workers=args.workers, route_budget=args.route_budget, upto=args.upto)
    wall = time.perf_counter() - started
    summary = ps.aggregate(rows)
    if args.json:
        print(json.dumps({"summary": summary, "rows": rows, "wall_seconds": round(wall, 1)}, indent=1))
    else:
        print(ps.report(summary, top=args.top, wall=wall))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
