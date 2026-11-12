"""Master site plan, computed once per map from a :class:`WorldMap`.

``plan_site(world)`` chooses (details in :mod:`site.planbuild`):

1. the primary patch of every required solid (iron, copper, coal, stone by
   default) and reserves ``mining:<resource>`` = patch bbox + margin;
2. a straight main bus (``bus``) with an ordered lane table; its
   ``direction`` is the item flow from the head (by smelting) to the tail;
3. the ``smelting`` zone just behind the bus head and the head-gap corridor
   ``corridor:smelting->bus``;
4. district strips (``districts:left`` / ``districts:right``) along the bus;
5. feeder corridors ``corridor:<resource>-><consumer>``, the ``power`` zone
   on a shore and ``corridor:power->smelting``.

Orientation is designed per zone from the geometry (see
:mod:`site.orientation`): every zone records ``input_side`` /
``output_side`` and ports, every corridor ``from_side`` / ``to_side`` and
directed segments, every allocation ``input_side`` / ``flow`` /
``output_side``.

Every zone is reserved on the map with ``WorldMap.reserve`` (owner = zone
name). District blocks are handed out with :meth:`MasterPlan.allocate`.
Units are tiles; see :mod:`site.busframe` for the bus-local frame.

Command line::

    python -m factorio_agent.site.masterplan <map.json> <out.svg>
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import sys

from . import allocator
from .busframe import Bus
from .corridors import subtract_rects
from .geometry import Rect
from .patches import Shore
from .planbuild import Draft, corridors_of, run_pipeline
from .planmodel import ResourcePlan, SiteConfig, SiteError, Strip, shore_from_dict, shore_to_dict
from .planvalidate import validate_plan
from .planzones import Allocation, Corridor, Zone
from .tilegrid import Occupancy, TileGrid
from .worldmap import WorldMap

FORMAT = "factorio-agent-masterplan"
VERSION = 2


@dataclass
class MasterPlan:
    """The whole site plan. Everything except ``allocations`` is fixed once planned."""

    start: tuple[int, int]
    config: SiteConfig
    bounds: Rect
    resources: tuple[ResourcePlan, ...]
    water: Shore | None
    bus: Bus
    district_depth: int
    zones: tuple[Zone, ...]
    strips: tuple[Strip, ...]
    corridors: tuple[Corridor, ...]
    warnings: tuple[str, ...] = ()
    allocations: list[Allocation] = field(default_factory=list)

    # ----- lookups ---------------------------------------------------------
    def zone(self, name: str) -> Zone | None:
        return next((z for z in self.zones if z.name == name), None)

    def resource(self, name: str) -> ResourcePlan | None:
        return next((r for r in self.resources if r.name == name), None)

    def corridor(self, name: str) -> Corridor | None:
        return next((c for c in self.corridors if c.name == name), None)

    # ----- districts -------------------------------------------------------
    def allocate(
        self, world: WorldMap, kind: str, along: int, depth: int, *, name: str, returns_to_bus: bool = True
    ) -> Allocation:
        """Next free district block of ``along`` x ``depth`` tiles; see site.allocator.

        ``returns_to_bus`` True: the product goes back onto the bus and leaves
        on the bus-adjacent edge; False: a terminal product leaving on the far
        edge. Both leave at the downstream end.
        """
        return allocator.allocate(self, world, kind, along, depth, name, returns_to_bus)

    def release(self, world: WorldMap, name: str) -> Allocation:
        """Free one allocation (its land returns to the district strip)."""
        return allocator.release(self, world, name)

    def extend(self, world: WorldMap, name: str, extra_along: int) -> Allocation:
        """Grow an allocation along the bus, away from the head, or raise SiteError."""
        return allocator.extend(self, world, name, extra_along)

    # ----- checks and output -----------------------------------------------
    def validate(self, world: WorldMap) -> list[str]:
        """Independent invariant check; returns a list of problems (empty = valid)."""
        return validate_plan(self, world)

    def summary(self) -> str:
        return plan_summary(self)

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "version": VERSION,
            "start": list(self.start),
            "config": self.config.to_dict(),
            "bounds": self.bounds.to_list(),
            "resources": [r.to_dict() for r in self.resources],
            "water": shore_to_dict(self.water),
            "bus": self.bus.to_dict(),
            "district_depth": self.district_depth,
            "zones": [z.to_dict() for z in self.zones],
            "strips": [s.to_dict() for s in self.strips],
            "corridors": [c.to_dict() for c in self.corridors],
            "warnings": list(self.warnings),
            "allocations": [a.to_dict() for a in self.allocations],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict) -> MasterPlan:
        if data.get("format") != FORMAT or data.get("version") != VERSION:
            raise ValueError(f"not a version {VERSION} factorio-agent master plan")
        return cls(
            start=tuple(data["start"]),
            config=SiteConfig.from_dict(data["config"]),
            bounds=Rect(*data["bounds"]),
            resources=tuple(ResourcePlan.from_dict(r) for r in data["resources"]),
            water=shore_from_dict(data["water"]),
            bus=Bus.from_dict(data["bus"]),
            district_depth=data["district_depth"],
            zones=tuple(Zone.from_dict(z) for z in data["zones"]),
            strips=tuple(Strip.from_dict(s) for s in data["strips"]),
            corridors=tuple(Corridor.from_dict(c) for c in data["corridors"]),
            warnings=tuple(data["warnings"]),
            allocations=[Allocation.from_dict(a) for a in data["allocations"]],
        )

    @classmethod
    def from_json(cls, text: str) -> MasterPlan:
        return cls.from_dict(json.loads(text))


def _reserve_all(world: WorldMap, zones, strips, corridors) -> None:
    """Reserve zones and strips; corridors skip tiles of earlier corridors."""
    for zone in zones:
        world.reserve(zone.rect, zone.name)
    for strip in strips:
        world.reserve(strip.rect, strip.owner)
    done: list[Rect] = []
    for corridor in corridors:
        for leg in corridor.rects:
            for piece in subtract_rects(leg, done):
                world.reserve(piece, corridor.name)
        done.extend(corridor.rects)


def _zone_order(zones: dict[str, Zone]) -> tuple[Zone, ...]:
    """Bus, smelting, power, then mining zones in planning order."""
    first = [zones[name] for name in ("bus", "smelting", "power") if name in zones]
    return tuple(first + [z for z in zones.values() if z.kind == "mining"])


def plan_site(world: WorldMap, *, start: tuple[int, int] = (0, 0), config: SiteConfig = SiteConfig()) -> MasterPlan:
    """Compute the master plan for ``world`` and reserve its zones on it.

    Deterministic for a given map, start and config. Raises SiteError only
    when even the minimum bus cannot be placed; softer shortfalls become
    ``MasterPlan.warnings``.
    """
    occupancy = Occupancy(TileGrid(world))
    draft = Draft(world=world, config=config, start=tuple(start), occupancy=occupancy)
    run_pipeline(draft)
    zones = _zone_order(draft.zones)
    corridors = corridors_of(draft)
    _reserve_all(world, zones, draft.strips, corridors)
    return MasterPlan(
        start=tuple(start),
        config=config,
        bounds=occupancy.grid.bounds,
        resources=tuple(draft.resources),
        water=draft.water,
        bus=draft.bus,
        district_depth=draft.depth,
        zones=zones,
        strips=tuple(draft.strips),
        corridors=tuple(corridors),
        warnings=tuple(draft.warnings),
    )


# ----- text summary and command line --------------------------------------
def _rect_text(rect: Rect) -> str:
    return f"[{rect.x0},{rect.y0})-[{rect.x1},{rect.y1}) {rect.width}x{rect.height}"


def _ports_text(zone: Zone) -> str:
    parts = [f"{p.material}{'>' if p.role == 'in' else '<'}{p.side}" for p in zone.ports]
    return " ".join(parts)


def plan_summary(plan: MasterPlan) -> str:
    """One-screen text description of the plan, including chosen sides."""
    bus = plan.bus
    assigned = [lane.material for lane in bus.lanes if lane.material]
    lines = [
        f"start {plan.start}  surveyed {_rect_text(plan.bounds)}",
        f"bus head {bus.head} -> tail {bus.tail} flow {bus.direction} width {bus.width} length {bus.length}"
        f"  lanes {len(bus.lanes)} ({len(assigned)} assigned, {len(bus.lanes) - len(assigned)} spare)",
    ]
    for resource in plan.resources:
        if resource.primary is not None:
            p = resource.primary
            lines.append(
                f"  {resource.name:<11} primary {p.tile_count} tiles at ({p.centroid[0]:.0f},{p.centroid[1]:.0f})"
                f" d={p.distance:.0f}  +{len(resource.others)} more"
            )
        else:
            lines.append(f"  {resource.name:<11} {len(resource.others)} patches (no primary)")
    if plan.water is not None:
        lines.append(f"  water shore at {plan.water.water}")
    for zone in plan.zones:
        sides = f"in {zone.input_side or '-'} out {zone.output_side or '-'}"
        lines.append(f"zone {zone.name:<18} {_rect_text(zone.rect)} {sides}  {_ports_text(zone)}")
    for side in ("left", "right"):
        runs = [s for s in plan.strips if s.side == side]
        total = sum(s.along1 - s.along0 for s in runs)
        lines.append(
            f"districts:{side:<5} {len(runs)} runs, {total} of {bus.length} tiles, depth {plan.district_depth}"
        )
    for corridor in plan.corridors:
        path = " ".join(
            f"{s.direction}{s.rect.width if s.direction in 'EW' else s.rect.height}" for s in corridor.segments
        )
        lines.append(f"{corridor.name:<28} {corridor.from_side}->{corridor.to_side} length {corridor.length} [{path}]")
    for a in plan.allocations:
        lines.append(
            f"block {a.name} {a.side} along {a.along0}-{a.along1} in {a.input_side} flow {a.flow} out {a.output_side}"
        )
    for warning in plan.warnings:
        lines.append(f"WARNING {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from .planrender import render_plan_svg

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m factorio_agent.site.masterplan <map.json> <out.svg>", file=sys.stderr)
        return 2
    world = WorldMap.from_json(Path(args[0]).read_text(encoding="utf-8"))
    try:
        plan = plan_site(world)
    except SiteError as error:
        print(f"planning failed: {error}", file=sys.stderr)
        return 1
    Path(args[1]).write_text(render_plan_svg(world, plan), encoding="utf-8")
    print(plan.summary())
    problems = plan.validate(world)
    for problem in problems:
        print(f"INVALID {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
