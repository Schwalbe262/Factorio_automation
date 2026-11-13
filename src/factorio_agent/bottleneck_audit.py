"""Bottleneck auditor: sustained starvation, back-pressure and idleness, sampled online (PLANNERS.md).

Every CLI ``observe`` (one per supervisor cycle) adds at most one compact
sample per ``SAMPLE_TICKS`` to ``bottleneck-samples.json`` (rolling window of
``WINDOW_TICKS``, world-bound like ``rate-samples.json``). A sample is cheap:
it reuses the observation the pass already has.

Evidence per sample:

- machine groups: every assembler, furnace, mining drill and lab, grouped by
  board block (the plan entity at that position, ``pcb-board.json``) and
  product (recipe main product, the drill block's resource, ``research`` for
  labs); counts working / starved per missing item (recipe ingredient below
  its amount, the fuel for ``no_fuel``, the research's packs a lab lacks) /
  output full / no power / no research / other;
- waiting jobs: the items each one claims (``critical_path.claims``, minus its
  own products): construction demand is a consumer too;
- belt ports: lane fill at typed board ports (``belt_lane_observation``);
- chest stock of every item a group makes or uses; lab-block belt packs;
- actor: the jobs' action total (``last_action`` keeps the tick it last moved).

Diagnosis (``diagnose``): for each consumer group starved on an item for at
least ``STARVED_SHARE`` of the window, follow the item upstream through the
recipe graph (producer groups of the item, registered blocks before
transitional ones) to the first producer that is not itself starved:

- ``at_capacity``: the producer works (``CAPACITY_SHARE``) -> it is the bottleneck;
- ``transport``: the producer's output is full (``BACKED_SHARE``) while the consumer
  starves -> the belt between them (``belt_bandwidth`` findings attached);
- ``no_power``: the producer has no power;
- ``no_producer``: nothing makes the item (a raw resource: no mine; else no block).

Labs that work while their pack producers are output-full (or packs back up
on the lab belt) are ``consumer_capacity`` (the labs are the limit). Labs
without research are an ``idle`` entry (follow-up: the research queue), not a
bottleneck. Ranking: consumer seconds lost per minute (machine-seconds of
starved consumers; a waiting job counts 60 s/min shared over its items).

Each bottleneck carries ``proposal`` (``growth_providers.assess``): the
chosen growth package or the gate that keeps it waiting. The idle watchdog
(``idle_with_work``) reports when the actor has not acted for
``IDLE_SECONDS`` while a bottleneck loses time, a blocked job has an untried
remedy or ready queue work cannot dispatch, naming each item and its gate.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

STATE_FILE = "bottleneck-samples.json"
SAMPLE_TICKS = 300  # at most one sample per 5 game seconds
WINDOW_TICKS = 18000  # 5 game minutes of evidence
MAX_SAMPLES = 64
MIN_SAMPLES = 3  # sustained: at least three samples ...
MIN_SPAN_TICKS = 1800  # ... spanning at least 30 game seconds
STARVED_SHARE = 0.25  # a consumer missing an item this share of the window starves
BACKED_SHARE = 0.5  # output full this share of the window: back-pressure
CAPACITY_SHARE = 0.7  # working this share of the window: at capacity
NO_POWER_SHARE = 0.5
NO_RESEARCH_SHARE = 0.5
LAB_BACKUP_PACKS = 2  # every pack at least this many in a lab: a lab backlog (science_growth)
BELT_BACKUP_PACKS = 8  # packs on the lab block's belts: a belt backlog (science_growth)
CONSUMER_CAPACITY_FLOOR = 6.0  # seconds/min credited to a labs-limited block whose packs only back up
IDLE_SECONDS = 60.0  # actor without an action this long while work exists: idle_with_work
MAX_DEPTH = 6
MAX_BOTTLENECKS = 8
MAX_GROUPS = 24
MAX_WORK = 8
LANE_ITEMS_PER_TILE = 4.0  # a belt tile lane holds four items
FUEL_ITEM = "coal"
SHORTFALL = ("capacity_shortfall", "single_lane_feed", "lane_starvation")  # bandwidth_audit.SHORTFALL
LOW_QUEUE = 2  # supervisor.LOW_QUEUE: below it the roadmap autopilot runs
GROWTH_PASS = True  # Supervisor._growth merges proposals every cycle (else only the queue-gated autopilot)

WORKING = frozenset({"working", "normal", "low_power", "low_input_fluid", "preparing_rocket_launch"})
STARVED = frozenset(
    {
        "no_ingredients",
        "item_ingredient_shortage",
        "fluid_ingredient_shortage",
        "no_input_fluid",
        "missing_required_fluid",
        "missing_science_packs",
        "no_fuel",
    }
)
FULL = frozenset({"full_output", "waiting_for_space_in_destination", "output_full"})
NO_POWER = frozenset({"no_power"})
NO_RESEARCH = frozenset({"no_research_in_progress"})
MACHINE_TYPES = frozenset({"assembling-machine", "furnace", "lab", "mining-drill", "rocket-silo"})
CHEST_TYPES = frozenset({"container", "logistic-container"})
# Board blocks that are temporary by design: their growth is never proposed (their replacement is).
TRANSITIONAL_BLOCKS = frozenset({"startup_science", "supply_cell", "unregistered"})
ACTIVE_JOB = frozenset({"pending", "running", "waiting"})
QUEUED = frozenset({"pending", "ready"})


# ----- evidence ----------------------------------------------------------------------------
def _table(catalog, name) -> Mapping:
    """A catalog table of a ``WorldCatalog`` (attribute) or of its JSON payload (key)."""
    if catalog is None:
        return {}
    value = catalog.get(name) if isinstance(catalog, Mapping) else getattr(catalog, name, None)
    return value if isinstance(value, Mapping) else {}


def _xy(position) -> tuple | None:
    if not isinstance(position, Mapping):
        return None
    x, y = position.get("x"), position.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return (float(x), float(y))


def block_index(board: Mapping) -> dict[tuple, str]:
    """``(entity name, x, y) -> board block`` of every planned entity (built or not)."""
    index: dict[tuple, str] = {}
    blocks = board.get("blocks") if isinstance(board, Mapping) else None
    for block, row in (blocks or {}).items() if isinstance(blocks, Mapping) else ():
        for e in (row or {}).get("entities") or () if isinstance(row, Mapping) else ():
            if isinstance(e, Mapping) and isinstance(e.get("name"), str):
                xy = _xy(e.get("position"))
                if xy is not None:
                    index.setdefault((e["name"], *xy), block)
    return index


def _main_product(recipe: Mapping) -> str | None:
    if isinstance(recipe.get("main_product"), str):
        return recipe["main_product"]
    for p in recipe.get("products") or ():
        if isinstance(p, Mapping) and isinstance(p.get("name"), str):
            return p["name"]
    return None


def _recipe_for_product(catalog, product) -> str | None:
    recipes = _table(catalog, "recipes")
    if product in recipes and _main_product(recipes[product]) == product:
        return product
    for name, recipe in recipes.items():
        if isinstance(recipe, Mapping) and _main_product(recipe) == product:
            return name
    return None


def _ingredients(catalog, recipe_name) -> list[tuple[str, float]]:
    recipe = _table(catalog, "recipes").get(recipe_name)
    rows = []
    for row in (recipe or {}).get("ingredients") or () if isinstance(recipe, Mapping) else ():
        if isinstance(row, Mapping) and isinstance(row.get("name"), str):
            amount = row.get("amount")
            rows.append((row["name"], float(amount) if isinstance(amount, (int, float)) else 1.0))
    return rows


def _packs(catalog, technology) -> list[str]:
    tech = _table(catalog, "technologies").get(technology) if isinstance(technology, str) else None
    return [
        row["name"]
        for row in (tech or {}).get("ingredients") or ()
        if isinstance(tech, Mapping) and isinstance(row, Mapping) and isinstance(row.get("name"), str)
    ]


def _missing(entity: Mapping, ingredients: list[tuple[str, float]]) -> list[str]:
    """Ingredients below their recipe amount; none found -> the scarcest one (inserter timing)."""
    if not ingredients:
        return []
    held = dict(entity.get("inventory") or {})
    for fluid in entity.get("fluids") or () if isinstance(entity.get("fluids"), list) else ():
        if isinstance(fluid, Mapping) and isinstance(fluid.get("name"), str):
            held[fluid["name"]] = held.get(fluid["name"], 0) + float(fluid.get("amount") or 0)
    if isinstance(entity.get("fluids"), Mapping):
        for name, amount in entity["fluids"].items():
            if isinstance(amount, (int, float)):
                held[name] = held.get(name, 0) + amount
    short = [name for name, amount in ingredients if float(held.get(name, 0) or 0) < amount]
    if short:
        return short
    return [min(ingredients, key=lambda row: float(held.get(row[0], 0) or 0) / max(row[1], 1e-9))[0]]


def _holds_full_stack(entity: Mapping, product, catalog) -> bool:
    """The machine holds at least one full stack of its own product (its result slot is full)."""
    if not isinstance(product, str):
        return False
    stack = (_table(catalog, "items").get(product) or {}).get("stack_size")
    held = (entity.get("inventory") or {}).get(product, 0)
    return isinstance(stack, (int, float)) and stack > 0 and isinstance(held, (int, float)) and held >= stack


def entity_rows(observation: Mapping, board: Mapping, catalog) -> list[dict]:
    """One row per production entity: group, block, product, kind, state, missing items."""
    index = block_index(board)
    blocks = board.get("blocks") if isinstance(board, Mapping) and isinstance(board.get("blocks"), Mapping) else {}
    recipes = _table(catalog, "recipes")
    research = observation.get("research")
    packs = _packs(catalog, research)
    machines = [
        e
        for e in observation.get("entities") or ()
        if isinstance(e, Mapping) and e.get("type") in MACHINE_TYPES and _xy(e.get("position"))
    ]
    placed = []
    for e in machines:
        xy = _xy(e["position"])
        placed.append((e, index.get((e.get("name"), *xy), "unregistered"), xy))
    # A furnace without a current recipe takes the nearest same-block same-name sibling's recipe.
    known = {}
    for e, block, xy in placed:
        if isinstance(e.get("recipe"), str):
            known.setdefault((block, e.get("name")), []).append((xy, e["recipe"]))
    rows = []
    for e, block, xy in placed:
        kind = "lab" if e.get("type") == "lab" else "drill" if e.get("type") == "mining-drill" else "machine"
        status = str(e.get("status_name") or "")
        recipe = e.get("recipe") if isinstance(e.get("recipe"), str) else None
        product = None
        if kind == "lab":
            product = "research"
        elif kind == "drill":
            resource = (blocks.get(block) or {}).get("resource") if block in blocks else None
            product = resource if isinstance(resource, str) else None
        else:
            if recipe is None:
                siblings = known.get((block, e.get("name"))) or []
                if siblings:
                    recipe = min(siblings, key=lambda s: (s[0][0] - xy[0]) ** 2 + (s[0][1] - xy[1]) ** 2)[1]
                else:
                    row = blocks.get(block) or {}
                    if isinstance(row.get("recipe"), str):
                        recipe = row["recipe"]
                    elif isinstance(row.get("product"), str):
                        recipe = _recipe_for_product(catalog, row["product"])
            product = _main_product(recipes[recipe]) if recipe in recipes else None
        if status in WORKING:
            state = "working"
        elif status in FULL:
            state = "full"
        elif status in NO_POWER:
            state = "no_power"
        elif status in NO_RESEARCH and kind == "lab":
            state = "no_research"
        elif status in STARVED:
            state = "starved"
        else:
            state = "other"
        if state == "starved" and kind == "machine" and _holds_full_stack(e, product, catalog):
            # eval-1575138435 (257 min): both brick furnaces "no_ingredients" while each held 100
            # stone bricks (a full result stack, the output inserters waiting for space: nothing
            # takes bricks yet). Their product is not taken, so they are full, not starved; the
            # stone drills backing up behind them are no transport bottleneck.
            state = "full"
        missing: list[str] = []
        if state == "starved":
            if status == "no_fuel":
                missing = [FUEL_ITEM]
            elif kind == "lab":
                held = e.get("inventory") or {}
                missing = [p for p in packs if not held.get(p)] or list(packs[:1]) or ["science-pack"]
            else:
                missing = _missing(e, _ingredients(catalog, recipe)) if recipe else []
                missing = missing or ["unknown"]
        rows.append(
            {
                "group": f"{block}|{product or '?'}",
                "block": block,
                "product": product,
                "kind": kind,
                "state": state,
                "missing": missing,
                "recipe": recipe,
                "inventory": e.get("inventory") or {},
                "position": xy,
            }
        )
    return rows


def _belt_packs(observation, index, lab_blocks, packs) -> dict[str, int]:
    """Science packs on transport belts of each lab block."""
    totals: Counter = Counter()
    if not lab_blocks or not packs:
        return {}
    for e in observation.get("entities") or ():
        if not isinstance(e, Mapping) or e.get("type") != "transport-belt":
            continue
        xy = _xy(e.get("position"))
        block = index.get((e.get("name"), *xy)) if xy else None
        if block in lab_blocks:
            stock = e.get("belt_inventory") or {}
            totals[block] += sum(int(stock.get(p, 0) or 0) for p in packs)
    return dict(totals)


def _waiting_claims(jobs, catalog, observation) -> dict[str, list[str]]:
    """``{job key: [items]}`` for waiting jobs: what each waits for (``critical_path.claims``), not its own products."""
    from .critical_path import claims, known_items, supplied_items

    try:
        known = known_items(catalog, observation)
    except Exception:  # an estimate never breaks observe
        return {}
    made = {p for r in _table(catalog, "recipes").values() if isinstance(r, Mapping) for p in [_main_product(r)] if p}
    out = {}
    for job in jobs or ():
        if not isinstance(job, Mapping) or job.get("status") != "waiting":
            continue
        # Only items something can make or mine (Space Age's "science" pseudo-item is neither).
        items = sorted(i for i in set(claims(job, known)) - supplied_items(job) if i in made or _raw(catalog, i))
        if items:
            out[f"{job.get('capability')}:{job.get('key')}"] = items[:4]
    return out


def sample(observation: Mapping, board: Mapping, catalog, jobs=()) -> dict:
    """One compact evidence sample of the current observation (module docstring)."""
    rows = entity_rows(observation, board, catalog)
    groups: dict[str, dict] = {}
    for row in rows:
        g = groups.setdefault(
            row["group"],
            {
                "block": row["block"],
                "product": row["product"],
                "kind": row["kind"],
                "n": 0,
                "states": {},
                "missing": {},
            },
        )
        g["n"] += 1
        g["states"][row["state"]] = g["states"].get(row["state"], 0) + 1
        for item in row["missing"]:
            g["missing"][item] = g["missing"].get(item, 0) + 1
        if row["kind"] == "lab":
            packs = _packs(catalog, observation.get("research"))
            if packs and all(int(row["inventory"].get(p, 0) or 0) >= LAB_BACKUP_PACKS for p in packs):
                g["backlog"] = g.get("backlog", 0) + 1
    index = block_index(board)
    lab_blocks = {g["block"] for g in groups.values() if g["kind"] == "lab"}
    belt = _belt_packs(observation, index, lab_blocks, _packs(catalog, observation.get("research")))
    for g in groups.values():
        if g["kind"] == "lab" and belt.get(g["block"]):
            g["belt_packs"] = belt[g["block"]]
    relevant = {g["product"] for g in groups.values() if g["product"]} | {
        item for g in groups.values() for item in g["missing"]
    }
    stock: Counter = Counter()
    for e in observation.get("entities") or ():
        if isinstance(e, Mapping) and e.get("type") in CHEST_TYPES:
            for item, count in (e.get("inventory") or {}).items():
                if item in relevant and isinstance(count, (int, float)):
                    stock[item] += int(count)
    ports = {}
    lanes = observation.get("belt_lane_observation")
    for p in (lanes or {}).get("ports") or () if isinstance(lanes, Mapping) else ():
        if not isinstance(p, Mapping) or not isinstance(p.get("item"), str):
            continue
        xy = _xy(p.get("position"))
        if xy is None:
            continue
        fill = [
            round(
                min(
                    1.0,
                    sum(v for v in (p.get(side) or {}).values() if isinstance(v, (int, float))) / LANE_ITEMS_PER_TILE,
                ),
                2,
            )
            for side in ("left", "right")
        ]
        ports[f"{p['item']}@{xy[0]:g},{xy[1]:g}"] = fill
    actions = sum(int(j.get("actions") or 0) for j in jobs or () if isinstance(j, Mapping))
    return {
        "tick": observation.get("tick"),
        "research": observation.get("research"),
        "groups": groups,
        "jobs": _waiting_claims(jobs, catalog, observation),
        "ports": ports,
        "stock": dict(stock),
        "actions": actions,
    }


def record(root, observation: Mapping, new: dict) -> dict:
    """Append ``new`` to the world-bound rolling window (one per ``SAMPLE_TICKS``); the saved state."""
    from .core.deterministic_state import RunLock, _atomic_json

    path = Path(root) / STATE_FILE
    with RunLock(Path(root) / "observer.lock"):
        try:
            old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            old = {}
        if old.get("world_id") != observation.get("world_id"):
            old = {}
        samples = [s for s in old.get("samples") or () if isinstance(s, Mapping)]
        tick = new.get("tick")
        if samples and isinstance(tick, int) and tick < samples[-1].get("tick", 0):
            samples = []  # rollback: the evidence restarts
            old.pop("last_action", None)
        last = old.get("last_action") if isinstance(old.get("last_action"), Mapping) else None
        if last is None or new.get("actions") != last.get("actions"):
            last = {"actions": new.get("actions"), "tick": tick}
        if not samples or (isinstance(tick, int) and tick - samples[-1].get("tick", 0) >= SAMPLE_TICKS):
            samples.append(new)
        samples = [s for s in samples if isinstance(tick, int) and s.get("tick", 0) >= tick - WINDOW_TICKS]
        state = {"world_id": observation.get("world_id"), "samples": samples[-MAX_SAMPLES:], "last_action": last}
        _atomic_json(path, state)
    return state


# ----- window statistics -------------------------------------------------------------------
def window_stats(samples: list[Mapping]) -> dict:
    """Per group: shares of each state and of each missing item over the window's samples."""
    groups: dict[str, dict] = {}
    for s in samples:
        for key, g in (s.get("groups") or {}).items():
            row = groups.setdefault(
                key,
                {
                    "block": g.get("block"),
                    "product": g.get("product"),
                    "kind": g.get("kind"),
                    "units": 0,
                    "states": Counter(),
                    "missing": Counter(),
                    "backlog": 0,
                    "belt_packs": [],
                    "samples": 0,
                },
            )
            row["n"] = g.get("n", 0)  # latest count
            row["units"] += g.get("n", 0)
            row["samples"] += 1
            row["states"].update(g.get("states") or {})
            row["missing"].update(g.get("missing") or {})
            row["backlog"] += g.get("backlog", 0)
            row["belt_packs"].append(g.get("belt_packs", 0))
    out = {}
    for key, row in groups.items():
        units = max(row["units"], 1)
        out[key] = {
            "block": row["block"],
            "product": row["product"],
            "kind": row["kind"],
            "n": row["n"],
            "samples": row["samples"],
            "share": {state: round(count / units, 3) for state, count in row["states"].items()},
            "starved_on": {item: round(count / units, 3) for item, count in row["missing"].items()},
            "backlog_share": round(row["backlog"] / units, 3),
            "belt_packs": round(sum(row["belt_packs"]) / max(len(row["belt_packs"]), 1), 1),
        }
    jobs: dict[str, dict] = {}
    for s in samples:
        for job, items in (s.get("jobs") or {}).items():
            row = jobs.setdefault(job, {"items": Counter(), "samples": 0})
            row["samples"] += 1
            row["items"].update(items)
    n = max(len(samples), 1)
    waiting = {
        job: {"share": round(row["samples"] / n, 3), "items": {i: round(c / n, 3) for i, c in row["items"].items()}}
        for job, row in jobs.items()
    }
    ports: dict[str, list] = {}
    for s in samples:
        for port, fill in (s.get("ports") or {}).items():
            ports.setdefault(port, []).append(fill)
    port_fill = {
        port: [round(sum(f[i] for f in rows) / len(rows), 2) for i in (0, 1)]
        for port, rows in ports.items()
        if all(isinstance(f, list) and len(f) == 2 for f in rows)
    }
    ticks = [s.get("tick") for s in samples if isinstance(s.get("tick"), int)]
    return {
        "groups": out,
        "jobs": waiting,
        "ports": port_fill,
        "stock": dict(samples[-1].get("stock") or {}) if samples else {},
        "samples": len(samples),
        "span_ticks": (max(ticks) - min(ticks)) if ticks else 0,
        "research": samples[-1].get("research") if samples else None,
    }


# ----- diagnosis ---------------------------------------------------------------------------
def _raw(catalog, item) -> bool:
    from .progression import RAW_ITEMS

    sources = _table(catalog, "raw_sources")
    return item in sources or item.endswith("-ore") or item in RAW_ITEMS - {"iron-plate", "copper-plate"}


def _aggregate(rows: list[Mapping]) -> dict:
    total = sum(max(r.get("n", 0), 1) for r in rows) or 1
    share: Counter = Counter()
    starved: Counter = Counter()
    for r in rows:
        weight = max(r.get("n", 0), 1) / total
        for state, value in (r.get("share") or {}).items():
            share[state] += value * weight
        for item, value in (r.get("starved_on") or {}).items():
            starved[item] += value * weight
    return {"n": sum(r.get("n", 0) for r in rows), "share": dict(share), "starved_on": dict(starved)}


def _producers(groups: Mapping[str, Mapping], item: str) -> list[str]:
    found = [k for k, g in groups.items() if g.get("product") == item and g.get("kind") != "lab"]
    permanent = [k for k in found if groups[k].get("block") not in TRANSITIONAL_BLOCKS]
    return sorted(permanent or found, key=lambda k: (-groups[k].get("n", 0), k))


def _findings(observation: Mapping, item: str | None, blocks: Iterable[str], kinds=None) -> list[dict]:
    """``belt_bandwidth`` findings of the item or of the named blocks (transport evidence), optionally by kind."""
    audit = observation.get("belt_bandwidth")
    names = set(blocks)
    out = []
    for row in (audit or {}).get("blocks") or () if isinstance(audit, Mapping) else ():
        if not isinstance(row, Mapping):
            continue
        for f in row.get("findings") or ():
            if not isinstance(f, Mapping) or (kinds is not None and f.get("kind") not in kinds):
                continue
            if (item is not None and f.get("item") == item) or row.get("block") in names:
                out.append(
                    {
                        "block": row.get("block"),
                        **{k: f.get(k) for k in ("kind", "item", "delivered_per_minute", "demand_per_minute")},
                    }
                )
    return out[:4]


def _walk(groups, catalog, observation, item, seen, depth) -> tuple[dict, list]:
    """The first producer upstream of ``item`` that limits it, and the chain walked to it."""
    producers = _producers(groups, item)
    if not producers:
        kind = "no_producer"
        return {"kind": kind, "group": None, "item": item, "raw": _raw(catalog, item)}, [{"item": item, "state": kind}]
    agg = _aggregate([groups[k] for k in producers])
    share, starved = agg["share"], agg["starved_on"]
    link = {
        "item": item,
        "groups": producers[:3],
        "n": agg["n"],
        "working": round(share.get("working", 0), 2),
        "full": round(share.get("full", 0), 2),
        "starved": round(share.get("starved", 0), 2),
    }
    base = {"group": producers[0], "groups": producers, "item": item, "n": agg["n"], "share": share}
    if share.get("no_power", 0) >= NO_POWER_SHARE:
        return dict(base, kind="no_power"), [dict(link, state="no_power")]
    upstream = sorted(
        ((i, v) for i, v in starved.items() if v >= STARVED_SHARE and i not in seen and i != "unknown"),
        key=lambda row: -row[1],
    )
    if upstream and depth < MAX_DEPTH:
        next_item = upstream[0][0]
        found, chain = _walk(groups, catalog, observation, next_item, seen | {item}, depth + 1)
        return found, [dict(link, state=f"starved on {next_item}")] + chain
    if share.get("full", 0) >= BACKED_SHARE:
        return dict(base, kind="transport"), [dict(link, state="output full")]
    kind = "at_capacity" if share.get("working", 0) >= CAPACITY_SHARE else "partly_working"
    return dict(base, kind=kind), [dict(link, state=kind)]


def diagnose(stats: Mapping, catalog, observation: Mapping) -> tuple[list[dict], list[dict]]:
    """Ranked bottlenecks (consumers held back, seconds lost per minute) and idle entries."""
    groups = stats.get("groups") or {}
    found: dict[tuple, dict] = {}
    idle = []

    def charge(consumer, item, lost, n):
        target, chain = _walk(groups, catalog, observation, item, set(), 0)
        consumer_block = (groups.get(consumer) or {}).get("block")
        if (
            target["kind"] == "transport"
            and consumer_block
            and _findings(observation, None, {consumer_block}, SHORTFALL)
        ):
            # The producer backs up while the consumer starves and the consumer block's own feed is
            # short in the lane flow model (belt_bandwidth): the limit is that feed, not the producer.
            target = dict(target, group=consumer, groups=[consumer])
        key = (target.get("group") or f"none|{target['item']}", target["kind"], target["item"])
        row = found.setdefault(
            key,
            {
                "id": "|".join(str(k) for k in key),
                "group": target.get("group"),
                "block": (groups.get(target.get("group")) or {}).get("block"),
                "item": target["item"],
                "kind": target["kind"],
                "raw": target.get("raw", False),
                "groups": target.get("groups", []),
                "chain": chain,
                "consumers": [],
                "lost_seconds_per_minute": 0.0,
            },
        )
        if len(chain) > len(row["chain"]):
            row["chain"] = chain  # the longest walk: from the furthest consumer to the limit
        row["consumers"].append(
            {
                "group": consumer,
                "block": (groups.get(consumer) or {}).get("block") if not consumer.startswith("job:") else "job",
                "item": item,
                "n": n,
                "lost_seconds_per_minute": round(lost, 1),
            }
        )
        row["lost_seconds_per_minute"] += lost

    for key, g in sorted(groups.items()):
        if g.get("samples", 0) < 1:
            continue
        n = g.get("n", 0)
        if g.get("kind") == "lab":
            if g["share"].get("no_research", 0) >= NO_RESEARCH_SHARE:
                idle.append(
                    {
                        "group": key,
                        "kind": "no_research",
                        "seconds_per_minute": round(60 * n * g["share"]["no_research"], 1),
                        "follow_up": "research queue: no technology in progress",
                    }
                )
                continue
            pack_producers = sorted(
                {
                    k
                    for k, p in groups.items()
                    if p.get("kind") == "machine" and str(p.get("product", "")).endswith("science-pack")
                }
            )
            backed = _aggregate([groups[k] for k in pack_producers]) if pack_producers else None
            backed_full = backed["share"].get("full", 0) if backed else 0.0
            if g["share"].get("working", 0) >= CAPACITY_SHARE and (
                backed_full >= BACKED_SHARE
                or g.get("belt_packs", 0) >= BELT_BACKUP_PACKS
                or g.get("backlog_share", 0) >= 0.5
            ):
                lost = max(60 * (backed["n"] if backed else 0) * backed_full, CONSUMER_CAPACITY_FLOOR)
                fkey = (key, "consumer_capacity", "research")
                found.setdefault(
                    fkey,
                    {
                        "id": f"{key}|consumer_capacity|research",
                        "group": key,
                        "block": g.get("block"),
                        "item": "research",
                        "kind": "consumer_capacity",
                        "raw": False,
                        "groups": [key],
                        "chain": [
                            {
                                "item": "research",
                                "groups": [key],
                                "n": n,
                                "working": round(g["share"].get("working", 0), 2),
                                "state": "labs at capacity, packs back up",
                                "belt_packs": g.get("belt_packs", 0),
                                "producers_full": round(backed_full, 2),
                            }
                        ],
                        # The pack producers are held back by the labs (their output backs up).
                        "consumers": [
                            {
                                "group": k,
                                "block": groups[k].get("block"),
                                "item": groups[k].get("product"),
                                "n": groups[k].get("n"),
                                "lost_seconds_per_minute": round(
                                    60 * groups[k].get("n", 0) * groups[k]["share"].get("full", 0), 1
                                ),
                            }
                            for k in pack_producers[:3]
                        ],
                        "lost_seconds_per_minute": lost,
                    },
                )
        for item, value in sorted(g.get("starved_on", {}).items()):
            if value < STARVED_SHARE or item == "unknown":
                continue
            charge(key, item, 60 * n * value, n)
    for job, row in sorted((stats.get("jobs") or {}).items()):
        items = [i for i, v in row.get("items", {}).items() if v >= STARVED_SHARE]
        for item in items:
            charge("job:" + job, item, 60 * row["items"][item] / max(len(items), 1), 1)
    rates = (
        ((observation.get("production_rate_measurement") or {}).get("rates_per_game_minute") or {})
        if isinstance(observation.get("production_rate_measurement"), Mapping)
        else {}
    )
    stock = stats.get("stock") or {}
    ranked = sorted(found.values(), key=lambda r: (-r["lost_seconds_per_minute"], r["id"]))
    for row in ranked:
        row["lost_seconds_per_minute"] = round(row["lost_seconds_per_minute"], 1)
        rate = rates.get(row["item"]) if isinstance(rates, Mapping) else None
        evidence = {"samples": stats.get("samples"), "window_seconds": round(stats.get("span_ticks", 0) / 60)}
        if isinstance(rate, Mapping):
            evidence["rate_per_minute"] = {
                k: round(float(v), 1) for k, v in rate.items() if isinstance(v, (int, float))
            }
        if row["item"] in stock:
            evidence["chest_stock"] = stock[row["item"]]
        ports = {p: f for p, f in (stats.get("ports") or {}).items() if p.split("@")[0] == row["item"]}
        if ports:
            evidence["port_fill"] = dict(list(ports.items())[:4])
        if row["kind"] == "transport":
            blocks = {row.get("block")} | {(groups.get(c["group"]) or {}).get("block") for c in row["consumers"]}
            evidence["belt_findings"] = _findings(observation, row["item"], {b for b in blocks if b})
        row["evidence"] = evidence
        row["consumers"] = row["consumers"][:6]
    return ranked[:MAX_BOTTLENECKS], idle


def sustained(stats: Mapping) -> bool:
    return stats.get("samples", 0) >= MIN_SAMPLES and stats.get("span_ticks", 0) >= MIN_SPAN_TICKS


# ----- idle watchdog -----------------------------------------------------------------------
def _lane(node: Mapping) -> str:
    from .jobs import admission_lane

    return admission_lane(str(node.get("capability")), node.get("parameters"))


def queue_gate(jobs, queue_nodes) -> dict:
    """Whether the supervisor's roadmap autopilot runs now (``Supervisor._queue_low``) and what holds dispatch."""
    from .jobs import MAX_ACTIVE_JOBS, slots_used

    active = [j for j in jobs or () if isinstance(j, Mapping) and j.get("status") in ACTIVE_JOB]
    busy = {_lane(j) for j in active}
    queued = sum(1 for q in queue_nodes or () if q.get("status") in QUEUED and _lane(q) not in busy)
    try:
        slots = len(slots_used([dict(j) for j in active]))
    except Exception:
        slots = len(active)
    return {
        "queue_low": queued < LOW_QUEUE,
        "queued": queued,
        "busy_lanes": sorted(busy),
        "slots_used": slots,
        "slots": MAX_ACTIVE_JOBS,
    }


def idle_with_work(state: Mapping, bottlenecks: list[Mapping], jobs, queue_nodes, tick) -> dict | None:
    """``idle_with_work`` when the actor has not acted for ``IDLE_SECONDS`` while work exists, else None.

    Work: (a) a bottleneck losing consumer time (gate: its proposal's gate);
    (b) a blocked job whose remedy ladder names an untried rung (gate
    ``remedy_pending``) or a blocked job without ``needs`` (gate
    ``no_remedy_rule``); (c) ready queue work that cannot dispatch (gate:
    ``lane_busy:<lane>`` or ``job_slots_full``).
    """
    last = state.get("last_action") if isinstance(state.get("last_action"), Mapping) else None
    if not isinstance(tick, int) or not last or not isinstance(last.get("tick"), int):
        return None
    idle = (tick - last["tick"]) / 60.0
    if idle < IDLE_SECONDS:
        return None
    gate = queue_gate(jobs, queue_nodes)
    work = []
    for b in bottlenecks:
        if not b.get("lost_seconds_per_minute"):
            continue
        proposal = b.get("proposal") or {}
        why = proposal.get("gate") or "no_growth_rule"
        if proposal.get("status") == "propose":
            # Proposed but not merged: the growth pass merges it this cycle, else only the queue-gated autopilot.
            why = (
                "growth_pass_next_cycle"
                if GROWTH_PASS
                else "queue_not_low"
                if not gate["queue_low"]
                else "autopilot_limit"
            )
        work.append(
            {
                "kind": "bottleneck",
                "item": f"grow {b.get('group') or b.get('item')} ({b.get('kind')}, {b.get('item')})",
                "seconds_per_minute": b.get("lost_seconds_per_minute"),
                "gate": why,
                "why": str(proposal.get("reason") or "")[:160],
            }
        )
    for job in jobs or ():
        if not isinstance(job, Mapping) or job.get("status") != "blocked":
            continue
        name = f"{job.get('capability')}:{job.get('key')}"
        if job.get("next_remedy"):
            work.append(
                {
                    "kind": "remedy",
                    "item": f"{name} rung {job.get('next_remedy')}",
                    "gate": "remedy_pending",
                    "why": str(job.get("reason"))[:160],
                }
            )
        elif not job.get("needs"):
            work.append(
                {"kind": "blocked", "item": name, "gate": "no_remedy_rule", "why": str(job.get("reason"))[:160]}
            )
    busy = set(gate["busy_lanes"])
    for node in queue_nodes or ():
        if node.get("status") != "ready":
            continue
        lane = _lane(node)
        why = (
            f"lane_busy:{lane}"
            if lane in busy
            else "job_slots_full"
            if gate["slots_used"] >= gate["slots"]
            else "dispatch_pending"
        )
        work.append(
            {"kind": "queued", "item": str(node.get("id")), "gate": why, "why": str(node.get("reason") or "")[:160]}
        )
    if not work:
        return None
    gates = Counter(w["gate"].split(":")[0] for w in work)
    return {
        "idle_seconds": round(idle, 1),
        "since_tick": last["tick"],
        "work": work[:MAX_WORK],
        "gates": dict(gates.most_common()),
        "queue": gate,
    }


# ----- receipt -----------------------------------------------------------------------------
def _read(root, name, world_id) -> dict:
    path = Path(root) / name
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    if "world_id" in value and value.get("world_id") != world_id:
        return {}
    return value


def _compact_groups(stats: Mapping) -> list[dict]:
    rows = []
    for key, g in stats.get("groups", {}).items():
        starved = {i: v for i, v in g.get("starved_on", {}).items() if v >= 0.05}
        rows.append(
            {
                "group": key,
                "n": g.get("n"),
                **{
                    s: g["share"][s]
                    for s in ("working", "full", "starved", "no_power", "no_research")
                    if g["share"].get(s)
                },
                **({"starved_on": starved} if starved else {}),
            }
        )
    rows.sort(key=lambda r: (-(r.get("starved", 0) + r.get("full", 0)), -int(r.get("n") or 0), r["group"]))
    return rows[:MAX_GROUPS]


def assess(bottleneck: Mapping, observation: Mapping, state: Mapping) -> dict:
    """The growth proposal of one bottleneck (``growth_providers.assess``); never raises."""
    try:
        from .growth_providers import assess as provider_assess
    except ImportError:
        return {"status": "wait", "gate": "no_growth_rule", "reason": "no growth provider module"}
    try:
        return provider_assess(bottleneck, observation, state)
    except Exception as exc:  # a proposal estimate never breaks observe
        return {"status": "wait", "gate": "provider_error", "reason": f"{type(exc).__name__}: {exc}"[:200]}


def receipt(root, catalog, jobs, observation) -> dict | None:
    """Observe receipt ``bottlenecks``: sample, window, diagnosis, proposals, idle watchdog."""
    world = observation.get("world_id")
    board = _read(root, "pcb-board.json", world)
    jobs = [j for j in jobs or () if isinstance(j, Mapping)]
    state = record(root, observation, sample(observation, board, catalog, jobs))
    stats = window_stats(state["samples"])
    queue = _read(root, "work_queue.json", None).get("nodes") or []
    queue = [q for q in queue if isinstance(q, Mapping)]
    out: dict[str, Any] = {
        "window": {"samples": stats["samples"], "seconds": round(stats["span_ticks"] / 60)},
        "groups": _compact_groups(stats),
    }
    bottlenecks, idle = diagnose(stats, catalog, observation) if sustained(stats) else ([], [])
    from .jobs import CAPABILITIES

    context = {"jobs": jobs, "queue_nodes": queue, "capabilities": CAPABILITIES}
    for row in bottlenecks:
        row["proposal"] = assess(row, observation, context)
    out["bottlenecks"] = bottlenecks
    out["idle"] = idle
    if not sustained(stats):
        out["status"] = f"sampling ({stats['samples']}/{MIN_SAMPLES} samples, {round(stats['span_ticks'] / 60)}s)"
    watchdog = idle_with_work(state, bottlenecks, jobs, queue, observation.get("tick"))
    if watchdog is not None:
        out["idle_with_work"] = watchdog
    out["scope"] = "rolling window of observed machine states; consumer machine/job seconds, not output rates"
    return out


# ----- reports -----------------------------------------------------------------------------
def _events(root):
    path = Path(root) / "logs" / "controller-events.jsonl"
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"idle_with_work"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event") == "idle_with_work":
                yield row


def idle_episodes(events: Iterable[Mapping]) -> list[dict]:
    """Idle episodes from ``idle_with_work`` events: seconds idle while work existed, seconds per top gate.

    An episode is a run of events with the same ``since_tick``; its seconds are
    the last ``idle_seconds`` minus ``IDLE_SECONDS`` (the grace before the
    watchdog speaks), charged to each event's top gate for the time since the
    previous event of the episode.
    """
    episodes: dict[Any, dict] = {}
    for e in events:
        since = e.get("since_tick")
        idle = e.get("idle_seconds")
        if not isinstance(idle, (int, float)):
            continue
        row = episodes.setdefault(
            since, {"since_tick": since, "seconds": 0.0, "gates": Counter(), "last": IDLE_SECONDS, "top": None}
        )
        gate = e.get("top_gate") or "unknown"
        step = max(0.0, float(idle) - row["last"])
        row["gates"][gate] += step
        row["seconds"] += step
        row["last"] = max(row["last"], float(idle))
        row["top"] = row["top"] or e.get("top_item")
    return [
        {"since_tick": r["since_tick"], "seconds": round(r["seconds"]), "gates": dict(r["gates"]), "top_item": r["top"]}
        for r in episodes.values()
        if r["seconds"] > 0
    ]


def run_idle(root) -> dict:
    """Fleet report: idle-with-work seconds of one run and seconds per gate."""
    episodes = idle_episodes(_events(root))
    gates: Counter = Counter()
    for ep in episodes:
        gates.update(ep["gates"])
    return {
        "seconds": round(sum(ep["seconds"] for ep in episodes)),
        "episodes": len(episodes),
        "gates": {g: round(s) for g, s in gates.most_common()},
    }


def format_idle(runs) -> list[str]:
    """``idle with work`` line of the fleet report: total seconds, runs, top gates."""
    rows = [r.get("idle_with_work") for r in runs if isinstance(r.get("idle_with_work"), Mapping)]
    if not rows:
        return []
    gates: Counter = Counter()
    for r in rows:
        gates.update(r.get("gates") or {})
    total = sum(r.get("seconds", 0) for r in rows)
    top = ", ".join(f"{g} {round(s)}s" for g, s in gates.most_common(4)) or "-"
    return [
        f"idle with work: {round(total)}s in {sum(1 for r in rows if r.get('seconds'))}/{len(rows)} runs; top gates: {top}"
    ]


def event_fields(receipt_row: Mapping) -> dict | None:
    """The supervisor's ``idle_with_work`` event (``Supervisor._idle_watch``) from the receipt, or None."""
    watch = receipt_row.get("idle_with_work") if isinstance(receipt_row, Mapping) else None
    if not isinstance(watch, Mapping) or not watch.get("work"):
        return None
    top = watch["work"][0]
    return {
        "idle_seconds": watch.get("idle_seconds"),
        "since_tick": watch.get("since_tick"),
        "top_item": top.get("item"),
        "top_gate": str(top.get("gate") or "").split(":")[0],
        "gates": watch.get("gates"),
        "work": [{k: w.get(k) for k in ("kind", "item", "gate")} for w in watch["work"][:4]],
    }


__all__ = [
    "receipt",
    "sample",
    "window_stats",
    "diagnose",
    "idle_with_work",
    "run_idle",
    "format_idle",
    "event_fields",
]
