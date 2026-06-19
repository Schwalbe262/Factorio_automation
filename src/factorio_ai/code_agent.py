"""FLE-style code-generation control loop (JackHopkins/factorio-learning-environment idea).

Instead of an LLM picking one rigid hand-written skill per cycle, the LLM writes a short PYTHON
PROGRAM each step that drives the factory through a high-level API (``api.move_to``, ``api.build``,
``api.connect``, ``api.craft`` ...). One program performs many operations and can adapt to ANY map
geometry / state, which the deterministic skill+strategy layer cannot (it deadlocks on far-resource
maps; see the autopilot memory). The program runs in a restricted sandbox bound to the live RCON
controller; its printed output + any exception traceback are fed back into the next prompt so the LLM
self-corrects.

This module is ADDITIVE: it does not touch the existing autopilot. Wire it via the
``run-no-mod-code-agent`` CLI subcommand. The LLM completion function is injected so the core
(``FactorioApi`` + sandbox + prompt) is fully unit-testable without a model or a running game.
"""
from __future__ import annotations

import io
import threading
import traceback
from contextlib import redirect_stdout
from typing import Any, Callable

from .models import (
    craftable_count,
    distance,
    entities_named,
    entity_item_count,
    inventory_count,
    nearest_entity,
    player_position,
    total_item_count,
)
from . import knowledge, planner

# Verbs the generated program may call. Mirrors the executor's ALLOWED_ACTION_TYPES but exposed as a
# small, ergonomic Python API so the LLM writes intent ("build a drill on the iron patch"), not raw
# action dicts. Query verbs read the (cached) observation; action verbs go through the controller.
ActFn = Callable[[dict[str, Any]], dict[str, Any]]
ObserveFn = Callable[[], dict[str, Any]]


class ApiError(Exception):
    """Raised inside a generated program when an API verb is misused (bad args)."""


class FactorioApi:
    """High-level, sandbox-facing API the LLM-generated program calls.

    Wraps an ``act(action_dict)->result`` and an ``observe()->obs`` callable (dependency-injected so
    tests can pass fakes). Query verbs use a per-step cached observation; ``refresh()`` re-reads it.
    Action verbs return the executor result dict (``{"ok": bool, ...}``) so the program can branch on
    success/failure and the loop can report what happened.
    """

    def __init__(
        self,
        act: ActFn,
        observe: ObserveFn,
        *,
        log: Callable[[str], None] | None = None,
        run_skill: Callable[[str, int], dict[str, Any]] | None = None,
        skill_names: list[str] | None = None,
        memory: dict[str, Any] | None = None,
    ) -> None:
        self._act = act
        self._observe = observe
        self._log_fn = log or (lambda _m: None)
        self._run_skill_fn = run_skill
        self.skill_names = list(skill_names or [])
        self._obs: dict[str, Any] | None = None
        self.actions_run = 0
        self.logs: list[str] = []
        # Persists ACROSS steps (shared between the loop api and each step's api) so a program can
        # hand state to its successor even though the sandbox env itself resets every step.
        self.memory: dict[str, Any] = memory if memory is not None else {}

    # --- observation / queries -------------------------------------------------
    def observe(self) -> dict[str, Any]:
        """Return the current cached observation (reads once per step unless refresh() is called)."""
        if self._obs is None:
            self._obs = self._observe()
        return self._obs

    def refresh(self) -> dict[str, Any]:
        """Force a fresh observation from the game (use after building to see new entities)."""
        self._obs = self._observe()
        return self._obs

    def position(self) -> dict[str, float]:
        """Agent (character) position {x, y}."""
        return player_position(self.observe())

    def inventory(self, item: str | None = None) -> Any:
        """Whole inventory dict, or the count of one item if ``item`` is given."""
        obs = self.observe()
        if item is not None:
            return inventory_count(obs, item)
        inv = obs.get("inventory")
        return dict(inv) if isinstance(inv, dict) else {}

    def total(self, item: str) -> int:
        """Count of ``item`` everywhere observed (inventory + machine inventories)."""
        return total_item_count(self.observe(), item)

    def entities(self, name: str | None = None) -> list[dict[str, Any]]:
        """All observed force entities, or just those with the given name."""
        obs = self.observe()
        if name is not None:
            return entities_named(obs, name)
        ents = obs.get("entities")
        return [e for e in ents if isinstance(e, dict)] if isinstance(ents, list) else []

    def nearest_resource(self, name: str) -> dict[str, Any] | None:
        """Nearest observed resource tile of ``name`` (e.g. 'iron-ore'), or None. Has .position."""
        return planner.nearest_resource(self.observe(), name)

    def distance_to(self, x: float, y: float) -> float:
        """Distance from the agent to (x, y)."""
        return distance(self.position(), {"x": float(x), "y": float(y)})

    def entity_item_count(self, entity: dict[str, Any], item: str) -> int:
        """Count of ``item`` held inside a machine entity (e.g. furnace output)."""
        return entity_item_count(entity, item)

    def research(self) -> dict[str, Any]:
        """The research sub-observation ({'current': ..., 'technologies': {...}} shape varies)."""
        r = self.observe().get("research")
        return dict(r) if isinstance(r, dict) else {}

    # --- planning queries (recipe DB + footprint math; no game round-trip) ------
    def recipe(self, name: str) -> dict[str, Any] | None:
        """Recipe to PRODUCE ``name``: {name, ingredients{item:amt}, products{item:amt},
        time_seconds, technology} or None if it has no crafting recipe (a raw resource like
        'iron-ore'). ``technology`` is the tech that must be researched first (None if available)."""
        if not name:
            raise ApiError("recipe: name required")
        r = knowledge.recipe_for_product(name)
        if r is None:
            return None
        return {
            "name": r.name, "ingredients": dict(r.ingredients), "products": dict(r.products),
            "time_seconds": r.time_seconds, "technology": r.technology,
        }

    def ingredients_for(self, item: str, count: int = 1) -> dict[str, float]:
        """DIRECT ingredients to craft ``count`` of ``item``: {ingredient: total_amount}. {} if the
        item is raw (no recipe). Accounts for recipe yield (e.g. copper-cable yields 2)."""
        r = self.recipe(item)
        if not r:
            return {}
        n = max(1, int(count))
        yield_n = float(r["products"].get(item, 1) or 1) or 1.0
        crafts = n / yield_n
        return {k: float(v) * crafts for k, v in r["ingredients"].items()}

    def raw_ingredients_for(self, item: str, count: int = 1) -> dict[str, float]:
        """Recursively expand to the RAW base resources needed for ``count`` of ``item`` (full BOM):
        {raw_item: amount}. Use to size how much ore/coal a build will ultimately consume."""
        if not item:
            raise ApiError("raw_ingredients_for: item required")
        return raw_ingredients(item, max(1, int(count)))

    def craftable(self, recipe: str) -> int:
        """How many of ``recipe`` can be hand/virtual-crafted RIGHT NOW from inventory (0 if none)."""
        return craftable_count(self.observe(), recipe)

    def production_chain(self, item: str, max_depth: int = 6) -> list[dict[str, Any]]:
        """Ordered (dependencies first) recipes to build a sub-factory for ``item``: each
        {item, recipe, machine, technology, ingredients}. Raw resources omitted. Plan a whole
        intermediate chain (e.g. for 'automation-science-pack') in one call."""
        if not item:
            raise ApiError("production_chain: item required")
        return production_chain(item, max_depth=int(max_depth))

    def missing_for(self, item: str, count: int = 1) -> dict[str, float]:
        """What you still LACK to craft ``count`` of ``item`` after using current inventory at every
        recipe level: {item: amount}. Empty dict means you can already make it. Drives 'what do I need
        to mine/build next' decisions."""
        if not item:
            raise ApiError("missing_for: item required")
        return missing_to_craft(self.inventory(), item, max(1, int(count)))

    def nearest_entity(self, name: str) -> dict[str, Any] | None:
        """Nearest force entity named ``name`` (e.g. 'stone-furnace'), or None. Has 'position'."""
        return nearest_entity(self.observe(), name)

    def nearest(self, name: str) -> dict[str, Any] | None:
        """Nearest resource tile OR force entity named ``name`` (resource checked first), or None."""
        return self.nearest_resource(name) or self.nearest_entity(name)

    def entities_near(self, x: float, y: float, radius: float = 10.0, name: str | None = None) -> list[dict[str, Any]]:
        """Force entities within ``radius`` tiles of (x, y) (optionally only those named ``name``),
        sorted nearest-first. Use to inspect what is already built around a spot before placing."""
        origin = {"x": float(x), "y": float(y)}
        out = []
        for entity in (self.entities(name) if name else self.entities()):
            pos = entity.get("position") if isinstance(entity.get("position"), dict) else None
            if pos and distance(origin, pos) <= float(radius):
                out.append(entity)
        return sorted(out, key=lambda e: distance(origin, e["position"]))

    def researched(self, name: str) -> bool:
        """Best-effort: True if technology ``name`` looks already researched in the observation."""
        r = self.research()
        techs = r.get("technologies")
        if isinstance(techs, dict):
            tech = techs.get(name)
            if isinstance(tech, dict):
                return bool(tech.get("researched"))
            if isinstance(tech, bool):
                return tech
        done = r.get("researched")
        if isinstance(done, (list, set, tuple)):
            return name in done
        return False

    def remember(self, key: str, value: Any) -> Any:
        """Store ``value`` under ``key`` to reuse in a LATER program. The sandbox namespace resets
        each step, but ``api.memory`` persists across steps, so this is how you carry a plan / chosen
        positions / progress forward. Returns ``value``."""
        self.memory[str(key)] = value
        return value

    def recall(self, key: str, default: Any = None) -> Any:
        """Read a value stored by an earlier program via ``api.remember`` (or ``default``)."""
        return self.memory.get(str(key), default)

    def resource_patch(self, name: str) -> dict[str, Any] | None:
        """The in-view patch of resource ``name``: {name, count, center{x,y}, bounds, width, height,
        nearest{x,y}} or None. Use count/width/height to decide how many drills fit before you must
        haul from a farther patch."""
        return _resource_patch(self.observe(), name)

    def can_place(self, name: str, x: float, y: float) -> bool:
        """Heuristic: True if placing ``name`` at (x, y) is not blocked by an existing entity / tree /
        rock (approximate footprints). The build itself rolls back on real collisions, so treat this
        as a fast pre-check, not a guarantee."""
        if not name:
            raise ApiError("can_place: name required")
        return tile_blocker(self.observe(), float(x), float(y), name) is None

    def find_buildable(self, name: str, near_x: float, near_y: float, radius: int = 8) -> dict[str, float] | None:
        """Nearest free tile to (near_x, near_y) where ``name`` is placeable (outward ring scan,
        heuristic), or None. Use to avoid the overlap bug when exact geometry is not required."""
        if not name:
            raise ApiError("find_buildable: name required")
        return _find_buildable_tile(self.observe(), float(near_x), float(near_y), name, int(radius))

    # --- actions ---------------------------------------------------------------
    def _do(self, action: dict[str, Any]) -> dict[str, Any]:
        self.actions_run += 1
        result = self._act(action)
        return result if isinstance(result, dict) else {"ok": False, "result": result}

    def move_to(self, x: float, y: float) -> dict[str, Any]:
        """Move the agent to (x, y). Instant for the virtual server agent."""
        return self._do({"type": "move_to", "position": {"x": float(x), "y": float(y)}})

    def build(self, name: str, x: float, y: float, direction: int = 0, allow_nearby: bool = False) -> dict[str, Any]:
        """Place entity ``name`` at (x, y). direction in {0:N,2:E,4:S,6:W}. allow_nearby lets the
        game relocate to the nearest buildable tile (use for poles/furnaces, not exact fluid tiles)."""
        if not name:
            raise ApiError("build: name required")
        result = self._do({
            "type": "build", "name": name, "position": {"x": float(x), "y": float(y)},
            "direction": int(direction), "allow_nearby": bool(allow_nearby),
        })
        if isinstance(result, dict):  # expose a position handle so programs can chain off the placed tile
            result.setdefault("x", float(x))
            result.setdefault("y", float(y))
        return result

    def place_next_to(self, name: str, ref: dict[str, Any] | tuple[float, float],
                      side: str, gap: int = 0, direction: int = 0) -> dict[str, Any]:
        """Build ``name`` immediately adjacent to ``ref`` (an entity dict or an (x,y)/{x,y} point) on
        ``side`` ('north'/'south'/'east'/'west'), with optional extra ``gap`` tiles. The offset is
        computed from approximate footprints so you don't do tile math by hand. Returns the build
        result (including the 'x','y' it placed at)."""
        if not name:
            raise ApiError("place_next_to: name required")
        side_key = str(side).lower()
        deltas = {"north": (0.0, -1.0), "south": (0.0, 1.0), "east": (1.0, 0.0), "west": (-1.0, 0.0)}
        if side_key not in deltas:
            raise ApiError("place_next_to: side must be north/south/east/west")
        base = ref.get("position") if isinstance(ref, dict) and isinstance(ref.get("position"), dict) else ref
        bp = _to_pos(base)
        ref_name = ref.get("name", "") if isinstance(ref, dict) else ""
        off = _entity_half_extent(str(ref_name)) + _entity_half_extent(name) + max(0, int(gap))
        dx, dy = deltas[side_key]
        return self.build(name, bp["x"] + dx * off, bp["y"] + dy * off, direction=direction)

    def mine(self, x: float, y: float, count: int = 1) -> dict[str, Any]:
        """Mine/remove whatever is at (x, y) (resource tile or your own placed entity)."""
        return self._do({"type": "mine", "position": {"x": float(x), "y": float(y)}, "count": max(1, int(count))})

    def craft(self, recipe: str, count: int = 1) -> dict[str, Any]:
        """Hand/virtual-craft ``count`` of ``recipe`` (resolves sub-ingredients if possible)."""
        if not recipe:
            raise ApiError("craft: recipe required")
        return self._do({"type": "craft", "recipe": recipe, "count": max(1, int(count))})

    def set_recipe(self, x: float, y: float, recipe: str) -> dict[str, Any]:
        """Set an assembling machine's recipe at (x, y)."""
        if not recipe:
            raise ApiError("set_recipe: recipe required")
        return self._do({"type": "set_recipe", "position": {"x": float(x), "y": float(y)}, "recipe": recipe})

    def insert(self, item: str, count: int, x: float, y: float) -> dict[str, Any]:
        """Insert ``count`` of ``item`` into the entity at (x, y) (e.g. coal into a drill, iron into an assembler)."""
        if not item:
            raise ApiError("insert: item required")
        return self._do({
            "type": "insert", "item": item, "count": max(1, int(count)),
            "position": {"x": float(x), "y": float(y)},
        })

    def take(self, item: str, count: int, x: float, y: float) -> dict[str, Any]:
        """Take ``count`` of ``item`` out of the entity at (x, y) (e.g. plates from a furnace)."""
        if not item:
            raise ApiError("take: item required")
        return self._do({
            "type": "take", "item": item, "count": max(1, int(count)),
            "position": {"x": float(x), "y": float(y)},
        })

    def connect(self, start: tuple[float, float] | dict[str, float],
                end: tuple[float, float] | dict[str, float], name: str = "transport-belt") -> dict[str, Any]:
        """Auto-route a belt/pipe/pole path of ``name`` between two points in ONE call (FLE's
        headline verb). Python computes the tiles; the game places them with rollback on failure."""
        s = _to_pos(start)
        e = _to_pos(end)
        tiles = planner.connect_entities_tiles(self.observe(), s, e, name)
        if not tiles:
            return {"ok": False, "result": "no_route", "name": name}
        return self._do({"type": "connect_entities", "name": name, "tiles": tiles, "allow_existing": True, "skip_blocked": True})

    def research_tech(self, technology: str) -> dict[str, Any]:
        """Queue a technology for research (needs the science packs to actually progress)."""
        if not technology:
            raise ApiError("research_tech: technology required")
        return self._do({"type": "research", "technology": technology})

    def wait(self, ticks: int = 60) -> dict[str, Any]:
        """Let the game run ``ticks`` ticks (production happens in real time)."""
        return self._do({"type": "wait", "ticks": max(1, int(ticks))})

    def produce_until(self, item: str, count: int, *, max_waits: int = 5, ticks: int = 120) -> int:
        """Let the factory run (api.wait in ``ticks`` chunks, up to ``max_waits`` times) until at least
        ``count`` of ``item`` exists anywhere (inventory + machines). Returns the final total. Use right
        after building a production line to confirm it actually produces before moving on."""
        if not item:
            raise ApiError("produce_until: item required")
        have = self.total(item)
        waits = 0
        while have < int(count) and waits < max(1, int(max_waits)):
            self.wait(ticks)
            self.refresh()
            have = self.total(item)
            waits += 1
        return have

    def run_skill(self, name: str, max_steps: int = 15) -> dict[str, Any]:
        """HYBRID verb: run one of the proven deterministic skills for up to ``max_steps`` steps and
        return {'ok','reason','skill'}. Use this for the reliable, well-trodden parts (e.g.
        'produce_iron_plate', 'setup_power', 'bootstrap_build_item_mall') instead of re-deriving them
        in code; write custom api.* code only for what the skills can't handle. ``api.skill_names``
        lists what's available."""
        if not name:
            raise ApiError("run_skill: name required")
        if self._run_skill_fn is None:
            return {"ok": False, "reason": "run_skill is not available in this environment", "skill": name}
        self.actions_run += 1
        result = self._run_skill_fn(name, max(1, int(max_steps)))
        return result if isinstance(result, dict) else {"ok": bool(result), "skill": name}

    def log(self, message: Any) -> None:
        """Report a line back to the agent loop (shown in the next prompt as program output)."""
        text = str(message)
        self.logs.append(text)
        self._log_fn(text)


def _to_pos(value: tuple[float, float] | dict[str, float]) -> dict[str, float]:
    if isinstance(value, dict):
        return {"x": float(value["x"]), "y": float(value["y"])}
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return {"x": float(value[0]), "y": float(value[1])}
    raise ApiError(f"position must be (x, y) or {{'x':..,'y':..}}, got {value!r}")


# Approximate half-footprint (tile radius from center) for occupancy/adjacency math. Default 0.5 (1x1).
# Exact bounding boxes live in the game; these are good-enough heuristics for can_place / placement
# spacing so the LLM avoids the most common overlap bug without doing tile math by hand.
_ENTITY_HALF_EXTENT: dict[str, float] = {
    "transport-belt": 0.5, "fast-transport-belt": 0.5, "express-transport-belt": 0.5,
    "burner-inserter": 0.5, "inserter": 0.5, "fast-inserter": 0.5, "long-handed-inserter": 0.5,
    "small-electric-pole": 0.5, "medium-electric-pole": 0.5, "pipe": 0.5, "pipe-to-ground": 0.5,
    "offshore-pump": 0.5, "wooden-chest": 0.5, "iron-chest": 0.5, "steel-chest": 0.5,
    "stone-furnace": 1.0, "steel-furnace": 1.0, "burner-mining-drill": 1.0, "pumpjack": 1.0,
    "boiler": 1.0, "big-electric-pole": 1.0,
    "electric-furnace": 1.5, "electric-mining-drill": 1.5, "assembling-machine-1": 1.5,
    "assembling-machine-2": 1.5, "assembling-machine-3": 1.5, "lab": 1.5, "steam-engine": 1.5,
    "chemical-plant": 1.5, "oil-refinery": 2.5, "rocket-silo": 4.5,
}
_OBSTACLE_TYPES = {"simple-entity", "tree", "cliff"}


def _entity_half_extent(name: str) -> float:
    return _ENTITY_HALF_EXTENT.get(str(name or ""), 0.5)


def _short_repr(value: Any, limit: int = 60) -> str:
    """Compact one-line repr of a remembered value for the prompt digest."""
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_obstacle(entity: dict[str, Any]) -> bool:
    t = str(entity.get("type") or "")
    n = str(entity.get("name") or "")
    return t in _OBSTACLE_TYPES or n.endswith("rock")


def tile_blocker(observation: dict[str, Any], x: float, y: float, name: str = "") -> dict[str, Any] | None:
    """Return the force-entity / tree / rock whose approximate footprint would overlap placing ``name``
    at (x, y), or None if the tile looks free. Heuristic AABB overlap using ``_entity_half_extent``;
    resource tiles and the character are ignored (you build on ore, and the agent is virtual)."""
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return None
    half = _entity_half_extent(name)
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        ename = str(entity.get("name") or "")
        if ename == "character":
            continue
        pos = entity.get("position") if isinstance(entity.get("position"), dict) else None
        if not pos:
            continue
        other_half = 0.5 if _is_obstacle(entity) else _entity_half_extent(ename)
        gap = half + other_half
        if abs(float(pos.get("x", 0.0)) - float(x)) < gap and abs(float(pos.get("y", 0.0)) - float(y)) < gap:
            return entity
    return None


def _find_buildable_tile(observation: dict[str, Any], x: float, y: float, name: str = "", radius: int = 8) -> dict[str, float] | None:
    """First tile (outward square-ring scan) near (x, y) with no blocker, or None within ``radius``."""
    x0, y0 = round(float(x)), round(float(y))
    if tile_blocker(observation, x0, y0, name) is None:
        return {"x": float(x0), "y": float(y0)}
    for r in range(1, max(1, int(radius)) + 1):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:  # only the new ring at distance r
                    continue
                if tile_blocker(observation, x0 + dx, y0 + dy, name) is None:
                    return {"x": float(x0 + dx), "y": float(y0 + dy)}
    return None


def _resource_patch(observation: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Bounding box, tile count, center and nearest tile of the observed ``name`` resource tiles."""
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return None
    tiles = [r for r in resources
             if isinstance(r, dict) and r.get("name") == name and isinstance(r.get("position"), dict)]
    if not tiles:
        return None
    xs = [float(r["position"]["x"]) for r in tiles]
    ys = [float(r["position"]["y"]) for r in tiles]
    nearest = min(tiles, key=lambda r: float(r.get("distance") or 999999))
    return {
        "name": name,
        "count": len(tiles),
        "center": {"x": sum(xs) / len(xs), "y": sum(ys) / len(ys)},
        "bounds": {"min_x": min(xs), "min_y": min(ys), "max_x": max(xs), "max_y": max(ys)},
        "width": max(xs) - min(xs) + 1.0,
        "height": max(ys) - min(ys) + 1.0,
        "nearest": dict(nearest["position"]),
    }


def raw_ingredients(item: str, count: float = 1, _depth: int = 0, _path: tuple[str, ...] = ()) -> dict[str, float]:
    """Recursively expand ``count`` of ``item`` to a flat map of RAW/base amounts using the recipe DB.
    Items with no recipe (raw resources) are leaves; recipe yield is divided out; cycles and depth > 8
    are treated as leaves so this always terminates."""
    recipe = knowledge.recipe_for_product(item)
    if recipe is None or _depth > 8 or item in _path:
        return {item: float(count)}
    yield_n = float(recipe.products.get(item, 1) or 1) or 1.0
    crafts = float(count) / yield_n
    out: dict[str, float] = {}
    for ingredient, amount in recipe.ingredients.items():
        for k, v in raw_ingredients(ingredient, float(amount) * crafts, _depth + 1, _path + (item,)).items():
            out[k] = out.get(k, 0.0) + v
    return out


def production_chain(item: str, max_depth: int = 6) -> list[dict[str, Any]]:
    """Ordered (DEPENDENCIES FIRST) list of recipes to build a sub-factory for ``item``:
    [{item, recipe, machine, technology, ingredients{item:amt}}]. Raw resources (you mine those) are
    omitted. Each entry's ``machine`` is the crafting category so you know what facility makes it.
    Use to set up assemblers/recipes in the right order for a target like 'automation-science-pack'."""
    order: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(name: str, depth: int, path: tuple[str, ...]) -> None:
        if depth < 0 or name in path:
            return
        recipe = knowledge.recipe_for_product(name)
        if recipe is None:  # raw resource -> not a build step
            return
        for ingredient in recipe.ingredients:
            visit(ingredient, depth - 1, path + (name,))
        if name not in seen:
            seen.add(name)
            order.append({
                "item": name, "recipe": recipe.name,
                "machine": knowledge.recipe_category_for(name),
                "technology": recipe.technology, "ingredients": dict(recipe.ingredients),
            })

    visit(item, max_depth, ())
    return order


def missing_to_craft(inventory: dict[str, Any], item: str, count: float = 1) -> dict[str, float]:
    """What you still LACK to craft ``count`` of ``item`` after consuming ``inventory`` at every level
    of the recipe tree. Returns {item: amount} of shortfalls (raw resources, or items with no recipe).
    A greedy estimate (shared intermediates are allocated first-come), good enough to drive decisions."""
    available = {k: float(v) for k, v in (inventory or {}).items() if isinstance(v, (int, float))}
    missing: dict[str, float] = {}

    def need(name: str, qty: float, depth: int) -> None:
        have = available.get(name, 0.0)
        used = min(have, qty)
        available[name] = have - used
        qty -= used
        if qty <= 1e-9:
            return
        recipe = knowledge.recipe_for_product(name)
        if recipe is None or depth > 8:
            missing[name] = missing.get(name, 0.0) + qty
            return
        yield_n = float(recipe.products.get(name, 1) or 1) or 1.0
        crafts = qty / yield_n
        for ingredient, amount in recipe.ingredients.items():
            need(ingredient, float(amount) * crafts, depth + 1)

    need(item, float(count), 0)
    return missing


# Builtins the sandboxed program may use. No import/open/eval/exec/__import__/getattr-tricks.
# Includes common exception types so generated programs can use try/except defensively.
_SAFE_BUILTINS = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "int",
        "len", "list", "map", "max", "min", "print", "range", "reversed", "round", "set",
        "sorted", "str", "sum", "tuple", "zip", "True", "False", "None",
        "Exception", "ValueError", "KeyError", "IndexError", "TypeError", "RuntimeError",
        "ZeroDivisionError", "AttributeError", "StopIteration", "isinstance",
    )
}


class ProgramResult:
    def __init__(self, ok: bool, output: str, error: str, actions_run: int) -> None:
        self.ok = ok
        self.output = output
        self.error = error
        self.actions_run = actions_run

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "output": self.output, "error": self.error, "actions_run": self.actions_run}


def run_program(program: str, api: FactorioApi, *, timeout_seconds: float = 60.0) -> ProgramResult:
    """Execute an LLM-generated program string in a restricted sandbox with ``api`` bound.

    Returns captured stdout (+ api.log lines) and any exception traceback. The sandbox blocks imports
    and dangerous builtins; the program reaches the game ONLY through ``api``. A watchdog thread bounds
    wall-clock so an accidental infinite loop in the generated code cannot wedge the agent."""
    stdout = io.StringIO()
    env: dict[str, Any] = {
        "__builtins__": dict(_SAFE_BUILTINS),
        "api": api,
        "log": api.log,
    }
    box: dict[str, Any] = {"error": "", "done": False}

    def _run() -> None:
        try:
            with redirect_stdout(stdout):
                exec(compile(program, "<agent_program>", "exec"), env, env)  # noqa: S102 - sandboxed
        except Exception:  # noqa: BLE001
            box["error"] = traceback.format_exc(limit=6)
        finally:
            box["done"] = True

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    captured = stdout.getvalue()
    if api.logs:  # merge api.log(...) lines into the output the LLM sees next step
        log_block = "\n".join(api.logs)
        captured = (captured.rstrip("\n") + "\n" + log_block).lstrip("\n") if captured.strip() else log_block
    if not box["done"]:
        # The program is still running past the deadline; we cannot kill the thread, but the agent
        # must not block forever. Report a timeout; the daemon thread dies with the process.
        return ProgramResult(False, captured, f"program exceeded {timeout_seconds:.0f}s timeout (likely an infinite loop)", api.actions_run)
    return ProgramResult(not box["error"], captured, box["error"], api.actions_run)


def extract_program(text: str) -> str:
    """Pull the Python program out of an LLM completion. Accepts a ```python ...``` fenced block,
    a bare ``` ... ``` block, or falls back to the whole text."""
    if not text:
        return ""
    lowered = text
    for fence in ("```python", "```py", "```"):
        start = lowered.find(fence)
        if start == -1:
            continue
        body_start = start + len(fence)
        end = lowered.find("```", body_start)
        if end != -1:
            return text[body_start:end].strip("\n")
        return text[body_start:].strip("\n")
    return text.strip()


# What each proven deterministic skill does, so the LLM can pick the right one for api.run_skill.
# Only skills actually wired in this environment (api.skill_names) are shown in the prompt.
SKILL_DESCRIPTIONS: dict[str, str] = {
    "produce_iron_plate": "place/relocate a burner drill on the nearest iron patch + a stone furnace, smelt iron plates",
    "produce_copper_plate": "same as iron but on copper ore -> copper plates",
    "setup_coal_supply": "place a burner drill on coal and belt-feed fuel to nearby machines",
    "setup_power": "build offshore-pump + boiler + steam-engine (water power) and wire electric poles",
    "research_automation": "queue the automation tech (needs automation-science-pack / red science)",
    "research_logistics": "queue the logistics tech (belts, inserters, ...)",
    "bootstrap_build_item_mall": "hand-bootstrap assemblers that craft basic items (a small 'mall')",
    "build_gear_belt_mall_logistics": "belt-wire iron plate into an iron-gear-wheel assembler mall",
    "build_iron_plate_logistic_line_to_gear_mall": "run an iron-plate belt line from smelting to the gear mall",
    "expand_iron_smelting": "add more drills/furnaces to raise iron-plate throughput",
    "produce_electronic_circuit": "build copper-cable + electronic-circuit assemblers and wire them",
}


API_REFERENCE = """You drive a Factorio factory by WRITING A PYTHON PROGRAM. An object named `api` is
already provided (do NOT import anything; imports are blocked). Write ONE program inside a ```python
fenced block. It runs in a sandbox; print() and api.log() output is returned to you next step.

QUERIES (read state; no game round-trip, so they are free to call):
  api.position() -> {x,y}              # agent position
  api.inventory(item=None)            # whole dict, or count of one item
  api.total(item) -> int              # count everywhere (inv + machines)
  api.entities(name=None) -> [..]     # force entities; each has 'name','position'{x,y},'status_name','mining_target'
  api.nearest_resource(name) -> e|None  # e.g. 'iron-ore','copper-ore','coal','stone'; e['position']
  api.nearest(name) / api.nearest_entity(name)   # nearest resource-or-entity / nearest built entity
  api.entities_near(x,y,radius=10,name=None) -> [..]  # what is already built around a spot
  api.resource_patch(name) -> {count,center,bounds,width,height,nearest}|None  # patch size (drills that fit)
  api.researched(tech) -> bool        # is a technology already researched
  api.distance_to(x,y) -> float
  api.entity_item_count(entity, item) # items inside a machine (e.g. furnace 'iron-plate')
  api.recipe(name) -> {ingredients,products,time_seconds,technology}|None  # how to craft `name`
  api.ingredients_for(item,count=1) -> {ing:amt}     # DIRECT ingredients
  api.raw_ingredients_for(item,count=1) -> {raw:amt} # full BOM down to ore/coal
  api.production_chain(item) -> [{item,recipe,machine,technology,ingredients}]  # deps-first build order
  api.missing_for(item,count=1) -> {item:amt}        # what you still LACK (after using inventory)
  api.craftable(recipe) -> int        # how many craftable from inventory RIGHT NOW
  api.can_place(name,x,y) -> bool      # heuristic free-tile check (avoid the overlap bug)
  api.find_buildable(name,x,y,radius=8) -> {x,y}|None  # nearest free tile to (x,y)
  api.research() -> {..}              # current research + technologies
  api.refresh()                       # re-read the world after building

ACTIONS (return {'ok':bool, ...}; check .get('ok')):
  api.move_to(x,y)
  api.build(name,x,y,direction=0,allow_nearby=False)   # dir 0=N 2=E 4=S 6=W; allow_nearby for poles/furnaces
                                                        # returns {...,'x','y'} so you can chain off the placed tile
  api.place_next_to(name, ref, side, gap=0)             # ref={x,y} or an entity; side 'north'/'south'/'east'/'west'
  api.mine(x,y,count=1)                                  # remove a tile/your entity (e.g. a drill stranded off ore)
  api.craft(recipe,count=1)
  api.set_recipe(x,y,recipe)
  api.insert(item,count,x,y)                             # e.g. coal into a drill, iron-plate into an assembler
  api.take(item,count,x,y)                               # e.g. iron-plate from a furnace
  api.connect(start,end,name='transport-belt')          # auto-route a belt/pipe/pole path in ONE call; start/end = (x,y)
  api.research_tech(technology)
  api.wait(ticks=60)
  api.produce_until(item,count,max_waits=5,ticks=120) -> int  # run the factory until it makes `count`
  api.run_skill(name, max_steps=15)  # HYBRID: run a PROVEN deterministic skill (see api.skill_names);
                                     # returns {'ok','reason'}. Use for reliable parts; write custom
                                     # api.* code only for what the skills can't do (e.g. odd geometry).
  api.remember(key,value) / api.recall(key,default=None)  # persist a plan/positions ACROSS steps
  api.log(msg)

RULES:
- A burner-mining-drill must sit ON the ore patch (api.nearest_resource); if a drill has mining_target
  None / status 'no_minable_resources' it is stranded -> mine it and rebuild ON the ore.
- Burner drills & stone furnaces consume coal: keep them fueled with api.insert('coal', n, x, y).
- Before building, check api.can_place(name,x,y) (or use api.find_buildable / api.place_next_to) to
  avoid placing entities on top of each other -- overlapping placement is the most common failure.
- Use api.recipe / api.ingredients_for to know what a craft needs, and api.craftable to know if you
  can make it now; if a recipe has a 'technology', research it first.
- The agent is virtual: api.move_to is instant, so hauling far resources is feasible.
- Do a FEW concrete operations per program, print what you did + the resulting state, then stop. You
  get another turn with fresh feedback. Prefer api.connect over many api.build calls for belts/poles."""


PROGRESSION_HINT = """ROUGH PROGRESSION toward a rocket (lean on run_skill for the proven early game,
write custom api.* code for whatever the skills can't place on THIS map's geometry):
  1. Power: api.run_skill('setup_power')                  # water -> boiler -> steam engine
  2. Plates: run_skill('produce_iron_plate'); run_skill('produce_copper_plate')
  3. Fuel:  run_skill('setup_coal_supply')               # keep burners/furnaces fed
  4. Intermediates: run_skill('build_gear_belt_mall_logistics'); run_skill('produce_electronic_circuit')
  5. Science: run_skill('research_automation') then run_skill('research_logistics')
  6. Scale up plates/power, build labs, then advance toward rocket parts.
ALWAYS check what already exists with api.entities(...) before building so you don't duplicate; if a
skill returns ok=False, read its reason and either fix the blocker with custom code or try the next step."""


def build_code_agent_prompt(
    objective: str,
    api: FactorioApi,
    *,
    previous_program: str = "",
    previous_result: ProgramResult | None = None,
) -> str:
    """Build the LLM prompt: goal + API reference + a compact live-state digest + last program feedback."""
    obs = api.observe()
    pos = player_position(obs)
    inv = obs.get("inventory") if isinstance(obs.get("inventory"), dict) else {}
    inv_top = ", ".join(f"{k}:{v}" for k, v in sorted(inv.items(), key=lambda kv: -kv[1])[:12]) or "(empty)"
    ent_counts: dict[str, int] = {}
    for e in api.entities():
        ent_counts[e.get("name")] = ent_counts.get(e.get("name"), 0) + 1
    ent_summary = ", ".join(f"{k}:{v}" for k, v in sorted(ent_counts.items(), key=lambda kv: -kv[1])[:14]) or "(none)"
    res_lines = []
    for r in ("iron-ore", "copper-ore", "coal", "stone"):
        node = api.nearest_resource(r)
        if node:
            rp = node.get("position") or {}
            patch = api.resource_patch(r)
            size = f"[{patch['count']}t]" if patch else ""
            res_lines.append(f"{r}@({rp.get('x')},{rp.get('y')}){size}")
    research = api.research()
    cur = research.get("current") or research.get("current_research") or "none"
    skills_line = ""
    if api.skill_names:
        rows = [f"  {name}: {SKILL_DESCRIPTIONS[name]}" if name in SKILL_DESCRIPTIONS else f"  {name}"
                for name in api.skill_names]
        skills_line = "\nrun_skill skills available (api.run_skill('name')):\n" + "\n".join(rows)
    memory_line = ""
    if api.memory:
        memory_line = "\nremembered (api.recall): " + ", ".join(
            f"{k}={_short_repr(v)}" for k, v in list(api.memory.items())[:10]
        )
    feedback = ""
    if previous_program:
        res = previous_result.as_dict() if previous_result else {}
        feedback = (
            "\n\n--- YOUR PREVIOUS PROGRAM ---\n" + previous_program.strip()
            + "\n--- ITS OUTPUT ---\n" + (str(res.get("output") or "").strip() or "(no output)")
            + ("\n--- ERROR (fix this) ---\n" + str(res.get("error")).strip() if res.get("error") else "")
        )
    return (
        f"GOAL: {objective} (ultimately: launch a rocket).\n\n"
        + API_REFERENCE
        + "\n\n" + PROGRESSION_HINT
        + "\n\n--- CURRENT STATE ---\n"
        + f"tick={obs.get('tick')} agent@({pos.get('x')},{pos.get('y')})\n"
        + f"inventory: {inv_top}\n"
        + f"entities: {ent_summary}\n"
        + f"nearest resources: {', '.join(res_lines) or '(none in view)'}\n"
        + f"current_research: {cur}"
        + skills_line + memory_line + "\n"
        + feedback
        + "\n\nWrite the next Python program (one ```python block)."
    )


CompleteFn = Callable[[str], str]


def run_code_agent_step(
    objective: str,
    api: FactorioApi,
    complete: CompleteFn,
    *,
    previous_program: str = "",
    previous_result: ProgramResult | None = None,
    timeout_seconds: float = 60.0,
) -> tuple[str, ProgramResult]:
    """One agent step: build prompt -> LLM completes -> extract program -> run in sandbox. Returns
    (program, result). ``complete(prompt)->text`` is injected (remote LLM, local endpoint, or a stub)."""
    api.refresh()
    prompt = build_code_agent_prompt(objective, api, previous_program=previous_program, previous_result=previous_result)
    completion = complete(prompt)
    program = extract_program(completion)
    if not program.strip():
        return program, ProgramResult(False, "", "LLM returned no program", api.actions_run)
    # Fresh api per program so actions_run / logs are per-step, but SHARE the memory dict so a
    # program's api.remember(...) is visible to the next step's program.
    step_api = FactorioApi(
        api._act, api._observe, log=api._log_fn,
        run_skill=api._run_skill_fn, skill_names=api.skill_names,
        memory=api.memory,
    )
    result = run_program(program, step_api, timeout_seconds=timeout_seconds)
    return program, result


def remote_program_complete(prompt: str, *, max_tokens: int = 2048, timeout_seconds: int | None = None) -> str:
    """Live completion: offload program generation to the cluster vLLM via the Slurm scheduler
    (same path as the strategy/foundry calls). Returns the program text (empty on failure)."""
    from . import remote_slurm

    res = remote_slurm.request_code_agent_program(prompt, max_tokens=max_tokens, timeout_seconds=timeout_seconds)
    return str(res.get("program") or "") if isinstance(res, dict) else ""


def run_code_agent_loop(
    act: ActFn,
    observe: ObserveFn,
    complete: CompleteFn,
    objective: str = "launch_rocket_program",
    *,
    cycles: int = 0,
    timeout_seconds: float = 60.0,
    on_step: Callable[[int, str, ProgramResult], None] | None = None,
    log: Callable[[str], None] | None = None,
    run_skill: Callable[[str, int], dict[str, Any]] | None = None,
    skill_names: list[str] | None = None,
) -> int:
    """Drive the factory with the FLE code-gen loop until ``cycles`` are done (0 = forever).

    Each iteration: observe -> LLM writes a program -> sandbox-execute it -> feed result into the next
    prompt. ``act``/``observe`` bind to the live controller; ``complete`` is the LLM (injected, so the
    same loop is unit-testable with a stub). ``run_skill``/``skill_names`` enable the HYBRID verb so a
    program can call proven deterministic skills. Never raises out of a step -- a failed step becomes
    feedback for the next program."""
    api = FactorioApi(act, observe, log=log, run_skill=run_skill, skill_names=skill_names)
    prev_program = ""
    prev_result: ProgramResult | None = None
    completed = 0
    while cycles <= 0 or completed < cycles:
        try:
            program, result = run_code_agent_step(
                objective, api, complete,
                previous_program=prev_program, previous_result=prev_result,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - a single bad step must not kill the loop
            program = ""
            result = ProgramResult(False, "", f"step failed: {type(exc).__name__}: {exc}", 0)
        if on_step is not None:
            on_step(completed, program, result)
        prev_program, prev_result = program, result
        completed += 1
    return completed

