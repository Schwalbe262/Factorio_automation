"""One site authority per runtime: the surveyed map and its master plan.

``SiteService(game)`` is shared by every controller of a runtime. On first
use it asks the game for the world id and the player spawn, surveys the
square of half-size ``half_size`` tiles around the spawn, computes the
master plan (:func:`site.masterplan.plan_site`) and caches both in the
runtime directory:

* ``site-map.json``  - ``{"world_id", "map": <WorldMap JSON>}``; the map
  carries the plan's zone reservations and allocation blocks;
* ``site-plan.json`` - ``{"world_id", "start", "half_size", "plan": ...}``.

A cache with a different world id (a new save or another seed) is ignored
and replaced. Every allocation change is persisted at once, so a restart
continues with the same land handed out.

Existing player entities found by the survey are ``entity`` features of the
map; the planner and the allocator treat them as occupied tiles. Call
:meth:`SiteService.refresh` on a rectangle before a decision that depends on
the current entities there (one cheap survey query per 96-tile slice).

Coordinates are map tiles (x east, y south); rects are half-open, see
:mod:`site.geometry`.
"""

from __future__ import annotations

import json
from pathlib import Path

from .geometry import Rect
from .masterplan import MasterPlan, plan_site
from .planmodel import SiteConfig, SiteError
from .planzones import Allocation, Corridor, Zone
from .survey import survey_area, update
from .worldmap import WorldMap

MAP_FILE = "site-map.json"
PLAN_FILE = "site-plan.json"
BOARD_FILE = "pcb-board.json"
# Owner of the planned-tile marks for other blocks' hardware (see SiteService.mark_board_hardware).
BOARD_HARDWARE_OWNER = "board-hardware"
HALF_SIZE = 384
IDENTITY_LUA = (
    "local p=f.get_spawn_position(s) return {ok=true,world_id=d.world_id,x=math.floor(p.x),y=math.floor(p.y)}"
)


class SiteService:
    """Lazily surveyed map + master plan, cached per world id.

    ``surveyor(game, rect) -> WorldMap`` and ``updater(world, game, rect)``
    default to the live bulk survey; tests pass fakes.
    """

    def __init__(
        self, game, *, half_size: int = HALF_SIZE, config: SiteConfig | None = None, surveyor=None, updater=None
    ):
        self.game = game
        self.half_size = half_size
        self.config = config or SiteConfig()
        self.root = Path(game.cfg.runtime_dir)
        self._surveyor = surveyor or (lambda g, rect: survey_area(g, rect).world)
        self._updater = updater or (lambda world, g, rect: update(world, g, rect))
        self._world: WorldMap | None = None
        self._plan: MasterPlan | None = None
        self.world_id = None
        self.surveys = 0

    # ----- loading ---------------------------------------------------------
    def _identity(self) -> tuple[object, tuple[int, int]]:
        reply = self.game.query(IDENTITY_LUA)
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise SiteError(f"site identity query failed: {reply!r}")
        return reply["world_id"], (int(reply["x"]), int(reply["y"]))

    def _read_cache(self, world_id) -> bool:
        map_path = self.root / MAP_FILE
        plan_path = self.root / PLAN_FILE
        if not (map_path.exists() and plan_path.exists()):
            return False
        try:
            stored_map = json.loads(map_path.read_text(encoding="utf-8"))
            stored_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if stored_map.get("world_id") != world_id or stored_plan.get("world_id") != world_id:
            return False
        if stored_plan.get("half_size") != self.half_size:
            return False
        self._world = WorldMap.from_json(json.dumps(stored_map["map"]))
        self._plan = MasterPlan.from_dict(stored_plan["plan"])
        self.world_id = world_id
        return True

    def _build(self, world_id, start: tuple[int, int]) -> None:
        h = self.half_size
        rect = Rect(start[0] - h, start[1] - h, start[0] + h, start[1] + h)
        world = self._surveyor(self.game, rect)
        self.surveys += 1
        self._plan = plan_site(world, start=start, config=self.config)
        self._world = world
        self.world_id = world_id
        self.save()

    def _ensure(self, world_id=None) -> None:
        if self._plan is not None and (world_id is None or world_id == self.world_id):
            return
        if world_id is not None and self._read_cache(world_id):
            return
        current, start = self._identity()
        if world_id is not None and current != world_id:
            raise SiteError(f"game reports world {current!r}, caller expected {world_id!r}")
        if self._read_cache(current):
            return
        self._build(current, start)

    def save(self) -> None:
        """Write both cache files (atomic replace each)."""
        self.root.mkdir(parents=True, exist_ok=True)
        map_payload = {"world_id": self.world_id, "map": json.loads(self._world.to_json())}
        plan_payload = {
            "world_id": self.world_id,
            "start": list(self._plan.start),
            "half_size": self.half_size,
            "plan": self._plan.to_dict(),
        }
        for name, payload in ((MAP_FILE, map_payload), (PLAN_FILE, plan_payload)):
            path = self.root / name
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)

    # ----- reads -------------------------------------------------------------
    def world(self, world_id=None) -> WorldMap:
        """The surveyed map (with reservations) for ``world_id`` (None: the current world)."""
        self._ensure(world_id)
        return self._world

    def plan(self, world_id=None) -> MasterPlan:
        self._ensure(world_id)
        return self._plan

    def zone(self, name: str, world_id=None) -> Zone | None:
        return self.plan(world_id).zone(name)

    def corridor(self, name: str, world_id=None) -> Corridor | None:
        return self.plan(world_id).corridor(name)

    def refresh(self, rect: Rect, world_id=None) -> None:
        """Re-survey ``rect`` (widened to whole chunks); reservations are kept."""
        world = self.world(world_id)
        self._updater(world, self.game, rect)
        self.surveys += 1
        self.save()

    # ----- allocation --------------------------------------------------------
    def mark_board_hardware(self) -> int:
        """Mark other blocks' planned hardware outside their reserved areas as planned map tiles.

        The cached map only knows surveyed entities. A block registered on the
        board (``pcb-board.json``) may still be unbuilt, and its power bridge
        or routed belts leave its reserved area, so neither the survey nor a
        reserved-area check sees them; a new block could then be allocated on
        top of them. :meth:`allocate` and :meth:`allocate_candidates` call
        this first: stale marks (owner :data:`BOARD_HARDWARE_OWNER`) are
        dropped and every such entity of the current board is marked with
        :meth:`WorldMap.apply_plan`; the allocator treats ``plan`` tiles as
        occupied. The marks are removed again right after the allocation, so
        they are never saved and never block :meth:`extend`. A missing,
        unreadable or other-world board marks nothing. Returns the number of
        marked entities.
        """
        from ..board import external_hardware

        world = self._world
        world.clear_plans(BOARD_HARDWARE_OWNER)
        try:
            board = json.loads((self.root / BOARD_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(board, dict) or board.get("world_id") != self.world_id:
            return 0
        blocks = board.get("blocks")
        if not isinstance(blocks, dict):
            return 0
        marked = 0
        for _name, e in external_hardware(blocks):
            try:
                world.apply_plan([e], owner=BOARD_HARDWARE_OWNER)
            except (KeyError, TypeError, ValueError):
                continue
            marked += 1
        return marked

    def allocation(self, name: str, world_id=None) -> Allocation | None:
        return next((a for a in self.plan(world_id).allocations if a.name == name), None)

    def allocate(
        self, kind: str, along: int, depth: int, *, name: str, returns_to_bus: bool = True, world_id=None
    ) -> Allocation:
        """Next free district block (see :meth:`MasterPlan.allocate`); persisted.

        An existing allocation of the same name is returned unchanged, so a
        repeated call after a restart does not hand out a second block.
        """
        existing = self.allocation(name, world_id)
        if existing is not None:
            return existing
        self.mark_board_hardware()
        try:
            allocation = self._plan.allocate(self._world, kind, along, depth, name=name, returns_to_bus=returns_to_bus)
        finally:
            self._world.clear_plans(BOARD_HARDWARE_OWNER)
        self.save()
        return allocation

    def allocate_candidates(
        self, kind: str, along: int, depth: int, *, name: str, count: int, returns_to_bus: bool = True, world_id=None
    ) -> list[Allocation]:
        """Up to ``count`` distinct blocks ``<name>#<i>`` in allocator order.

        Earlier unused candidates of the same name are released first. The
        caller keeps one with :meth:`keep_only` (or releases all).
        """
        self.release_prefix(name + "#", world_id)
        self.mark_board_hardware()
        found = []
        try:
            for index in range(count):
                try:
                    found.append(
                        self._plan.allocate(
                            self._world, kind, along, depth, name=f"{name}#{index}", returns_to_bus=returns_to_bus
                        )
                    )
                except SiteError:
                    break
        finally:
            self._world.clear_plans(BOARD_HARDWARE_OWNER)
        self.save()
        return found

    def keep_only(self, prefix: str, keep: str | None, world_id=None) -> None:
        """Release every allocation starting with ``prefix`` except ``keep``."""
        self.release_prefix(prefix, world_id, keep=keep)

    def release_prefix(self, prefix: str, world_id=None, keep: str | None = None) -> int:
        plan = self.plan(world_id)
        names = [a.name for a in plan.allocations if a.name.startswith(prefix) and a.name != keep]
        for name in names:
            plan.release(self._world, name)
        if names:
            self.save()
        return len(names)

    def release(self, name: str, world_id=None) -> Allocation:
        allocation = self.plan(world_id).release(self._world, name)
        self.save()
        return allocation

    def extend(self, name: str, extra_along: int, world_id=None) -> Allocation:
        allocation = self.plan(world_id).extend(self._world, name, extra_along)
        self.save()
        return allocation
