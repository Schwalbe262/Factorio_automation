"""Bounded, ordinary-stock construction collection for the v2 executor.

The observation chooses candidates; a single native operation rechecks their
identities and live normal-quality stocks.  No source ingredients, fuel,
science inventory, raw ore or mining output are eligible for this fast path.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_bootstrap import DeterministicBootstrap


MAX_SOURCES = 16
MAX_ITEMS = 50
OUTPUT_NAMES = frozenset(
    {
        "stone-furnace",
        "steel-furnace",
        "electric-furnace",
        "assembling-machine-1",
        "assembling-machine-2",
        "assembling-machine-3",
    }
)
CHEST_NAMES = frozenset({"wooden-chest", "iron-chest", "steel-chest"})
CONSTRUCTION_ITEMS = frozenset(
    {
        "iron-plate",
        "copper-plate",
        "steel-plate",
        "stone-brick",
        "iron-gear-wheel",
        "electronic-circuit",
        "advanced-circuit",
        "processing-unit",
        "copper-cable",
        "transport-belt",
        "fast-transport-belt",
        "express-transport-belt",
        "underground-belt",
        "fast-underground-belt",
        "express-underground-belt",
        "splitter",
        "fast-splitter",
        "express-splitter",
        "inserter",
        "fast-inserter",
        "long-handed-inserter",
        "bulk-inserter",
        "stack-inserter",
        "burner-inserter",
        "gun-turret",
        "pipe",
        "pipe-to-ground",
    }
)
# Fuel a hand-fuel trip may take off a proven coal mining bank's belts (``V2Bootstrap.trunk_fuel``).
BELT_FUELS = frozenset({"coal"})
AMMO_ITEMS = frozenset(
    {
        "artillery-shell",
        "atomic-bomb",
        "cannon-shell",
        "capture-robot-rocket",
        "explosive-cannon-shell",
        "explosive-rocket",
        "explosive-uranium-cannon-shell",
        "firearm-magazine",
        "flamethrower-ammo",
        "piercing-rounds-magazine",
        "piercing-shotgun-shell",
        "railgun-ammo",
        "rocket",
        "shotgun-shell",
        "tesla-ammo",
        "uranium-cannon-shell",
        "uranium-rounds-magazine",
    }
)


def _position(value):
    return (
        isinstance(value, dict)
        and set(value) == {"x", "y"}
        and all(type(value[k]) in (int, float) and math.isfinite(value[k]) for k in ("x", "y"))
    )


def validate_take_many(action):
    """Reject the entire malformed envelope before any native I/O."""
    if (
        not isinstance(action, dict)
        or set(action) - {"type", "item", "count", "world_id", "sources", "reason"}
        or action.get("type") != "take_many"
        or not isinstance(action.get("item"), str)
        or action["item"] not in CONSTRUCTION_ITEMS | BELT_FUELS
        or type(action.get("count")) is not int
        or not 1 <= action["count"] <= MAX_ITEMS
        or not isinstance(action.get("world_id"), str)
        or not action["world_id"]
        or not isinstance(action.get("sources"), list)
        or not 1 <= len(action["sources"]) <= MAX_SOURCES
        or ("reason" in action and not isinstance(action["reason"], str))
    ):
        raise ValueError("take_many requires a constructed item, global count 1..50, world and 1..16 sources")
    units = set()
    positions = set()
    for row in action["sources"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "position", "unit_number", "kind"}
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("kind"), str)
            or type(row.get("unit_number")) is not int
            or row["unit_number"] < 1
            or not _position(row.get("position"))
        ):
            raise ValueError("take_many sources require exact name, finite position, unit identity and stock kind")
        if not (
            (row["kind"] == "output" and row["name"] in OUTPUT_NAMES)
            or (row["kind"] == "chest" and row["name"] in CHEST_NAMES)
            or (row["kind"] == "belt" and row["name"] == "transport-belt")
        ):
            raise ValueError("take_many accepts only machine outputs, chests and ordinary transport belts")
        if action["item"] in BELT_FUELS and row["kind"] not in ("belt", "chest"):
            raise ValueError("take_many takes fuel only from mine trunk belts and coal chests")
        position = row["position"]["x"], row["position"]["y"]
        if row["unit_number"] in units or position in positions:
            raise ValueError("take_many sources must have distinct unit identities and positions")
        units.add(row["unit_number"])
        positions.add(position)


BURNERS = frozenset({"burner-mining-drill", "stone-furnace"})
FUELS = ("coal", "wood")
FUEL_BATCH = 8  # coal pieces worth one fetch-and-fuel trip
# Coal put into one burner per visit and fetched per trip. With 8 per visit,
# 16 burner cells turned 43% of all actions into small coal and plate moves
# (seed 2099054105: 119 coal inserts, 110 plate takes of 10 in 28 min).
FUEL_FILL = 20  # cap; the amount follows coal production (haul_batch)
FUEL_FETCH = 48  # cap for one coal fetch
# Trunk fuel (``V2Bootstrap.trunk_fuel``): once the coal patch has a proven electric bank, its
# collector and trunk belts are the hand-fuel coal source, one take_many of a full fetch. Fleet
# aud-1114-0841: after minute 18 the courier took 8 coal per trip from the starter coal chests
# (whatever had piled up) and put 8 into one burner, about a third of the actor's actions up to
# the bus. A tile counts only while it holds this much coal (a backed-up or saturated belt), so a
# trip never empties a trunk that is short.
TRUNK_FUEL_TILE_MIN = 6
COLLECT_BATCH = 10  # construction items worth one collection trip
# A block's machine holding this many of the block's own output-port item cannot move it out (its
# output belt is backed up): that pile is surplus a construction trip may take (eval-1575138435,
# 375 min: two steel furnaces held 163 steel behind a full output belt that the engine row's route
# also owned, so the engine row's steel chest (8) and the pumpjacks (5 each) waited for ever).
SURPLUS_OUTPUT_STOCK = 20
# Hauling adapts to production: one trip carries about HAUL_MINUTES of the
# item's measured output, between a floor and a cap. Early (15 plates/min) a
# trip is small; with more cells it grows instead of multiplying trips.
HAUL_MINUTES = 0.5
PLATE_BATCH = 50  # cap for one plate trip (MAX_ITEMS)
PLATES = frozenset({"iron-plate", "copper-plate"})
# A transfer of this many items or fewer is a "tiny" one (``tools/waste_report.py`` action_loop,
# count<=2). Fleet aud-1114-0930 (12 seeds, first 32 min): 266 of 726 supply-cell iron feeds,
# 286 of 305 wood feeds and 97 of 102 startup science inserts were tiny, typically a 1-plate
# take followed by a 1-plate feed, 91 times in a row. A trip that goes anyway takes what has piled
# up there (up to its batch) and a feed carries the trip's surplus, so the next visits fold in.
SMALL_TRANSFER = 2
RATE_WINDOW_TICKS = 3600  # production rate is measured over the last game minute or more


def production_rate(samples: dict, observation, item: str) -> float | None:
    """Items/minute of ``item`` from the observation's cumulative ``production`` counter.

    ``samples`` (owned by the caller) keeps one older reading per item; the
    rate is the growth since that reading once it is RATE_WINDOW_TICKS old,
    else the lifetime average. None without a counter or a tick.
    """
    row = (observation.get("production") or {}).get(item)
    tick = observation.get("tick")
    if not isinstance(row, dict) or not isinstance(tick, (int, float)) or tick <= 0:
        return None
    produced = row.get("produced", 0)
    old = samples.get(item)
    if old is None or tick < old[0]:
        samples[item] = old = (tick, produced, None)
    if tick - old[0] >= RATE_WINDOW_TICKS:
        rate = (produced - old[1]) * 3600.0 / (tick - old[0])
        samples[item] = (tick, produced, rate)
        return rate
    return old[2] if old[2] is not None else produced * 3600.0 / tick


def haul_batch(rate: float | None, floor: int, cap: int) -> int:
    """Items worth one trip: HAUL_MINUTES of production, clamped to ``floor``..``cap``."""
    if rate is None:
        return floor
    return max(floor, min(cap, int(rate * HAUL_MINUTES)))


FUEL_SKIP_STOCK = 50  # finished items in a cell that make more fuel pointless
# Hand-craft cap: when more than this many finished items of one kind are
# missing and a verified, producing ``produce`` row makes that item
# (``machine_stock.MachineStock``), ``ensure_item`` collects the row's stock
# (batched ``take_many``) or waits for it instead of queueing a large hand
# craft. At or below the cap, or when the row is building, stalled or broken,
# or no row exists, hand crafting proceeds as before (never a deadlock); a
# fallback above the cap names the row state in the craft reason. The temporary
# supply cell (``supply_cell.SupplyCell.stock``) answers the same hook, adds a
# time rule (``_waiting_pays``) and asks to be fed when it has no plates.
HANDCRAFT_LIMIT = 10


def _waiting_pays(machine, missing):
    """Time rule of the supply cell (``supply_cell.stock``): wait when the machines need at most
    ``wait_factor`` x the hand time for what the chest does not hold; a source without the
    figures (``produce`` rows) always waits."""
    if "wait_seconds_per_unit" not in machine:
        return True
    wait = max(0, missing - machine.get("stock", 0)) * machine["wait_seconds_per_unit"]
    return wait <= machine["wait_factor"] * missing * machine["hand_seconds_per_unit"]


def _entity_key(entity):
    position = entity.get("position") or {}
    return (entity.get("name"), position.get("x"), position.get("y"))


def _produced_coal(observation, coal_cells):
    """Coal lying in the receivers (chests) of the given coal cells."""
    places = {
        (
            c["receiver"].get("name"),
            (c["receiver"].get("position") or {}).get("x"),
            (c["receiver"].get("position") or {}).get("y"),
        )
        for c in coal_cells
    }
    return sum(
        (e.get("inventory") or {}).get("coal", 0) for e in observation.get("entities", []) if _entity_key(e) in places
    )


BUS_FED_PHASES = frozenset({"commission", "verified"})  # planned_bus phases after the feed splice


def bus_phase(root: Path, world_id) -> str | None:
    """Phase of the planned main bus of this world (``planned-main-bus.json``), or None."""
    from .planned_bus import STATE_FILE

    path = Path(root) / STATE_FILE
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("world_id") != world_id:
        return None
    phase = state.get("phase")
    return phase if isinstance(phase, str) else None


def bus_fed_plates(bus, phase) -> set[str]:
    """Plates the published bus trunks take from their district ports once spliced (``BUS_FED_PHASES``)."""
    if phase not in BUS_FED_PHASES or not isinstance(bus, dict):
        return set()
    return {t.get("item") for t in bus.get("trunks") or () if isinstance(t, dict) and isinstance(t.get("item"), str)}


class V2Bootstrap(DeterministicBootstrap):
    def _unreserved_stock_observation(self, observation, item):
        self._collection_points = {}
        root = getattr(getattr(getattr(self, "game", None), "cfg", None), "runtime_dir", None)
        if not isinstance(root, (str, Path)):
            return observation
        path = Path(root) / "pcb-board.json"
        if not path.exists():
            return observation
        world = observation.get("world_id")
        fingerprint = getattr(self.catalog, "fingerprint", None)
        raw = path.read_bytes()
        phase = bus_phase(Path(root), world)
        version = (world, fingerprint, hashlib.sha256(raw).digest(), phase)
        cached = getattr(self, "_stock_ownership_cache", None)
        if cached is None or cached[0] != version:
            board = json.loads(raw.decode("utf-8"))
            if board.get("world_id") != world or board.get("catalog_fingerprint") != fingerprint:
                raise ValueError("construction stock ownership world or catalog changed")
            blocks = board.get("blocks", {})
            if not isinstance(blocks, dict):
                raise ValueError("invalid registered construction stock ownership")
            groups = list(blocks.items())
            if board.get("shared_main_bus"):
                groups.append(("shared_main_bus", board["shared_main_bus"]))
            ownership = {}
            owners = {}
            surplus = {}  # machine key -> the owning blocks' output-port items (SURPLUS_OUTPUT_STOCK)
            # Once the bus feed is spliced (phase commission/verified) the district's plate ports
            # feed the trunks: their plates belong to the bus, never to a construction trip
            # (eval-4177156614: 42 takes at the iron port kept the lane tail empty for 10 min).
            fed = bus_fed_plates(board.get("shared_main_bus"), phase)
            for name, block in groups:
                for entity in block.get("entities", []):
                    p = entity.get("position")
                    if not _position(p):
                        raise ValueError("invalid registered stock position")
                    key = (entity["name"], p["x"], p["y"])
                    # Finite startup batches own their whole cell, including
                    # intermediates and the output used by their own courier.
                    public = {
                        port["item"]
                        for port in block.get("ports", [])
                        if name != "startup_science" and port.get("direction") == "output" and port.get("position") == p
                    } - fed
                    ownership[key] = public if key not in ownership else ownership[key] & public
                    owners.setdefault(key, set()).add(name)
                    if entity["name"] in OUTPUT_NAMES and name != "startup_science":
                        surplus.setdefault(key, set()).update(
                            port["item"]
                            for port in block.get("ports", [])
                            if port.get("direction") == "output" and isinstance(port.get("item"), str)
                        )
            # Surplus trunk tiles past the last tap (starter_retirement.collection_points)
            # are the construction plate stock once the bus exists.
            from .starter_retirement import collection_points

            # Only after the bus proved plates on every lane tail: before that, plates taken
            # off the trunk are plates that never reach the tail the proof waits for.
            points = (
                {
                    key: plate
                    for key, plate in collection_points(board.get("shared_main_bus")).items()
                    if owners.get(key) == {"shared_main_bus"}
                }
                if phase == "verified"
                else {}
            )
            for key, plate in points.items():
                ownership[key] = ownership[key] | {plate}
            self._stock_ownership_cache = (version, ownership, points, surplus)
        ownership = self._stock_ownership_cache[1]
        self._collection_points = self._stock_ownership_cache[2]
        surplus = self._stock_ownership_cache[3]

        def available(entity):
            p = entity.get("position")
            if not _position(p):
                return True  # ordinary candidate validation still rejects malformed rows
            key = (entity.get("name"), p["x"], p["y"])
            allowed = ownership.get(key)
            if allowed is None or item in allowed:
                return True
            held = (entity.get("output_inventory") or {}).get(item, 0)
            return item in surplus.get(key, ()) and isinstance(held, (int, float)) and held >= SURPLUS_OUTPUT_STOCK

        return dict(observation, entities=[e for e in observation.get("entities", []) if available(e)])

    def _trunk_fuel_tiles(self, observation) -> set:
        """Tiles of the transport belts of every proven coal mining bank of this world (plan + traced trunk)."""
        root = getattr(getattr(getattr(self, "game", None), "cfg", None), "runtime_dir", None)
        if not isinstance(root, (str, Path)):
            return set()
        path = Path(root) / "mining-outposts.json"
        try:
            stamp = (observation.get("world_id"), path.stat().st_mtime_ns, path.stat().st_size)
        except OSError:
            return set()
        cached = getattr(self, "_trunk_fuel_cache", None)
        if cached is None or cached[0] != stamp:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            banks = []
            if isinstance(state, dict) and state.get("world_id") == observation.get("world_id"):
                banks = [
                    b
                    for b in state.get("banks") or ()
                    if isinstance(b, dict) and b.get("ready") and (b.get("plan") or {}).get("resource") == "coal"
                ]
            cached = self._trunk_fuel_cache = (stamp, banks)
        tiles = set()
        if not cached[1]:
            return tiles
        from .mine_trunk import trace_trunk

        for bank in cached[1]:
            plan = bank.get("plan") or {}
            for e in plan.get("entities") or ():
                if e.get("name") == "transport-belt" and _position(e.get("position")):
                    tiles.add((e["position"]["x"], e["position"]["y"]))
            for port in (plan.get("ports") or ())[:1]:
                if port.get("item") == "coal" and port.get("direction") == "output":
                    for e in trace_trunk(observation, port):
                        if e.get("name") == "transport-belt":
                            tiles.add((e["position"]["x"], e["position"]["y"]))
        return tiles

    def _trunk_fuel_sources(self, observation) -> list[tuple[int, dict]]:
        """``(coal, source)`` of trunk belts holding at least ``TRUNK_FUEL_TILE_MIN`` coal, fullest first."""
        tiles = self._trunk_fuel_tiles(observation)
        if not tiles:
            return []
        rows = []
        for e in observation.get("entities", []):
            if e.get("name") != "transport-belt" or not _position(e.get("position")):
                continue
            unit, p = e.get("unit_number"), e["position"]
            if type(unit) is not int or unit < 1 or (p["x"], p["y"]) not in tiles:
                continue
            stock = (e.get("belt_inventory") or {}).get("coal", 0)
            if type(stock) in (int, float) and stock >= TRUNK_FUEL_TILE_MIN:
                rows.append((int(stock), {"name": e["name"], "position": dict(p), "unit_number": unit, "kind": "belt"}))
        rows.sort(key=lambda r: (-r[0], r[1]["unit_number"]))
        return rows[:MAX_SOURCES]

    def trunk_coal(self, observation) -> int:
        """Coal a trunk fuel take could collect now (``TRUNK_FUEL_TILE_MIN`` tiles only)."""
        return sum(n for n, _ in self._trunk_fuel_sources(observation))

    def trunk_fuel(self, observation, item, count) -> dict | None:
        """One ``take_many`` of hand-fuel coal off the proven coal bank's belts, or None.

        Only when the belts hold at least ``min(count, FUEL_BATCH)``: never a smaller trip.
        """
        if item not in BELT_FUELS or type(count) is not int or count < 1:
            return None
        world = observation.get("world_id")
        if not isinstance(world, str) or not world:
            return None
        rows = self._trunk_fuel_sources(observation)
        on_hand = sum(n for n, _ in rows)
        if not rows or on_hand < min(count, FUEL_BATCH):
            return None
        return {
            "type": "take_many",
            "item": item,
            "count": min(count, MAX_ITEMS, on_hand),
            "world_id": world,
            "sources": [source for _, source in rows],
            "reason": f"collect hand-fuel {item} from the coal patch trunk",
        }

    def chest_fuel(self, observation, item, count) -> dict | None:
        """One ``take_many`` of coal from every chest holding it (the starter coal cells), or None.

        The ordinary take empties one chest; fleet aud-1114-0930 (12 seeds, first 32 min) took 1373
        times about 11 coal from one starter coal chest while the neighbouring chests held as much,
        and each fetch fed one burner (1394 fuel inserts of 10.5 coal). Two or more chests only.
        ``observation`` is the unreserved one (block-owned chests are already left out).
        """
        if item not in BELT_FUELS or type(count) is not int or count < 1:
            return None
        world = observation.get("world_id")
        if not isinstance(world, str) or not world:
            return None
        rows = []
        for e in observation.get("entities", []):
            unit, p = e.get("unit_number"), e.get("position")
            if e.get("name") not in CHEST_NAMES or type(unit) is not int or unit < 1 or not _position(p):
                continue
            stock = (e.get("inventory") or {}).get(item, 0)
            if type(stock) in (int, float) and math.isfinite(stock) and stock >= 1:
                rows.append(
                    (int(stock), {"name": e["name"], "position": dict(p), "unit_number": unit, "kind": "chest"})
                )
        if len(rows) < 2:
            return None
        rows.sort(key=lambda r: (-r[0], r[1]["unit_number"]))
        rows = rows[:MAX_SOURCES]
        return {
            "type": "take_many",
            "item": item,
            "count": min(count, MAX_ITEMS, sum(n for n, _ in rows)),
            "world_id": world,
            "sources": [source for _, source in rows],
            "reason": f"collect hand-fuel {item} from the coal cells' chests",
        }

    def production_rate(self, observation, item):
        """Recent items/minute of ``item`` (see :func:`production_rate`)."""
        samples = self.__dict__.setdefault("_rate_samples", {})
        return production_rate(samples, observation, item)

    def maintain_fuel(self, observation):
        """Hand-fuel burners only where it pays, and only in batches.

        Rules, in order:
        - A burner whose output is backed up, or whose cell already holds
          FUEL_SKIP_STOCK finished items, gets no fuel: burning coal to make
          plates nobody is taking starves the coal drills and the boiler.
        - Only a burner that is out of fuel (or about to be) is urgent.
        - Coal producers are fuelled before coal consumers.
        - With no coal in hand, wait until FUEL_BATCH coal has piled up at a
          running coal drill instead of fetching it one piece at a time. The
          primary job keeps working meanwhile.
        """
        skipped = None
        retirement = getattr(self, "retirement", None)  # starter_retirement.StarterRetirement
        retired = retirement.cell_keys(observation) if retirement is not None else set()
        if retired:
            # Cells of a resource with a permanent source are left to cool: no fuel, ever.
            observation = dict(
                observation, entities=[e for e in observation.get("entities", []) if _entity_key(e) not in retired]
            )

        def urgent(entity):
            if entity.get("name") not in BURNERS:
                return True
            if entity.get("status_name") in {"full_output", "waiting_for_space", "waiting_for_space_in_destination"}:
                return False
            fuel = entity.get("inventory") or {}
            if fuel.get("coal", 0) + fuel.get("wood", 0) > 0:
                return False
            if entity.get("remaining_burning_fuel", 0) > 1_000_000:
                return False
            return _entity_key(entity) not in skipped

        def hungry(rows):
            return any(
                e.get("name") in BURNERS and sum((e.get("inventory") or {}).get(item, 0) for item in FUELS) < 2
                for e in rows
            )

        skipped = set()
        if not hungry([e for e in observation.get("entities", []) if urgent(e)]):
            return None
        skipped = self._well_stocked_burners(observation)
        entities = [e for e in observation.get("entities", []) if urgent(e)]
        if not hungry(entities):
            return None
        observation = dict(observation, entities=entities)
        survey = self._existing_cells("coal")
        if not survey.get("ok"):
            return {"status": "blocked", "reason": "starter_coal_priority_survey_failed"}
        coal_cells = [
            c
            for c in survey.get("cells", [])
            if not c.get("electric")
            and c.get("receiver")
            and c.get("remaining", 0) > 0
            and _entity_key(c["drill"]) not in retired
        ]
        producers = {(c["drill"]["name"], c["drill"]["position"]["x"], c["drill"]["position"]["y"]) for c in coal_cells}
        carried = self._count(observation, "coal")
        if carried < FUEL_BATCH and any(c.get("fuel", 0) > 0 or c.get("burning") for c in coal_cells):
            # Less than a batch in hand while coal is still being mined: top up
            # in one trip once a batch has piled up, otherwise leave the few
            # pieces alone. Spending them one at a time is the loop to avoid.
            if _produced_coal(observation, coal_cells) < FUEL_BATCH and self.trunk_coal(observation) < FUEL_BATCH:
                return None
            rate = self.production_rate(observation, "coal")
            return self.ensure_item(observation, "coal", carried + haul_batch(rate, 3 * FUEL_BATCH, FUEL_FETCH))
        # Keep a real coal producer running before spending the last carried
        # fuel on its consumers. The base routine retains ordinary partial
        # transfers and its existing bounded emergency mining behavior.
        ordered = sorted(entities, key=lambda e: _entity_key(e) not in producers)
        action = super().maintain_fuel(dict(observation, entities=ordered))
        if isinstance(action, dict) and action.get("type") == "insert" and action.get("item") == "coal":
            rate = self.production_rate(observation, "coal")
            action["count"] = max(action["count"], min(haul_batch(rate, FUEL_BATCH, FUEL_FILL), carried))
        return action

    def _well_stocked_burners(self, observation):
        """Drills and furnaces of non-coal cells that already hold enough finished output."""
        by_place = {
            (e.get("name"), (e.get("position") or {}).get("x"), (e.get("position") or {}).get("y")): e
            for e in observation.get("entities", [])
        }
        stocked = set()
        for e in observation.get("entities", []):
            if e.get("name") == "stone-furnace" and sum((e.get("output_inventory") or {}).values()) >= FUEL_SKIP_STOCK:
                stocked.add(_entity_key(e))
        for resource in observation.get("resources") or {}:
            if resource in FUELS:
                continue
            survey = self._existing_cells(resource)
            if not survey.get("ok"):
                continue
            for cell in survey.get("cells", []):
                receiver = cell.get("receiver") or {}
                place = receiver.get("position") or {}
                held = by_place.get((receiver.get("name"), place.get("x"), place.get("y")))
                if held is None:
                    continue
                finished = held.get("output_inventory")
                if finished is None:
                    finished = {k: v for k, v in (held.get("inventory") or {}).items() if k not in FUELS}
                if sum(finished.values()) >= FUEL_SKIP_STOCK:
                    drill = cell["drill"]
                    stocked.add((drill["name"], drill["position"]["x"], drill["position"]["y"]))
        return stocked

    def _ensure_cell(self, observation, resource, receiver_name):
        retirement = getattr(self, "retirement", None)
        if retirement is not None and resource in retirement.resources(observation):
            return None  # a permanent source replaced these cells: never rebuild one
        result = super()._ensure_cell(observation, resource, receiver_name)
        cleanup = getattr(self, "cell_site_cleanup", None)
        if result and result.get("type") == "build" and cleanup is not None:
            recovery = cleanup(observation, {"entities": []}, result)
            if recovery is not None:
                return recovery
        return result

    def ensure_item(self, observation, item, count, _stack=()):
        source = getattr(self, "construction_stock_source", None)
        if source is not None and self._count(observation, item) < count:
            supplied = source(observation, item, count)
            if supplied is not None:
                if not isinstance(supplied, dict) or not (
                    supplied.get("type") == "take_mall_stock"
                    or (not supplied.get("type") and supplied.get("status") in {"waiting", "blocked"})
                ):
                    raise ValueError("construction stock provider requires guarded mall collection or an explicit wait")
                if supplied.get("type") == "take_mall_stock" and (
                    supplied.get("item") != item
                    or type(supplied.get("count")) is not int
                    or not 1 <= supplied["count"] <= min(50, count - self._count(observation, item))
                ):
                    raise ValueError("mall collection must match the requested missing stock")
                return supplied
        missing = count - self._count(observation, item)
        hook = getattr(self, "machine_stock", None)
        machine = hook(observation, item) if hook is not None and missing > 0 else None
        if (
            machine is not None
            and machine.get("status") == "starved"
            and machine.get("feed")
            and missing > HANDCRAFT_LIMIT
        ):
            return machine["feed"]  # supply cell without plates: feed it rather than hand craft
        if machine is not None and machine.get("status") == "producing":
            collect = self._take_output(observation, item, missing)
            if collect is not None:
                return collect
            if missing > HANDCRAFT_LIMIT and _waiting_pays(machine, missing):
                return {
                    "status": "waiting",
                    "reason": f"waiting for machine-made {item}",
                    "evidence": {"item": item, "missing": missing, "limit": HANDCRAFT_LIMIT, "machine": machine},
                }
        intermediates = getattr(self, "cell_intermediates", None)  # supply_cell.SupplyCell.intermediate_take
        taken = intermediates(observation, item, missing) if intermediates is not None and missing > 0 else None
        if taken is not None and self._take_output(observation, item, missing) is None:  # finished stock first
            return taken  # stocked gears/circuits first: the craft queue only does the final assembly
        result = super().ensure_item(observation, item, count, _stack)
        if (
            machine is not None
            and machine.get("status") != "producing"
            and missing > HANDCRAFT_LIMIT
            and isinstance(result, dict)
            and result.get("type") == "craft"
        ):
            note = f"; machine-made {item} {machine.get('status')}: hand-craft fallback"
            result = dict(result, reason=(str(result.get("reason") or "") + note)[:300])
        return result

    def _fuel_is_piling_up(self, item):
        """True while some drill is still filling a chest with this fuel.

        Waiting for a batch only makes sense if the batch can arrive. When
        every chest-feeding drill is out of fuel, the few pieces left are the
        only way to restart one; on a live map all eleven burners sat idle
        beside a chest of five coal. A survey failure counts as not piling up.
        """
        survey = self._existing_cells(item)
        if not isinstance(survey, dict) or not survey.get("ok"):
            return False
        return any(
            cell.get("receiver") and (cell.get("fuel", 0) > 0 or cell.get("burning") or cell.get("operating"))
            for cell in survey.get("cells", [])
        )

    def _take_output(self, observation, item, count):
        catalog_item = getattr(self.catalog, "items", {}).get(item, {})
        if item in AMMO_ITEMS or catalog_item.get("type") == "ammo":
            # Generic item-count transfers erase remaining-round metadata. Let
            # ensure_item use ordinary engine crafting for fresh full magazines.
            # Even a machine output could contain a manually stored used stack.
            return None
        whole = observation  # trunk fuel reads the mining bank's own belts, which block ownership hides
        observation = self._unreserved_stock_observation(observation, item)
        if item not in CONSTRUCTION_ITEMS:
            depot = getattr(self, "fuel_depot", None)  # carried_surplus.CarriedSurplus.depot_take
            stored = depot(observation, item, count) if depot is not None and item in FUELS else None
            if stored is not None:
                return stored  # surplus parked in a depot is used before any other chest
            action = super()._take_output(observation, item, count)
            trunk = self.trunk_fuel(whole, item, count) if item in BELT_FUELS else None
            if trunk is not None and (action is None or action.get("count", 0) < min(count, trunk["count"])):
                return trunk  # the permanent coal source in one full fetch, not a chest's few pieces
            chests = self.chest_fuel(observation, item, count) if item in BELT_FUELS else None
            if chests is not None and (action is None or action.get("count", 0) < chests["count"]):
                action = chests  # every coal chest in one fetch, not one chest's few pieces
            if (
                item in FUELS
                and action is not None
                and type(action.get("count")) is int
                and action["count"] < min(count, FUEL_BATCH)
                and self._fuel_is_piling_up(item)
            ):
                # Any job that needs fuel waits for a batch instead of taking
                # single pieces; callers then report the ordinary supply wait.
                return None
            return action
        if type(count) is not int or count < 1:
            return None
        world = observation.get("world_id")
        if not isinstance(world, str) or not world:
            return None
        buffer = self.construction_buffers.get(item) or {}
        points = {k for k, plate in (getattr(self, "_collection_points", None) or {}).items() if plate == item}
        candidates = []
        for e in observation.get("entities", []):
            name = e.get("name")
            if name in OUTPUT_NAMES:
                kind, inventory = "output", e.get("output_inventory") or {}
            elif name in CHEST_NAMES:
                kind, inventory = "chest", e.get("inventory") or {}
            elif name == "transport-belt":
                kind, inventory = "belt", e.get("belt_inventory") or {}
            else:
                continue
            unit, position = e.get("unit_number"), e.get("position")
            if type(unit) is not int or unit < 1 or not _position(position):
                continue
            stock = inventory.get(item, 0)
            if type(stock) not in (int, float) or not math.isfinite(stock) or stock < 0:
                continue
            verified = (
                kind == "chest"
                and buffer.get("world_id") == world
                and buffer.get("unit_number") == unit
                and buffer.get("name") == name
                and buffer.get("position") == position
            )
            # Include a verified buffer even when its snapshot was empty: it may
            # have filled, and an empty live buffer never stops the rest of the sweep.
            if stock > 0 or verified:
                candidates.append(
                    (
                        not verified,
                        (name, position["x"], position["y"]) not in points,  # the permanent supply first
                        -stock,
                        unit,
                        {"name": name, "position": dict(position), "unit_number": unit, "kind": kind},
                    )
                )
        candidates.sort(key=lambda row: row[:4])
        sources = []
        units, positions = set(), set()
        for _, _, _, unit, source in candidates:
            position = source["position"]["x"], source["position"]["y"]
            if unit in units or position in positions:
                continue
            sources.append(source)
            units.add(unit)
            positions.add(position)
            if len(sources) == MAX_SOURCES:
                break
        if not sources:
            return None
        # Fetching each plate as it leaves the furnace costs one action per
        # plate. Let a batch pile up unless less than a batch is needed or a
        # registered buffer chest (whose snapshot may be stale) is involved.
        on_hand = sum(-row[2] for row in candidates)
        batch = COLLECT_BATCH
        if item in PLATES:
            batch = haul_batch(self.production_rate(observation, item), COLLECT_BATCH, PLATE_BATCH)
        # A plate trip is never tiny (SMALL_TRANSFER): with plates being made it waits for a few more.
        least = max(count, SMALL_TRANSFER + 1) if item in PLATES else count
        if on_hand < min(least, batch) and all(row[0] for row in candidates):
            return None
        if any(not row[1] for row in candidates):
            count = max(count, batch)  # surplus on the permanent supply: one full trip, not just the shortfall
        elif item in PLATES:
            # The trip goes anyway: take what has piled up at the furnaces, up to one batch, so
            # the next small requests are served from the hands (aud-1114-0930: 2088 iron takes
            # in 12 runs, runs of 1-plate takes each followed by a 1-plate supply-cell feed).
            count = max(count, min(on_hand, batch))
        return {
            "type": "take_many",
            "item": item,
            "count": min(count, MAX_ITEMS),
            "world_id": world,
            "sources": sources,
            "reason": f"collect live construction stocks {item}",
        }


TAKE_MANY_LUA = r"""
if not d or d.world_id~=x.world_id then return failure("take_many_world_changed") end
if not a or not a.valid or a.force~=f then return failure("agent_dead_or_force_changed") end
local inv=a.get_main_inventory()
if not inv or not inv.valid then return failure("character_main_inventory_missing") end
if not prototypes.item[x.item] then return failure("take_many_item_missing") end
local id={name=x.item,quality="normal"}
local total=0;local rows={}
for _,spec in ipairs(x.sources) do
 if total>=x.count or inv.get_insertable_count(id)<1 then break end
 local row={name=spec.name,unit_number=spec.unit_number,position=spec.position,moved=0}
 rows[#rows+1]=row
 local e=target(spec.position,spec.name)
 if not e or not e.valid or e.unit_number~=spec.unit_number or e.force~=f or e.surface~=s
    or e.position.x~=spec.position.x or e.position.y~=spec.position.y then
  row.status="skipped";row.reason="source_identity_changed"
 elseif character_backend and not a.can_reach_entity(e) then
  row.status="skipped";row.reason="out_of_reach"
 else
  local source=nil;local first=nil;local second=nil;local available=0
  if spec.kind=="belt" and e.type=="transport-belt" then
   first=e.get_transport_line(1);second=e.get_transport_line(2)
   available=first.get_item_count(id)+second.get_item_count(id)
  elseif spec.kind=="output" and (e.type=="furnace" or e.type=="assembling-machine") then
   source=e.get_output_inventory()
   if source then available=source.get_item_count(id) end
  elseif spec.kind=="chest" and e.type=="container" then
   source=e.get_inventory(defines.inventory.chest)
   if source then available=source.get_item_count(id) end
  end
  row.available=available
  local n=math.min(x.count-total,inv.get_insertable_count(id),available)
  if n<1 then row.status="skipped";row.reason="no_normal_stock_or_capacity"
  else
   local inserted=inv.insert{name=x.item,quality="normal",count=n}
   local removed=0
   if inserted>0 and first then
    removed=first.remove_item{name=x.item,quality="normal",count=inserted}
    if removed<inserted then removed=removed+second.remove_item{name=x.item,quality="normal",count=inserted-removed} end
   elseif inserted>0 and source then removed=source.remove{name=x.item,quality="normal",count=inserted} end
   if removed<inserted then
    local rolled=inv.remove{name=x.item,quality="normal",count=inserted-removed}
    if rolled~=inserted-removed then return failure("take_many_conservation_rollback_failed") end
   end
   row.moved=removed;row.status=removed>0 and "succeeded" or "skipped"
   total=total+removed
  end
 end
end
return success{status=total>0 and "succeeded" or "waiting",moved=total,requested=x.count,sources=rows}
"""


def execute_take_many(game, action):
    """One native query; no retry after an uncertain mutation or transport error."""
    validate_take_many(action)
    expected = getattr(game, "expected_world", None)
    if expected is not None and expected != action["world_id"]:
        raise ValueError("take_many must target the bound world")
    backend = getattr(game, "backend", "assisted")
    if backend not in ("assisted", "character"):
        raise ValueError("take_many requires an ordinary supported backend")
    encoded = json.dumps(json.dumps(action, separators=(",", ":")))
    receipt = game.query(
        "local x=helpers.json_to_table("
        + encoded
        + ");local character_backend="
        + ("true" if backend == "character" else "false")
        + ";"
        + TAKE_MANY_LUA
    )
    if not receipt.get("ok"):
        return receipt
    rows = receipt.get("sources")
    if rows == {}:  # Factorio encodes an empty Lua array as an object.
        rows = []
        receipt = dict(receipt, sources=rows)
    moved = receipt.get("moved")
    allowed = {s["unit_number"] for s in action["sources"]}
    if (
        type(moved) is not int
        or not 0 <= moved <= action["count"]
        or not isinstance(rows, list)
        or len(rows) > len(allowed)
        or any(
            not isinstance(row, dict)
            or row.get("unit_number") not in allowed
            or type(row.get("moved")) is not int
            or row["moved"] < 0
            for row in rows
        )
        or len({row["unit_number"] for row in rows}) != len(rows)
        or sum(row["moved"] for row in rows) != moved
        or receipt.get("status") != ("succeeded" if moved else "waiting")
    ):
        # The operation may have happened. Never conceal or automatically retry
        # an inconsistent/unknown result; the job owner must reconcile inventory.
        raise RuntimeError("take_many receipt violates aggregate conservation; reconcile before retry")
    return receipt
