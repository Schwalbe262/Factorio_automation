"""Where the next boiler/engine pair goes, derived from the first plant's plan.

The first plant is one ``steam_bank`` template bank (``core/factory_templates``)
turned toward the coast by ``FactoryBuilder._choose_power_plan``. In the
template's canonical frame bank ``i`` has its boiler at ``(8 i, 0.5)`` from the
anchor, its engines toward canonical north, water entering from canonical west
at ``(8 i - 4, 1)`` and the boiler's second water connection free at
``(8 i + 2, 1)``. Rules:

* A new pair is the template's next bank on either end of the row, at the
  plant's own rotation and pitch, so the result is one tidy plant.
* Water is shared by two bridge pipes from one boiler's free water connection
  to the next bank's inlet; the water flows through the boilers.
* A layout is only offered when it overlaps no known hardware, keeps every new
  pipe away from steam/pump fluid boxes and the new boiler's water connection
  reaches the old boiler's water network in a plan audit.

Everything here is pure geometry; the game checks live in ``steam_siting``.
"""

from __future__ import annotations

from collections import deque

from .board import entity
from .core.factory_templates import COAL_ENTRIES, DIRECTIONS, _rotate, build_template, coal_entry_detour
from .site.geometry import plan_rect

PITCH = 8  # steam_bank template spacing between neighbouring banks
BRIDGE = ((2, 1), (3, 1))  # canonical, relative to the western bank's origin
WATER_TILES = ((-2, 0.5), (2, 0.5))  # relative to a boiler centre, before its rotation
PIPES = {"pipe", "pipe-to-ground"}
FLUID_MACHINES = {"boiler", "steam-engine", "offshore-pump"}


def _round(value: float) -> float:
    return round(value, 6)


def world_point(frame: dict, cx: float, cy: float) -> tuple[float, float]:
    """Map position of a canonical template point of ``frame``."""
    rx, ry = _rotate(cx, cy, frame["rotation"])
    return _round(frame["anchor"]["x"] + rx), _round(frame["anchor"]["y"] + ry)


def canonical_point(frame: dict, x: float, y: float) -> tuple[float, float]:
    """Inverse of :func:`world_point`."""
    cx, cy = _rotate(x - frame["anchor"]["x"], y - frame["anchor"]["y"], (16 - frame["rotation"]) % 16)
    return _round(cx), _round(cy)


def plant_frame(power_plan: dict) -> dict | None:
    """Rotation, template anchor and bank indices of the saved first plant.

    Every boiler must share one rotation and sit on the template's bank grid;
    otherwise the plant was not built from ``steam_bank`` and None is returned
    (the caller then needs the free-site fallback or blocks).
    """
    boilers = [e for e in (power_plan or {}).get("entities", []) if e["name"] == "boiler"]
    if not boilers:
        return None
    rotation = boilers[0].get("direction", 0) % 16
    if rotation not in DIRECTIONS:
        return None
    ox, oy = _rotate(0, 0.5, rotation)
    first = boilers[0]["position"]
    frame = {"rotation": rotation, "anchor": {"x": _round(first["x"] - ox), "y": _round(first["y"] - oy)}}
    indices = []
    for boiler in boilers:
        if boiler.get("direction", 0) % 16 != rotation:
            return None
        cx, cy = canonical_point(frame, boiler["position"]["x"], boiler["position"]["y"])
        index = round(cx / PITCH)
        if abs(cx - PITCH * index) > 1e-6 or abs(cy - 0.5) > 1e-6:
            return None
        indices.append(index)
    return dict(frame, indices=sorted(indices))


def bank_at(frame: dict, index: int, entry: str) -> dict:
    """One ``steam_bank`` bank at bank ``index`` of ``frame`` with coal stub ``entry``."""
    x, y = world_point(frame, PITCH * index, 0)
    return build_template("steam_bank", anchor={"x": x, "y": y}, rotation=frame["rotation"], coal_entry=entry)


def bridge_pipes(frame: dict, west_index: int) -> list[dict]:
    """Pipes joining bank ``west_index``'s free water tile to the next bank's inlet."""
    return [entity("pipe", *world_point(frame, PITCH * west_index + dx, dy)) for dx, dy in BRIDGE]


def tiles(entities) -> set[tuple[int, int]]:
    """Integer map tiles covered by plan rows."""
    found = set()
    for e in entities:
        found.update(plan_rect(e).tiles())
    return found


def _tile(x: float, y: float) -> tuple[int, int]:
    return int(x // 1), int(y // 1)


def water_tiles(boiler: dict) -> set[tuple[int, int]]:
    """The two tiles where a pipe joins ``boiler``'s water box."""
    x, y = boiler["position"]["x"], boiler["position"]["y"]
    result = set()
    for dx, dy in WATER_TILES:
        rx, ry = _rotate(dx, dy, boiler.get("direction", 0) % 16)
        result.add(_tile(x + rx, y + ry))
    return result


def _key(e: dict) -> tuple:
    return e["name"], e["position"]["x"], e["position"]["y"]


def overlaps(new: list[dict], existing: list[dict]) -> bool:
    """True when a new row covers a tile of known hardware (identical pipes are shared)."""
    shared = {_key(e) for e in existing if e["name"] in PIPES}
    fresh = [e for e in new if _key(e) not in shared]
    return bool(tiles(fresh) & tiles(existing))


def fluid_conflicts(new: list[dict], existing: list[dict]) -> list[dict]:
    """New pipe/fluid-box contacts that could join water to steam or a pump.

    A pipe may touch a boiler only on that boiler's water tiles and may never
    touch a steam engine or an offshore pump. Contacts between two existing
    rows are the old plant's own business and are ignored.
    """
    fresh = {_key(e) for e in new}
    rows = list({_key(e): e for e in existing + new}.values())
    pipes = [e for e in rows if e["name"] in PIPES]
    machines = [e for e in rows if e["name"] in FLUID_MACHINES]
    conflicts = []
    for pipe in pipes:
        (tile,) = tiles([pipe])
        around = {(tile[0] + dx, tile[1] + dy) for dx, dy in DIRECTIONS.values()}
        for machine in machines:
            if _key(pipe) not in fresh and _key(machine) not in fresh:
                continue
            if not around & tiles([machine]):
                continue
            if machine["name"] == "boiler" and tile in water_tiles(machine):
                continue
            conflicts.append({"pipe": pipe["position"], "machine": machine["name"], "position": machine["position"]})
    return conflicts


def _pipe_links(pipes: dict, pairs) -> dict:
    """Undirected fluid links between pipe tiles (surface pipes and paired mouths)."""
    links = {t: set() for t in pipes}
    for t, row in pipes.items():
        if row["name"] == "pipe-to-ground":
            dx, dy = DIRECTIONS[row.get("direction", 0) % 16]
            ends = [(t[0] + dx, t[1] + dy)]
        else:
            ends = [(t[0] + dx, t[1] + dy) for dx, dy in DIRECTIONS.values()]
        for other in ends:
            if other in links:
                links[t].add(other)
    # A link holds only when both sides open toward each other.
    for t in links:
        links[t] = {o for o in links[t] if t in links[o]}
    for pair in pairs or ():
        a, b = _tile(*pair["first"]), _tile(*pair["last"])
        if a in links and b in links:
            links[a].add(b)
            links[b].add(a)
    return links


def water_network(entities: list[dict], boiler: dict, pairs=()) -> tuple[set, set]:
    """Pipe tiles and boilers on ``boiler``'s water network in a plan.

    The search walks pipe links and passes through any boiler whose water
    tiles it reaches (boilers pass water from one side to the other).
    """
    pipes = {next(iter(tiles([e]))): e for e in entities if e["name"] in PIPES}
    links = _pipe_links(pipes, pairs)
    boilers = [e for e in entities if e["name"] == "boiler"]
    seen_boilers = {_key(boiler)}
    queue = deque(t for t in water_tiles(boiler) if t in pipes)
    seen = set(queue)
    while queue:
        t = queue.popleft()
        reached = list(links[t])
        for other in boilers:
            if _key(other) not in seen_boilers and t in water_tiles(other):
                seen_boilers.add(_key(other))
                reached += [w for w in water_tiles(other) if w in pipes]
        for other in reached:
            if other not in seen:
                seen.add(other)
                queue.append(other)
    return seen, seen_boilers


def water_connected(entities: list[dict], old_boiler: dict, new_boiler: dict, pairs=()) -> bool:
    """Plan audit: ``new_boiler``'s water box is on ``old_boiler``'s water network."""
    return _key(new_boiler) in water_network(entities, old_boiler, pairs)[1]


def foreign_pipe_contacts(new: list[dict], existing: list[dict], old_boiler: dict) -> list[dict]:
    """Existing pipes a new pipe would join that are not on the old plant's water network."""
    own, _ = water_network(existing, old_boiler)
    fresh = [e for e in new if e["name"] in PIPES]
    others = {next(iter(tiles([e]))): e for e in existing if e["name"] in PIPES}
    contacts = []
    for pipe in fresh:
        (tile,) = tiles([pipe])
        for dx, dy in DIRECTIONS.values():
            near = (tile[0] + dx, tile[1] + dy)
            if near in others and near not in own:
                contacts.append({"pipe": pipe["position"], "foreign": others[near]["position"]})
    return contacts


def reserved_area(entities: list[dict]) -> list[list[int]]:
    """Whole-tile bounding box ``[[x0, y0], [x1, y1]]`` of plan rows."""
    covered = tiles(entities)
    xs = [t[0] for t in covered]
    ys = [t[1] for t in covered]
    return [[min(xs), min(ys)], [max(xs) + 1, max(ys) + 1]]


def variant(bank: dict, extra: list[dict], old_boiler: dict, existing: list[dict], pairs=()) -> dict | None:
    """Audit one candidate bank plus its water link; None when it is not valid."""
    if not bank.get("ok"):
        return None
    known = {_key(e) for e in existing if e["name"] in PIPES}
    rows = []
    seen = set(known)
    for e in bank["entities"] + extra:
        # Shared pipes stay with their owner; a repeated tile keeps the template row.
        if _key(e) not in seen:
            seen.add(_key(e))
            rows.append(e)
    if overlaps(rows, existing) or fluid_conflicts(rows, existing):
        return None
    if foreign_pipe_contacts(rows, existing, old_boiler):
        return None
    boiler = next(e for e in bank["entities"] if e["name"] == "boiler")
    if not water_connected(existing + rows, old_boiler, boiler, pairs):
        return None
    coal = next(p for p in bank["ports"] if p["item"] == "coal")
    return {"entry": bank["coal_entry"], "entities": rows, "bank": bank, "boiler": boiler, "coal_port": coal}


def adjacent_layouts(frame: dict, power_plan: dict, existing: list[dict]) -> list[dict]:
    """Next-bank slots on both ends of the row, each with its valid coal stubs.

    ``existing`` is every known planned row (first plant, its coal feed,
    registered blocks). A slot whose every stub variant fails the audit is
    dropped; the caller checks the survivors against the live map.
    """
    boilers = {
        round(canonical_point(frame, **e["position"])[0] / PITCH): e
        for e in power_plan["entities"]
        if e["name"] == "boiler"
    }
    layouts = []
    for side, index, west in (
        (1, frame["indices"][-1] + 1, frame["indices"][-1]),
        (-1, frame["indices"][0] - 1, frame["indices"][0] - 1),
    ):
        old = boilers[index - side]
        pipes = bridge_pipes(frame, west)
        variants = []
        for entry in COAL_ENTRIES:
            bank = dict(bank_at(frame, index, entry), coal_entry=entry)
            found = variant(bank, pipes, old, existing)
            if found:
                variants.append(found)
        if variants:
            layouts.append(
                {
                    "kind": "adjacent_bank",
                    "side": side,
                    "index": index,
                    "old_boiler": old,
                    "rotation": frame["rotation"],
                    "variants": variants,
                }
            )
    return layouts


def rank_variants(variants: list[dict], origin: dict | None) -> list[dict]:
    """Coal stubs ordered so the port faces ``origin`` (the coal source), as the first plant does."""
    if origin is None:
        return list(variants)
    return sorted(variants, key=lambda v: coal_entry_detour(v["coal_port"], origin))
