"""Belt bandwidth audit of saved plans (``belt_flow``; docs/v2/LAYOUT_PRINCIPLES.md "Belt lanes and bandwidth").

Every plan that publishes ports or feeds consumers is pushed through the lane flow model:
input ports feed a full belt (both lanes, or the lane the port declares), mining drills drop
their nominal rate on the near lane, inserters that pick from a belt are consumers. Where the
plan declares a demand (furnace blocks and smelting banks per furnace at full reserved growth,
bus and trunk taps per branch) the delivered capacity is checked against it x ``MARGIN``;
everywhere the structural findings (side-load before a splitter, starvation, unmodelled
joins) apply. Pure: reads saved state files, never the game.

``audit_root(root)`` audits one runtime; the observe receipt ``belt_bandwidth``
(``receipt``) adds the rework proposals for built blocks whose feed is below their demand,
and ``tools/fleet.py report`` prints the finding counts per block type (``run_counts``,
``format_counts``).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from . import belt_flow
from .belt_flow import MARGIN, full_belt, inserter_lanes, kind_of, tile_of

INSERTERS = ("inserter", "long-handed-inserter", "fast-inserter", "burner-inserter", "bulk-inserter", "stack-inserter")
# Machine footprints (tiles) for "what does this inserter drop into".
FOOTPRINT = {
    "stone-furnace": 2,
    "steel-furnace": 2,
    "electric-furnace": 3,
    "assembling-machine-1": 3,
    "assembling-machine-2": 3,
    "assembling-machine-3": 3,
    "lab": 3,
    "chemical-plant": 3,
    "burner-mining-drill": 2,
    "electric-mining-drill": 3,
}
# Nominal drill output (items/min, mining speed x 60 / mining time 1 for ore and coal).
DRILL_RATE = {"electric-mining-drill": 30.0, "burner-mining-drill": 15.0}
DRILL_REACH = {"electric-mining-drill": 2, "burner-mining-drill": 1.5}
# Stone/steel furnace: crafting speed, burner power (W). Plates are 3.2 s from one ore; coal 4 MJ.
FURNACE_SPEED = {"stone-furnace": 1.0, "steel-furnace": 2.0}
FURNACE_WATTS = {"stone-furnace": 90000.0, "steel-furnace": 90000.0}
PLATE_SECONDS = 3.2
COAL_JOULES = 4.0e6
SHORTFALL = ("capacity_shortfall", "single_lane_feed", "lane_starvation")
MAX_KEPT = 12  # findings kept per block in a receipt


def _occupancy(entities) -> dict:
    """Tile -> entity for machines with a footprint (furnaces, assemblers, labs, drills)."""
    tiles = {}
    for e in entities:
        size = FOOTPRINT.get(e.get("name"))
        if not size:
            continue
        x, y = e["position"]["x"], e["position"]["y"]
        x0, y0 = math.floor(x - size / 2 + 1e-9), math.floor(y - size / 2 + 1e-9)
        for dx in range(size):
            for dy in range(size):
                tiles[(x0 + dx, y0 + dy)] = e
    return tiles


def _belt_tiles(entities) -> dict:
    tiles = {}
    for e in entities:
        k = kind_of(e.get("name", ""))
        if k in ("belt", "underground"):
            tiles[tile_of(e["position"])] = e
    return tiles


def port_sources(ports) -> tuple[list, list, list]:
    """(sources, sinks, reported) of a plan's item ports: inputs feed a full belt, outputs are sinks."""
    sources, sinks, reported = [], [], []
    for p in ports or []:
        if not isinstance(p, dict) or not isinstance(p.get("position"), dict) or p.get("kind", "item") != "item":
            continue
        item = p.get("item")
        if not item:
            continue
        name = p.get("name") or f"{p.get('direction')}:{item}"
        reported.append({"name": name, "item": item, "tile": tile_of(p["position"])})
        if p.get("direction") == "output":
            sinks.append(p["position"])
            continue
        lane = p.get("lane")
        lanes = (lane,) if lane in ("left", "right") else belt_flow.LANES
        sources.append(full_belt(item, p["position"], lanes=lanes))
    return sources, sinks, reported


def inserter_rows(entities, belts=None, machines=None):
    """``(entity, pickup, drop, machine at drop, machine at pickup)`` for every inserter."""
    belts = _belt_tiles(entities) if belts is None else belts
    machines = _occupancy(entities) if machines is None else machines
    rows = []
    for e in entities:
        if e.get("name") not in INSERTERS:
            continue
        pickup, drop, _reach = inserter_lanes(e)
        rows.append((e, pickup, drop, machines.get(drop), machines.get(pickup)))
    return rows


def drill_sources(entities, belts) -> list:
    out = []
    for e in entities:
        rate = DRILL_RATE.get(e.get("name"))
        if not rate:
            continue
        v = belt_flow.VECTORS[int(e.get("direction", 0)) % 16]
        reach = DRILL_REACH[e["name"]]
        drop = tile_of((e["position"]["x"] + reach * v[0], e["position"]["y"] + reach * v[1]))
        belt = belts.get(drop)
        if belt is None:
            continue
        side = (-v[0], -v[1])
        far = belt_flow.drop_lane(int(belt.get("direction", 0)) % 16, side)
        near = "left" if far == "right" else "right"
        out.append(
            {
                "tile": drop,
                "item": e.get("item") or e.get("resource") or "ore",
                "lane": near,
                "rate": rate,
                "mode": "drop",
            }
        )
    return out


def audit_plan(
    entities,
    ports=(),
    *,
    demand_for=None,
    product_for=None,
    extra_sources=(),
    extra_consumers=(),
    sinks=(),
    groups=(),
    margin=MARGIN,
    item=None,
) -> dict:
    """Flow + findings of one plan.

    ``demand_for(inserter, machine) -> {item: per min}`` gives a consumer's demand (None: a
    consumer without declared demand, which only keeps its line live); ``product_for(inserter,
    machine) -> (item, per min)`` makes an inserter dropping onto a belt a source.
    """
    entities = [e for e in entities if isinstance(e, dict) and isinstance(e.get("position"), dict)]
    belts = _belt_tiles(entities)
    machines = _occupancy(entities)
    sources, port_sinks, reported = port_sources(ports)
    sources = list(sources) + list(extra_sources) + drill_sources(entities, belts)
    if item:
        for s in sources:
            if s.get("item") == "ore":
                s["item"] = item
    consumers, all_sinks = list(extra_consumers), list(port_sinks) + list(sinks)
    for e, pickup, drop, at_drop, at_pickup in inserter_rows(entities, belts, machines):
        if pickup in belts:
            need = demand_for(e, at_drop) if demand_for else None
            if need:
                consumers.append({"tile": pickup, "demand": need})
            else:
                all_sinks.append(pickup)
        if drop in belts and product_for is not None:
            made = product_for(e, at_pickup)
            if made:
                belt = belts[drop]
                v = belt_flow.VECTORS[int(e.get("direction", 0)) % 16]
                lane = belt_flow.drop_lane(int(belt.get("direction", 0)) % 16, v)
                sources.append({"tile": drop, "item": made[0], "lane": lane, "rate": made[1], "mode": "drop"})
    for c in consumers:
        all_sinks.append(c["tile"])
    flow = belt_flow.solve(entities, sources, consumers, sinks=all_sinks, ports=reported, groups=groups, margin=margin)
    return belt_flow.summary(flow)


# ----- block adapters -----------------------------------------------------------------------
def furnace_bank(
    entities, ports, *, raw, raw_per_furnace, coal_per_furnace, product=None, product_per_furnace=0.0, margin=MARGIN
) -> dict:
    """A furnace bank (district bank, steel or brick block) with every furnace's input demand."""

    def demand(_inserter, machine):
        if machine is None or "furnace" not in machine.get("name", ""):
            return None
        return {raw: raw_per_furnace, "coal": coal_per_furnace}

    def made(_inserter, machine):
        if machine is None or "furnace" not in machine.get("name", "") or not product:
            return None
        return product, product_per_furnace

    return audit_plan(entities, ports, demand_for=demand, product_for=made, margin=margin)


def _with_growth(plan: dict) -> list:
    """Built and reserved entities: growth slots are declared demand (they are built in place)."""
    keys = set()
    out = []
    for e in list(plan.get("entities") or []) + list(plan.get("growth_entities") or []):
        if not isinstance(e, dict) or "position" not in e:
            continue
        key = (e.get("name"), e["position"]["x"], e["position"]["y"])
        if key not in keys:
            keys.add(key)
            out.append(e)
    return out


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def rated_per_furnace(state: dict) -> tuple[float, float, float]:
    rated = state.get("rated") or {}
    count = max(1, int(rated.get("furnace_count") or 1))
    return (
        float(rated.get("raw_per_minute") or 0) / count,
        float(rated.get("coal_per_minute_at_max_catalog_usage") or 0) / count,
        float(rated.get("product_per_minute") or 0) / count,
    )


def furnace_block_rows(root: Path) -> list:
    """``planned-steel.json`` / ``planned-brick.json``: the bank at full reserved growth."""
    rows = []
    for kind, owner in (("steel", "steel_production"), ("brick", "brick_production")):
        state = _load(root / f"planned-{kind}.json")
        if not isinstance(state, dict) or not isinstance(state.get("bank"), dict):
            continue
        bank = state["bank"]
        raw, coal, made = rated_per_furnace(state)
        result = furnace_bank(
            _with_growth(bank),
            bank.get("ports"),
            raw=bank.get("resource") or (state.get("plan") or {}).get("raw"),
            raw_per_furnace=raw,
            coal_per_furnace=coal,
            product=bank.get("product"),
            product_per_furnace=made,
        )
        rows.append(
            _row(owner, "furnace_block", result, phase=state.get("phase"), head=bank.get("feed_head", "shared"))
        )
    return rows


def district_rows(root: Path) -> list:
    state = _load(root / "smelting-district.json")
    rows = []
    if not isinstance(state, dict):
        return rows
    for bank in state.get("banks") or []:
        if not isinstance(bank, dict):
            continue
        furnace = next((e["name"] for e in bank.get("entities", []) if e.get("name") in FURNACE_SPEED), "stone-furnace")
        ore = FURNACE_SPEED[furnace] * 60 / PLATE_SECONDS
        coal = FURNACE_WATTS[furnace] * 60 / COAL_JOULES
        result = furnace_bank(
            _with_growth(bank),
            bank.get("ports"),
            raw=bank.get("resource"),
            raw_per_furnace=ore,
            coal_per_furnace=coal,
            product=bank.get("product"),
            product_per_furnace=ore,
        )
        rows.append(
            _row(
                f"smelting_district:{bank.get('resource')}",
                "smelting_bank",
                result,
                head=bank.get("feed_head", "shared"),
            )
        )
    return rows


def _tap_consumers(taps, entities=None, rate=None) -> tuple[list, list]:
    """Branch ends of taps with declared demand, and every tap entity.

    With ``entities`` (the rest of the plan), a tap whose trunk is not part of the plan (a
    produce row's tap on the bus) gets the trunk belt behind and ahead of it from
    ``trunk_side``: a full belt of the item in, the trunk continuing off-plan out.
    """
    consumers, out = [], []
    present = None
    if entities is not None:
        present = {tile_of(e["position"]) for e in entities if isinstance(e, dict) and kind_of(e.get("name", ""))}
    for tap in taps or []:
        if not isinstance(tap, dict):
            continue
        tap_entities = [e for e in tap.get("entities") or [] if isinstance(e, dict)]
        out += tap_entities
        port = tap.get("port")
        demand = tap.get("demand_per_minute")
        if isinstance(port, dict) and isinstance(port.get("position"), dict) and tap.get("item") and demand:
            consumers.append({"tile": tile_of(port["position"]), "demand": {tap["item"]: float(demand)}})
        if present is not None and tap.get("item"):
            trunk, sources, sinks = trunk_side(tap, tap["item"], rate)
            if trunk and tile_of(trunk[0]["position"]) not in present:
                out += trunk
                consumers += [{"tile": s["tile"], "source": s} for s in sources]
                primary = tap.get("primary_demand")
                if primary:  # the trunk's own consumer downstream, declared on the tap
                    consumers += [{"tile": t, "demand": {tap["item"]: float(primary)}} for t in sinks]
                else:
                    consumers += [{"tile": t, "sink": True} for t in sinks]
    return consumers, out


def _split_rows(rows) -> tuple[list, list, list]:
    """``_tap_consumers`` rows -> (consumers, sources, sinks)."""
    consumers, sources, sinks = [], [], []
    for r in rows:
        if "source" in r:
            sources.append(r["source"])
        elif r.get("sink"):
            sinks.append(r["tile"])
        else:
            consumers.append(r)
    return consumers, sources, sinks


def _merged(*groups) -> list:
    """Entities of several plans; a later group replaces what stands on the same tile
    (a tap's splitter and branch replace the trunk belts it was cut into)."""
    by_tile = {}
    for group in groups:
        for e in group or []:
            if not isinstance(e, dict) or not isinstance(e.get("position"), dict):
                continue
            if kind_of(e.get("name", "")) == "splitter":
                d = int(e.get("direction", 0)) % 16
                lv = belt_flow.left_vector(d)
                x, y = e["position"]["x"], e["position"]["y"]
                tiles = [tile_of((x + k * 0.5 * lv[0], y + k * 0.5 * lv[1])) for k in (1, -1)]
            elif kind_of(e.get("name", "")):
                tiles = [tile_of(e["position"])]
            else:
                tiles = [("entity", e.get("name"), e["position"]["x"], e["position"]["y"])]
            for tile in tiles:
                old = by_tile.get(tile)
                if old is not None and old is not e:
                    for key, value in list(by_tile.items()):
                        if value is old:
                            del by_tile[key]
                by_tile[tile] = e
    seen, out = set(), []
    for e in by_tile.values():
        if id(e) not in seen:
            seen.add(id(e))
            out.append(e)
    return out


def bus_rows(root: Path) -> list:
    state = _load(root / "planned-main-bus.json")
    if not isinstance(state, dict) or not isinstance(state.get("plan"), dict):
        return []
    plan = state["plan"]
    rows, tap_entities = _tap_consumers(state.get("taps"), plan.get("entities"))
    consumers, sources, sinks = _split_rows(rows)
    result = audit_plan(
        _merged(plan.get("entities"), tap_entities),
        plan.get("ports"),
        extra_consumers=consumers,
        extra_sources=sources,
        sinks=sinks,
    )
    return [_row("planned_main_bus", "bus", result)]


def plan_rows(root: Path) -> list:
    """Produce rows, the green science cluster: structural audit of plan + taps."""
    rows = []
    paths = sorted(root.glob("production-row-*.json")) + [root / "logistic-science.json"]
    for path in paths:
        state = _load(path)
        if not isinstance(state, dict) or not isinstance(state.get("plan"), dict):
            continue
        plan = state["plan"]
        rows_, tap_entities = _tap_consumers(state.get("taps"), plan.get("entities"))
        consumers, sources, sinks = _split_rows(rows_)
        result = audit_plan(
            _merged(plan.get("entities"), tap_entities),
            plan.get("ports"),
            extra_consumers=consumers,
            extra_sources=sources,
            sinks=sinks,
        )
        if path.name == "logistic-science.json":
            rows.append(_row("logistic_science", "science_cluster", result))
        else:
            rows.append(_row(f"produce:{state.get('item')}", "produce_row", result))
    return rows


def mine_rows(root: Path) -> list:
    """Mining banks with their drills, plus the taps of the patch trunk that starts at their port."""
    state = _load(root / "mining-outposts.json")
    trunks = (_load(root / "mine-trunks.json") or {}).get("trunks") or {}
    rows = []
    for i, bank in enumerate((state or {}).get("banks") or []):
        plan = bank.get("plan") if isinstance(bank, dict) else None
        if not isinstance(plan, dict):
            continue
        ports = plan.get("ports") or []
        heads = {tile_of(p["position"]) for p in ports if isinstance(p, dict) and isinstance(p.get("position"), dict)}
        taps = []
        for name, trunk in trunks.items():
            if not isinstance(trunk, dict):
                continue
            head = name.split("@", 1)[-1].split(",")
            try:
                tile = tile_of((float(head[0]), float(head[1])))
            except (ValueError, IndexError):
                continue
            if tile in heads:
                taps += [t for t in (trunk.get("taps") or {}).values() if isinstance(t, dict)]
        rows_, tap_entities = _tap_consumers(taps, plan.get("entities"))
        consumers, sources, sinks = _split_rows(rows_)
        resource = bank.get("resource") or next((p.get("item") for p in ports if isinstance(p, dict)), None)
        result = audit_plan(
            _merged(plan.get("entities"), tap_entities),
            ports,
            extra_consumers=consumers,
            extra_sources=sources,
            sinks=sinks,
            item=resource,
        )
        rows.append(_row(f"mine:{resource}:{i}", "mine", result))
    return rows


def feed_rows(root: Path) -> list:
    """Power coal feed (``factory-builder.json`` ``coal_plan``), wood feed, material-supply hubs."""
    rows = []
    builder = _load(root / "factory-builder.json")
    plan = (builder or {}).get("coal_plan") if isinstance(builder, dict) else None
    if isinstance(plan, dict):
        rows.append(
            _row(
                "power_coal_feed",
                "power_coal_feed",
                audit_plan(plan.get("entities") or [], plan.get("ports"), item="coal"),
            )
        )
    wood = _load(root / "wood-burner-feed.json")
    layout = (wood or {}).get("layout") if isinstance(wood, dict) else None
    if isinstance(layout, dict):
        rows.append(
            _row("wood_burner_feed", "return_feed", audit_plan(layout.get("entities") or [], layout.get("ports")))
        )
    supply = _load(root / "material-supply.json")
    for name, row in ((supply or {}).get("supplies") or {}).items():
        if not isinstance(row, dict) or not isinstance(row.get("tap"), dict):
            continue
        hub, tap = row.get("hub") or {}, row["tap"]
        hub_entities = [e for e in hub.get("entities") or [] if isinstance(e, dict)]
        hub_ports = hub.get("ports") if isinstance(hub.get("ports"), dict) else {}
        rows_, tap_entities = _tap_consumers([tap], hub_entities)
        consumers, sources, sinks = _split_rows(rows_)
        port = tap.get("port") or {}
        if hub_entities and isinstance(
            port.get("position"), dict
        ):  # the hub continues the branch: its outputs are the sinks
            consumers = [c for c in consumers if c["tile"] != tile_of(port["position"])]
        result = audit_plan(
            _merged(tap_entities, hub_entities),
            list(hub_ports.values()),
            extra_sources=sources,
            extra_consumers=consumers,
            sinks=sinks,
        )
        rows.append(_row(f"material_supply:{name}", "material_supply", result))
    return rows


def trunk_side(tap: dict, item: str, rate=None) -> tuple[list, list, list]:
    """A tap recorded without its trunk: one trunk belt behind the trunk half (a full belt of
    ``item``) and one ahead of it (the trunk continuing off-plan, a sink)."""
    splitter = tap.get("splitter")
    if not isinstance(splitter, dict):
        return [], [], []
    d = int(splitter.get("direction", 0)) % 16
    v, lv = belt_flow.VECTORS[d], belt_flow.left_vector(d)
    taken = {tile_of(e["position"]) for e in tap.get("entities") or [] if isinstance(e, dict)}
    cx, cy = splitter["position"]["x"], splitter["position"]["y"]
    for k in (1, -1):
        half = (cx + k * 0.5 * lv[0], cy + k * 0.5 * lv[1])
        ahead = tile_of((half[0] + v[0], half[1] + v[1]))
        if ahead in taken:
            continue  # the branch leaves from this half
        behind = (half[0] - v[0], half[1] - v[1])
        belts = [
            {
                "name": "transport-belt",
                "position": {"x": math.floor(p[0]) + 0.5, "y": math.floor(p[1]) + 0.5},
                "direction": d,
            }
            for p in (behind, (half[0] + v[0], half[1] + v[1]))
        ]
        source = full_belt(item, behind)
        if rate:
            source["rate"] = float(rate)  # the trunk carries what its bank mines, not a full belt
        return belts, [source], [ahead]
    return [], [], []


ADAPTERS = (furnace_block_rows, district_rows, bus_rows, plan_rows, mine_rows, feed_rows)


def _row(block: str, kind: str, result: dict, **extra) -> dict:
    return {"block": block, "type": kind, **extra, **result}


def audit_root(root) -> list:
    """Bandwidth rows of every saved plan of one runtime (an adapter error is a row, never a crash)."""
    root = Path(root)
    rows = []
    for adapter in ADAPTERS:
        try:
            rows += adapter(root)
        except Exception as exc:  # noqa: BLE001 - one malformed record must not hide the others
            rows.append(
                {
                    "block": adapter.__name__,
                    "type": "error",
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                    "findings": [],
                    "counts": {},
                }
            )
    return rows


def counts_by_type(rows) -> dict:
    """``{block type: {finding kind: n}}``."""
    out = {}
    for row in rows:
        for kind, n in (row.get("counts") or {}).items():
            bucket = out.setdefault(row["type"], {})
            bucket[kind] = bucket.get(kind, 0) + n
    return out


def rework_proposals(rows) -> list:
    """Built furnace blocks whose feed delivers less than their declared demand (receipt only)."""
    out = []
    for row in rows:
        if row.get("type") not in ("furnace_block", "smelting_bank"):
            continue
        if row.get("type") == "furnace_block" and row.get("phase") not in (
            "verified",
            "growing",
            "production",
            "supply",
        ):
            continue
        short = [f for f in row.get("findings", []) if f["kind"] in SHORTFALL]
        waste = [f for f in row.get("findings", []) if f["kind"] == "side_load_before_splitter"]
        if not short:
            continue
        out.append(
            {
                "block": row["block"],
                "status": "proposed",
                "action": "rework_feed_head",
                "replacement": "split_feed_head (planned_furnaces.split_feed_head: split first, side-load each row)",
                "cause": "side_load_before_splitter" if waste else short[0]["kind"],
                "shortfalls": [
                    {k: f.get(k) for k in ("kind", "item", "tile", "delivered_per_minute", "demand_per_minute")}
                    for f in short[:4]
                ],
                "rule": "plan republished first; new head built beside the old one, the old head mined only after "
                "the rows carry raw and coal from the new one; never removes the only feed of a working row",
                "next": "not built by any capability yet: the growth guard (planned_furnaces.growth_evidence) "
                "stops growth past the head's capacity until the rework exists",
            }
        )
    return out


def receipt(root, obs=None) -> dict:
    """Observe receipt ``belt_bandwidth``: per block counts (findings trimmed), totals, rework proposals."""
    rows = audit_root(root)
    compact = [
        {
            "block": r["block"],
            "type": r["type"],
            "counts": r.get("counts") or {},
            "findings": (r.get("findings") or [])[:MAX_KEPT],
            **({"error": r["error"]} if r.get("error") else {}),
        }
        for r in rows
    ]
    return {
        "blocks": compact,
        "counts_by_type": counts_by_type(rows),
        "rework": rework_proposals(rows),
        "scope": "planned layouts through the lane flow model (belt_flow); capacities, not measured flow",
    }


def run_counts(root) -> dict:
    """Fleet report: finding counts per block type of one run (pure, from its state files)."""
    return counts_by_type(audit_root(root))


def format_counts(runs) -> list:
    """Fleet report lines: per block type and finding kind, total count and runs with it."""
    totals = {}
    for r in runs:
        for kind, counts in (r.get("bandwidth") or {}).items():
            for finding, n in counts.items():
                row = totals.setdefault((kind, finding), [0, 0])
                row[0] += n
                row[1] += 1
    if not totals:
        return []
    lines = ["belt bandwidth findings (block type / finding: count in runs):"]
    for (kind, finding), (n, runs_with) in sorted(totals.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"  {kind:18s} {finding:26s} {n:4d} in {runs_with} runs")
    return lines
