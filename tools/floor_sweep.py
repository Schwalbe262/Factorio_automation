"""Offline before/after measure of district slot choice on fake worlds. Read-only.

Usage: python -B tools/floor_sweep.py [--catalog runtime/v2/<name>/catalog.json] [--seeds 20]
       [--size 512] [--jitter 0.25] [--workers 4]

For every seed (``site.fakeworld.make_world``) the base's allocation requests are replayed twice
(``site.floorsim.simulate``): ``first-fit`` (no planned floor, the allocator before the floor) and
``floor`` (``site.floorplan`` slots from ``floor_blocks``), and once more with ``--jitter`` on
the requested lengths. Prints per seed and the totals: supplier-consumer link length (sum, max),
bus crossings of private links, bus-lane consumers upstream of their producer, growth steps that
stayed in frame, allocation failures. The catalog (optional) gives the rocket bill blocks and the
real footprints; without one only the roadmap blocks with fallback sizes are used.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

MODES = (
    ("first-fit", False, 0.0),
    ("floor", True, 0.0),
    ("first-fit+jitter", False, None),
    ("floor+jitter", True, None),
)


def _setup(catalog_path):
    from factorio_agent.floor_blocks import floor_blocks
    from factorio_agent.site.planmodel import SiteConfig

    if catalog_path:
        from factorio_agent.core.world_catalog import WorldCatalog
        from factorio_agent.rocket_plan import site_config

        catalog = WorldCatalog(json.loads(Path(catalog_path).read_text(encoding="utf-8")))
        config = site_config(catalog)
    else:
        catalog, config = None, SiteConfig()
    return config, floor_blocks(catalog, config.lane_order)


def _run(args):
    seed, size, jitter, catalog_path = args
    from factorio_agent.site.fakeworld import make_world
    from factorio_agent.site.floorsim import simulate
    from factorio_agent.site.planmodel import SiteError

    config, blocks = _setup(catalog_path)
    rows = {}
    for label, use_floor, mode_jitter in MODES:
        world = make_world(seed, size=size)
        try:
            result = simulate(
                world, config, blocks, use_floor=use_floor, jitter=jitter if mode_jitter is None else 0.0, seed=seed
            )
        except SiteError as error:
            rows[label] = {"error": str(error)[:120]}
            continue
        rows[label] = dict(vars(result))
    return seed, rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--jitter", type=float, default=0.25)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    catalog = str(args.catalog) if args.catalog else None
    jobs = [(seed, args.size, args.jitter, catalog) for seed in range(1, args.seeds + 1)]
    with ProcessPoolExecutor(max_workers=max(1, min(8, args.workers))) as pool:
        results = sorted(pool.map(_run, jobs))
    if args.json:
        print(json.dumps(results))
        return 0
    keys = (
        "link_sum",
        "links",
        "link_max",
        "core_sum",
        "core_links",
        "core_max",
        "crossings",
        "upstream",
        "grown",
        "growth_tried",
        "failures",
        "allocated",
    )
    totals = {label: dict.fromkeys(keys, 0.0) for label, _f, _j in MODES}
    skipped = {label: 0 for label, _f, _j in MODES}
    for seed, rows in results:
        parts = []
        for label, _f, _j in MODES:
            row = rows[label]
            if "error" in row:
                skipped[label] += 1
                parts.append(f"{label}: {row['error']}")
                continue
            for key in keys:
                old = totals[label][key]
                totals[label][key] = max(old, row[key]) if key.endswith("_max") else old + row[key]
            parts.append(
                f"{label}: core {row['core_sum']:.0f}/{row['core_links']} max {row['core_max']:.0f} "
                f"all {row['link_sum']:.0f}/{row['links']} cross {row['crossings']} up {row['upstream']} "
                f"grown {row['grown']}/{row['growth_tried']} fail {row['failures']}"
                + (f" {row['notes'][0]}" if row["notes"] else "")
            )
        print(f"seed {seed:>3}  " + " | ".join(parts))
    for label, _f, _j in MODES:
        t = totals[label]
        print(
            f"TOTAL {label:<16} core links {t['core_links']:.0f} sum {t['core_sum']:.0f} "
            f"mean {t['core_sum'] / max(1, t['core_links']):.1f} max {t['core_max']:.0f} | all links "
            f"{t['links']:.0f} sum {t['link_sum']:.0f} mean {t['link_sum'] / max(1, t['links']):.1f} "
            f"max {t['link_max']:.0f} | crossings {t['crossings']:.0f} upstream {t['upstream']:.0f} "
            f"in-frame growth {t['grown']:.0f}/{t['growth_tried']:.0f} failures {t['failures']:.0f} "
            f"allocated {t['allocated']:.0f} plan_errors {skipped[label]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
