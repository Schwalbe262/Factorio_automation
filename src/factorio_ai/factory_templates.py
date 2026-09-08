"""Pure, reusable factory geometry and bounded tile routing.

Positions use the centre of a tile as the local origin. The default world anchor
is (0.5, 0.5); even-sized machines therefore receive a half-tile local offset.
Fluid geometry must describe the *exterior pipe tile*, relative to the machine
centre, for every recipe fluid. Geometry is a construction plan, never proof
that a running factory has enough materials, electricity, or throughput.
"""

from __future__ import annotations

from collections import Counter
import heapq
import math
from typing import Any, Iterable, Mapping


DIRECTIONS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
FLUID_ITEMS = {"water", "steam", "crude-oil", "heavy-oil", "light-oil",
               "petroleum-gas", "sulfuric-acid", "lubricant"}


def _point(value: Any) -> tuple[float, float]:
    x, y = (value["x"], value["y"]) if isinstance(value, Mapping) else value
    x, y = float(x), float(y)
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("positions must be finite")
    return x, y


def _position(x: float, y: float) -> dict[str, float]:
    return {"x": round(x, 6), "y": round(y, 6)}


def _rotate(x: float, y: float, direction: int) -> tuple[float, float]:
    for _ in range(direction // 4):
        x, y = -y, x
    return x, y


def _failure(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "entities": [], "ports": [],
            "required_items": {}, "bounds": {}}


def _entity(name: str, x: float, y: float, *, direction: int = 0,
            width: int = 1, height: int = 1, recipe: str | None = None,
            fluid: str | None = None) -> dict[str, Any]:
    result = {"name": name, "position": _position(x, y), "direction": direction,
              "_width": width, "_height": height}
    if recipe:
        result["recipe"] = recipe
    if fluid:
        result["_fluid"] = fluid
    return result


def _port(kind: str, item: str, direction: str, x: float, y: float,
          facing: int | None = None, **extra: Any) -> dict[str, Any]:
    result = {"kind": kind, "item": item, "direction": direction,
              "position": _position(x, y), **extra}
    if facing is not None:
        result["facing"] = facing
    return result


def _rect(entity: dict[str, Any]) -> tuple[float, float, float, float]:
    x, y = _point(entity["position"])
    w, h = entity.get("_width", 1), entity.get("_height", 1)
    if entity.get("direction", 0) in (4, 12):
        w, h = h, w
    return x - w / 2, y - h / 2, x + w / 2, y + h / 2


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ax, ay, ar, ab = _rect(a)
    bx, by, br, bb = _rect(b)
    return min(ar, br) - max(ax, bx) > 1e-6 and min(ab, bb) - max(ay, by) > 1e-6


def _free(entities: list[dict[str, Any]], candidate: dict[str, Any]) -> bool:
    return not any(_overlap(candidate, existing) for existing in entities)


def _item_connection(entities: list[dict[str, Any]], ports: list[dict[str, Any]],
                     item: str, direction: str, slot: tuple[float, float, int],
                     machine_index: int, inserter: str = "inserter") -> bool:
    x, y, outward = slot
    dx, dy = DIRECTIONS[outward]
    facing = outward if direction == "input" else (outward + 8) % 16
    belt_direction = (outward + 8) % 16 if direction == "input" else outward
    additions = [_entity(inserter, x, y, direction=facing)]
    additions.extend(_entity("transport-belt", x + dx * n, y + dy * n,
                             direction=belt_direction) for n in (1, 2))
    if any(not _free(entities, e) for e in additions):
        return False
    entities.extend(additions)
    ports.append(_port("item", item, direction, x + 2 * dx, y + 2 * dy,
                       belt_direction, machine_index=machine_index))
    return True


def _solid_row(template: str, recipe: str | None, machine: str, count: int,
               inputs: list[str], output: str | None,
               geometry: dict[str, Any] | None) -> tuple[list, list]:
    if template == "furnace_row":
        if machine not in {"stone-furnace", "steel-furnace"}:
            raise ValueError("furnace_row supports the 2x2 stone/steel furnace geometry")
        if len(inputs) != 2 or "coal" not in inputs or not output:
            raise ValueError("furnace_row requires one smelting ingredient, coal, and output")
        entities, ports = [], []
        for i in range(count):
            x = i * 6
            entities.append(_entity(machine, x + .5, .5, width=2, height=2))
            for k, item in enumerate(inputs):
                _item_connection(entities, ports, item, "input", (x + k, -1, 0), i)
            _item_connection(entities, ports, output, "output", (x, 2, 8), i)
            for y in (-1, 2):
                entities.append(_entity("small-electric-pole", x + 2, y))
            ports.append(_port("power", "electricity", "input", x + 2, -1))
        return entities, ports

    if template == "labs_row":
        if machine != "lab" or not inputs or len(inputs) > 6 or output:
            raise ValueError("labs_row requires lab and one to six science inputs, no output")
        recipe = None
    elif not recipe or not output or len(inputs) > 3:
        raise ValueError("assembler_row requires recipe, output, and at most three inputs")
    if any(item in FLUID_ITEMS for item in inputs + ([output] if output else [])):
        raise ValueError("solid template cannot accept fluids; use a fluid template")
    if machine not in {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3", "lab"}:
        raise ValueError("unsupported solid machine geometry")
    entities, ports = [], []
    for i in range(count):
        x = i * 11
        entities.append(_entity(machine, x, 0, width=3, height=3, recipe=recipe))
        slots = [(x, -2, 0), (x - 2, 0, 12), (x, 2, 8)]
        if template == "labs_row":
            slots = [(x + dx, -2, 0) for dx in (-1, 0, 1)]
            slots += [(x - 2, dy, 12) for dy in (-1, 0, 1)]
        for item, slot in zip(inputs, slots):
            if not _item_connection(entities, ports, item, "input", slot, i):
                raise ValueError("item input corridor blocked")
        if output and not _item_connection(entities, ports, output, "output", (x + 2, 0, 4), i):
            raise ValueError("item output corridor blocked")
        _corner_poles(entities, ports, x, 3, 3)
    return entities, ports


def _corner_poles(entities: list[dict[str, Any]], ports: list[dict[str, Any]],
                  x: float, width: int, height: int) -> None:
    for dx in (-(width + 1) / 2, (width + 1) / 2):
        for y in (-(height + 1) / 2, (height + 1) / 2):
            pole = _entity("small-electric-pole", x + dx, y)
            if not _free(entities, pole):
                raise ValueError("fluid/item geometry occupies reserved power pole position")
            entities.append(pole)
    ports.append(_port("power", "electricity", "input", x - (width + 1) / 2,
                       -(height + 1) / 2))


def _fluid_row(recipe: str | None, machine: str, count: int, inputs: list[str],
               output: str | None, geometry: dict[str, Any] | None) -> tuple[list, list]:
    if not recipe or not geometry or not geometry.get("fluid_ports"):
        raise ValueError("fluid templates require recipe and explicit prototype fluid ports")
    width, height = geometry.get("width"), geometry.get("height")
    if width not in (3, 5) or height not in (3, 5):
        raise ValueError("fluid templates support explicit 3/5-tile odd machine dimensions")
    raw_ports = geometry["fluid_ports"]
    if not isinstance(raw_ports, list):
        raise ValueError("fluid_ports must be a list")
    declared = {(p.get("item"), p.get("direction")) for p in raw_ports if isinstance(p, dict)}
    required = {(item, "input") for item in inputs if item in FLUID_ITEMS}
    required |= {(item, "input") for item in geometry.get("fluid_inputs", [])}
    required |= {(item, "output") for item in geometry.get("fluid_outputs", [])}
    if output in FLUID_ITEMS:
        required.add((output, "output"))
    if not required.issubset(declared):
        raise ValueError("missing recipe fluid port")
    if any(not isinstance(p, dict) or not p.get("item") or p.get("direction") not in {"input", "output"}
           for p in raw_ports):
        raise ValueError("each fluid port requires item and input/output direction")
    fluid_inputs = {item for item, direction in declared if direction == "input"}
    fluid_outputs = {item for item, direction in declared if direction == "output"}
    solid_inputs = [item for item in inputs if item not in fluid_inputs]
    solid_output = output if output not in fluid_outputs else None
    entities, ports = [], []
    for i in range(count):
        cx = i * (width + 8)
        entities.append(_entity(machine, cx, 0, width=width, height=height, recipe=recipe))
        for p in raw_ports:
            px, py = _point(p["position"])
            if abs(px) == (width + 1) / 2 and abs(py) <= (height - 1) / 2:
                outward = 4 if px > 0 else 12
            elif abs(py) == (height + 1) / 2 and abs(px) <= (width - 1) / 2:
                outward = 8 if py > 0 else 0
            else:
                raise ValueError("fluid port must be an exterior pipe tile on the machine edge")
            if px != int(px) or py != int(py):
                raise ValueError("fluid pipe tiles must align with the machine tile grid")
            dx, dy = DIRECTIONS[outward]
            for n in range(3):
                pipe = _entity("pipe", cx + px + n * dx, py + n * dy, fluid=p["item"])
                if not _free(entities, pipe):
                    raise ValueError("fluid port corridors overlap")
                entities.append(pipe)
            ports.append(_port("fluid", p["item"], p["direction"], cx + px + 2 * dx,
                               py + 2 * dy, outward if p["direction"] == "output" else (outward + 8) % 16,
                               machine_index=i))
        # Input inserters use the edges first, keeping the central fluid ports clear.
        edge_x, edge_y = (width - 1) // 2, (height - 1) // 2
        slots = [(cx + x, -(height + 1) / 2, 0) for x in (-edge_x, edge_x, 0)]
        slots += [(cx - (width + 1) / 2, y, 12) for y in (-edge_y, edge_y, 0)]
        slots += [(cx + x, (height + 1) / 2, 8) for x in (-edge_x, edge_x, 0)]
        for item in solid_inputs:
            if not any(_item_connection(entities, ports, item, "input", slot, i) for slot in slots):
                raise ValueError("no clear solid input slot beside fluid ports")
        if solid_output:
            output_slots = [(cx + (width + 1) / 2, y, 4) for y in (-edge_y, edge_y, 0)]
            if not any(_item_connection(entities, ports, solid_output, "output", slot, i) for slot in output_slots):
                raise ValueError("no clear solid output slot beside fluid ports")
        _corner_poles(entities, ports, cx, width, height)
    # Pipes carrying unlike fluids must never touch, even if entities do not overlap.
    pipes = [e for e in entities if e["name"] == "pipe"]
    for i, pipe in enumerate(pipes):
        x, y = _point(pipe["position"])
        for other in pipes[i + 1:]:
            ox, oy = _point(other["position"])
            if pipe["_fluid"] != other["_fluid"] and abs(x - ox) + abs(y - oy) == 1:
                raise ValueError("different fluid corridors would connect")
    return entities, ports


def _steam_bank(count: int) -> tuple[list, list]:
    # Installed base prototype: north boiler water ports (+/-1,+.5), steam
    # port (0,-.5); 3x5 engines have north/south ports at y=+/-2.
    entities, ports = [], []
    for i in range(count):
        x = i * 8
        entities.append(_entity("boiler", x, .5, width=3, height=2))
        for y in (-3, -8):
            entities.append(_entity("steam-engine", x, y, width=3, height=5))
        for px in (-4, -3, -2):
            entities.append(_entity("pipe", x + px, 1, fluid="water"))
        for bx in range(-4, 1):
            entities.append(_entity("transport-belt", x + bx, 3, direction=4))
        entities.append(_entity("burner-inserter", x, 2, direction=8))
        for y in (0, -4, -8):
            entities.append(_entity("small-electric-pole", x + 2, y))
        ports.extend([_port("fluid", "water", "input", x - 4, 1, 4, machine_index=i),
                      _port("item", "coal", "input", x - 4, 3, 4, machine_index=i),
                      _port("power", "electricity", "output", x + 2, 0, machine_index=i)])
    return entities, ports


def build_template(template: str, *, recipe: str | None = None, machine: str | None = None,
                   count: int = 1, inputs: list[str] | None = None, output: str | None = None,
                   prototype_geometry: dict[str, Any] | None = None,
                   anchor: dict[str, float] | None = None, rotation: int = 0) -> dict[str, Any]:
    """Create steam_bank, furnace_row, assembler_row, labs_row, chemical_row,
    or refinery_row. ``count`` counts machines (boiler/engine pairs for steam).

    ``ports[].facing`` is the required belt travel direction at that endpoint.
    Fluid ports use the same convention, with direction describing input/output.
    Successful plans still require terrain checks and observed flow verification.
    """
    try:
        if rotation not in DIRECTIONS or isinstance(rotation, bool):
            raise ValueError("rotation must be a Factorio cardinal direction: 0,4,8,12")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 64:
            raise ValueError("count must be an integer between 1 and 64")
        inputs = list(inputs or [])
        if any(not isinstance(item, str) or not item for item in inputs) or len(set(inputs)) != len(inputs):
            raise ValueError("inputs must contain distinct nonempty item names")
        if template in {"steam_bank", "steam_power"}:
            entities, ports = _steam_bank(count)
        elif template in {"furnace_row", "assembler_row", "labs_row"}:
            defaults = {"furnace_row": "stone-furnace", "assembler_row": "assembling-machine-1", "labs_row": "lab"}
            entities, ports = _solid_row(template, recipe, machine or defaults[template], count,
                                         inputs, output, prototype_geometry)
        elif template in {"chemical_row", "refinery_row", "fluid_machine_row"}:
            default_machine = "oil-refinery" if template == "refinery_row" else "chemical-plant"
            entities, ports = _fluid_row(recipe, machine or default_machine, count, inputs,
                                         output, prototype_geometry)
        else:
            raise ValueError(f"unsupported template: {template}")
        ax, ay = _point(anchor if anchor is not None else {"x": .5, "y": .5})
        for obj in entities + ports:
            x, y = _rotate(*_point(obj["position"]), rotation)
            obj["position"] = _position(ax + x, ay + y)
            if obj in entities:
                obj["direction"] = (obj["direction"] + rotation) % 16
            elif "facing" in obj:
                obj["facing"] = (obj["facing"] + rotation) % 16
        for i, entity in enumerate(entities):
            if any(_overlap(entity, other) for other in entities[i + 1:]):
                raise ValueError("template contains overlapping entities")
        poles = [e for e in entities if e["name"] == "small-electric-pole"]
        electric_names = {"inserter", "lab", "chemical-plant", "oil-refinery", "steam-engine",
                          "assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}
        for entity in entities:
            if entity["name"] not in electric_names:
                continue
            left, top, right, bottom = _rect(entity)
            covered = False
            for pole in poles:
                px, py = _point(pole["position"])
                if min(right, px + 2.5) > max(left, px - 2.5) and min(bottom, py + 2.5) > max(top, py - 2.5):
                    covered = True
                    break
            if not covered:
                raise ValueError(f"template leaves {entity['name']} outside power coverage")
        rects = [_rect(e) for e in entities]
        bounds = {"min_x": min(r[0] for r in rects), "min_y": min(r[1] for r in rects),
                  "max_x": max(r[2] for r in rects), "max_y": max(r[3] for r in rects)}
        bounds.update(width=bounds["max_x"] - bounds["min_x"], height=bounds["max_y"] - bounds["min_y"])
        for e in entities:
            e.pop("_width", None)
            e.pop("_height", None)
            e.pop("_fluid", None)
        return {"ok": True, "reason": "", "template": template, "entities": entities, "ports": ports,
                "required_items": dict(sorted(Counter(e["name"] for e in entities).items())),
                "bounds": bounds, "validation_required": ["placement", "power", "sustained_flow"]}
    except (ValueError, TypeError, KeyError) as exc:
        return _failure(str(exc))


def route_orthogonal(start: Any, end: Any, *, occupied: Iterable[Any] = (),
                     bounds: Mapping[str, float] | None = None, max_nodes: int = 10000,
                     start_direction: int | None = None, end_direction: int | None = None) -> dict[str, Any]:
    """Bounded A* on a tile lattice. Occupied endpoints fail; callers must remove
    only the intended reusable source/destination tiles from occupancy explicitly.
    Start/end directions constrain belt departure/arrival, preventing reversed taps.
    """
    failure = {"ok": False, "path": [], "segments": [], "visited": 0, "reason": ""}
    try:
        sx, sy = _point(start)
        ex, ey = _point(end)
        if any(d is not None and d not in DIRECTIONS for d in (start_direction, end_direction)):
            raise ValueError("route directions must be 0,4,8,12")
        if (sx, sy) == (ex, ey) and start_direction is not None and end_direction is not None and start_direction != end_direction:
            raise ValueError("coincident route endpoints require the same direction")
        if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
            raise ValueError("max_nodes must be a positive integer")
        if abs((ex - sx) - round(ex - sx)) > 1e-6 or abs((ey - sy) - round(ey - sy)) > 1e-6:
            raise ValueError("route endpoints must share the same tile lattice")
        blocked = {_point(p) for p in occupied}
        if (sx, sy) in blocked or (ex, ey) in blocked:
            raise ValueError("route endpoint is occupied")
        limits = dict(bounds or {"min_x": min(sx, ex) - 32, "max_x": max(sx, ex) + 32,
                                "min_y": min(sy, ey) - 32, "max_y": max(sy, ey) + 32})
        def inside(x: float, y: float) -> bool:
            return limits["min_x"] <= x <= limits["max_x"] and limits["min_y"] <= y <= limits["max_y"]
        if not inside(sx, sy) or not inside(ex, ey):
            raise ValueError("route endpoint is outside bounds")
        initial = (sx, sy, -1)
        frontier = [(0.0, 0, initial)]
        costs, parents = {initial: 0}, {}
        visited, sequence, goal = 0, 0, None
        while frontier and visited < max_nodes:
            _, _, node = heapq.heappop(frontier)
            x, y, previous = node
            visited += 1
            if (x, y) == (ex, ey) and (end_direction is None or previous == end_direction or node == initial):
                goal = node
                break
            ancestor_tiles = set()
            ancestor = node
            while True:
                ancestor_tiles.add(ancestor[:2])
                if ancestor == initial:
                    break
                ancestor = parents[ancestor]
            for direction, (dx, dy) in DIRECTIONS.items():
                if node == initial and start_direction is not None and direction != start_direction:
                    continue
                nx, ny = x + dx, y + dy
                if not inside(nx, ny) or (nx, ny) in blocked:
                    continue
                # Direction is part of the search state, but one physical belt
                # tile cannot be visited again with a different direction. In
                # particular, a forced first step must never be undone by a
                # U-turn through the source to escape an obstructed departure.
                if (nx, ny) in ancestor_tiles:
                    continue
                if (nx, ny) == (ex, ey) and end_direction is not None and direction != end_direction:
                    continue
                next_node = (nx, ny, direction)
                cost = costs[node] + 10 + (1 if previous not in (-1, direction) else 0)
                if cost >= costs.get(next_node, math.inf):
                    continue
                costs[next_node], parents[next_node] = cost, node
                sequence += 1
                heuristic = 10 * (abs(nx - ex) + abs(ny - ey))
                heapq.heappush(frontier, (cost + heuristic, sequence, next_node))
        if goal is None:
            failure.update(visited=visited, reason="route search budget exhausted" if frontier else "no route within bounds")
            return failure
        path = []
        while True:
            path.append(_position(goal[0], goal[1]))
            if goal == initial:
                break
            goal = parents[goal]
        path.reverse()
        if len({(p["x"], p["y"]) for p in path}) != len(path):
            failure.update(visited=visited, reason="route revisits a physical tile")
            return failure
        segments = []
        for i, position in enumerate(path):
            if i + 1 < len(path):
                delta = (round(path[i + 1]["x"] - position["x"]), round(path[i + 1]["y"] - position["y"]))
                direction = next(d for d, vec in DIRECTIONS.items() if vec == delta)
            else:
                direction = end_direction if end_direction is not None else (segments[-1]["direction"] if segments else start_direction or 0)
            segments.append({"position": position, "direction": direction})
        return {"ok": True, "reason": "", "path": path, "segments": segments, "visited": visited}
    except (ValueError, TypeError, KeyError) as exc:
        failure["reason"] = str(exc)
        return failure
