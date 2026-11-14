"""Offline seed sweep of the pure planners on synthetic worlds (``tools/plan_sweep.py``).

For one ``site.fakeworld.make_world`` seed :func:`run_seed` drives the planning pipeline as far
as it goes without a game and returns one row: per stage ``ok`` or the planner's own failure
reason, plus tidiness numbers. The stages follow the roadmap order:

1. ``masterplan``: ``site.masterplan.plan_site`` + ``MasterPlan.validate`` (warnings recorded);
2. ``mine:<ore>``: ``mine_layout.choose_bank`` on the plan's primary patch of each solid, on
   the planned exit edge (``mining_site.plan_exit``), snapshot built from the world's ore tiles;
3. ``smelting_district``: ``smelting_site.plan_candidates`` with the real furnace counts, land
   check of the area (what ``SmeltingDistrict.prepare``'s native survey does);
4. ``feed:<ore>``: ``transport.plan_connection`` from the bank's port to the district's ore
   port (the ore feeds the district needs);
5. ``main_bus``: ``planned_bus.PlannedBus.prepare`` (trunks, feeds through
   ``transport.plan_connection``);
6. ``automation_science`` / ``logistic_science``: ``science.AutomaticScience.prepare`` and
   ``green_science.LogisticScience.prepare`` on the published bus (layout candidates,
   ``SiteService`` blocks, ``planned_tap.connect_inputs`` taps, routes) - the chain the
   ``tests/test_v2_green_site.World`` harness drives, here with the real catalog;
7. ``block:<core>``: ``block_sites`` allocations (``SiteService.allocate_candidates``) of the
   later fixed cores in roadmap order, footprints probed from their real builders.

The game is replaced by :class:`OfflineGame`, which answers only read-only native checks from
the ``WorldMap`` (the planners' own query bodies, their JSON payload read back): the route
corridor survey, ``planning.survey_slots``, ``planning.preflight``, ``builder.can_place``, the
underground span and the site identity. Any other query is answered ``offline_query_unsupported``
and counted, so a stage that needs more of the game shows up as such (``NOT_OFFLINE``). Native
power-pole planning (``power_grid``) is stubbed ``ok`` with no poles: power is not swept.

Tidiness per block (``tidiness``): belts (transport, underground, splitter) of its plan and how
many lie outside its reserved area / allocation, total route length and turns
(``routing.metrics`` via each route's ``route_metrics``), underground pairs, longest feed;
planned corridor crossings of the master plan (``site.corridors.count_crossings``).
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import time
import traceback
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

WORLD_ID = "sweep"
SIZE = 512
MAX_SPAN = 5  # underground-belt max_underground_distance (base 2.x); the catalog does not carry it
BELTS = frozenset({"transport-belt", "underground-belt", "splitter"})
SOLIDS = ("iron-ore", "copper-ore", "coal", "stone")
FEEDS = ("iron-ore", "copper-ore", "coal")
DEFAULT_CATALOG = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "catalog_eval_1575138435.json.gz"

# Planners that cannot run offline today: (stage, file:function, what needs the game).
NOT_OFFLINE = (
    (
        "mining_outpost (bank build)",
        "mining_layout_policy.prepare_new_bank / plan_offore_exit",
        "native patch survey, exit prefix selection and drill preflight in Lua (mining exit routes)",
    ),
    ("power", "power_grid.plan_connection", "pole chains are planned against native entities; stubbed ok"),
    ("steam_expansion", "steam_expansion.py / steam_siting.py", "offshore pump and shore sites from native water"),
    (
        "steel_production / brick_production",
        "planned_furnaces.PlannedFurnaces.prepare",
        "needs built mining banks' dedicated material ports (mining-outposts.json from the live bank build)",
    ),
    ("oil_gathering", "oil_gathering.py", "pumpjack sites and pipe routes from native wells"),
    ("defense_perimeter", "defense_perimeter.py", "enemy bases and turret sites (no enemies on the fake world)"),
)


# ----- the offline game ------------------------------------------------------------------------
_PAYLOAD = re.compile(r'json_to_table\(("(?:[^"\\]|\\.)*")\)')


def _payload(body: str):
    match = _PAYLOAD.search(body)
    if match is None:
        return None
    return json.loads(json.loads(match.group(1)))


def _tile(x: float, y: float) -> tuple[int, int]:
    return math.floor(x), math.floor(y)


class OfflineGame:
    """Read-only native checks answered from a ``WorldMap``; anything else is unsupported."""

    def __init__(self, root: Path, world, builder):
        self.cfg = SimpleNamespace(runtime_dir=str(root))
        self.world = world
        self.builder = builder
        self.entities: list[dict] = []  # "built" entities (the harness's obs)
        self.unsupported: dict[str, int] = {}
        self.queries = 0
        self._occupied: dict | None = None

    # -- world tiles --
    def set_entities(self, entities) -> None:
        self.entities = list(entities)
        self._occupied = None

    def occupied(self) -> dict:
        if self._occupied is None:
            found = {}
            for e in self.entities:
                for p in self.builder._occupied_by_plan([e]):
                    found[p] = e
            self._occupied = found
        return self._occupied

    def terrain_blocked(self, x: int, y: int) -> bool:
        """Water, unbuildable, off the map, cliffs and fixed features (trees and rocks are clearable)."""
        if not self.world.is_surveyed(x, y):
            return True
        return not self.world.is_buildable(x, y, allow_clearable=True)

    def ore(self, x: int, y: int) -> str | None:
        if not self.world.is_surveyed(x, y):
            return None
        found = self.world.resource_at(x, y)
        return found[0] if found else None

    def nature(self, x: int, y: int) -> list[dict]:
        if not self.world.is_surveyed(x, y):
            return []
        return [
            {"name": f.name, "position": {"x": (f.rect.x0 + f.rect.x1) / 2, "y": (f.rect.y0 + f.rect.y1) / 2}}
            for f in self.world.features_at(x, y)
            if f.clearable
        ]

    # -- dispatch --
    def query(self, body: str):
        self.queries += 1
        if "get_spawn_position" in body:
            return {"ok": True, "world_id": WORLD_ID, "x": 0, "y": 0}
        if "max_underground_distance" in body and "json_to_table" not in body:
            return {"ok": True, "max_span": MAX_SPAN}
        args = _payload(body)
        if isinstance(args, dict) and "pipe_candidates" in args and "bounds" in args:
            return self._route_survey(args)
        if isinstance(args, dict) and "specs" in args and "zones" in args:
            return self._preflight(args)
        if "local slots=helpers.json_to_table" in body and isinstance(args, list):
            return self._slots(args)
        if "local plans=helpers.json_to_table" in body and isinstance(args, list):
            return self._slots([[plan["area"]] for plan in args])
        if "local specs=helpers.json_to_table" in body and isinstance(args, list):
            return self._can_place(args)
        key = " ".join(body.split())[:60]
        self.unsupported[key] = self.unsupported.get(key, 0) + 1
        return {"ok": False, "reason": "offline_query_unsupported"}

    def _in_zones(self, x, y, zones) -> bool:
        for zone in zones:
            (x0, y0), (x1, y1) = zone["area"]
            if x + 0.9 > x0 and x + 0.1 < x1 and y + 0.9 > y0 and y + 0.1 < y1:
                return True
        return False

    def _route_survey(self, args):
        b = args["bounds"]
        zones = args.get("zones") or []
        ends = {_tile(p["position"]["x"], p["position"]["y"]) for p in (args["source"], args["destination"])}
        occupied = self.occupied()
        ignored = set(args.get("ignored") or ())
        blocked, clearable = [], []
        x = b["min_x"]
        while x <= b["max_x"]:
            y = b["min_y"]
            while y <= b["max_y"]:
                tx, ty = _tile(x, y)
                hard = self.terrain_blocked(tx, ty)
                if not hard and (tx, ty) not in ends:
                    hard = self.ore(tx, ty) is not None or self._in_zones(tx, ty, zones)
                other = occupied.get((tx + 0.5, ty + 0.5))
                if not hard and other is not None and other.get("unit_number") not in ignored:
                    hard = other["name"] not in BELTS and other["name"] != "pipe"
                if hard:
                    blocked.append({"x": x, "y": y})
                else:
                    natural = self.nature(tx, ty)
                    if natural:
                        clearable.append({"position": {"x": x, "y": y}, "entities": natural})
                y += 1
            x += 1
        return {
            "ok": True,
            "blocked": blocked,
            "clearable": clearable,
            "max_span": MAX_SPAN,
            "exit_prefix": [],
            "exit_length": -1,
            "crossable_pipes": [],
        }

    def _area_tiles(self, area):
        (x0, y0), (x1, y1) = area
        for tx in range(math.floor(x0 + 1e-6), math.ceil(x1 - 1e-6)):
            for ty in range(math.floor(y0 + 1e-6), math.ceil(y1 - 1e-6)):
                yield tx, ty

    def _slots(self, slots):
        legal = []
        occupied = self.occupied()
        for index, areas in enumerate(slots, start=1):
            clear = True
            for area in areas:
                for tx, ty in self._area_tiles(area):
                    other = occupied.get((tx + 0.5, ty + 0.5))
                    if (
                        self.terrain_blocked(tx, ty)
                        or self.ore(tx, ty)
                        or (other is not None and other["name"] != "small-electric-pole")
                    ):
                        clear = False
                        break
                if not clear:
                    break
            if clear:
                legal.append(index)
        return {"ok": True, "legal": legal}

    def _preflight(self, args):
        ignored = set(args.get("ignored") or ())
        occupied = self.occupied()
        obstacles, seen = [], set()
        for spec in args["specs"]:
            if spec.get("resource_virtual_tunnel"):
                continue  # the tiles an underground passes under: no collision check (the native body skips them)
            mining = bool(spec.get("mining_resource")) or spec["name"] in ("electric-mining-drill", "pumpjack")
            for tx, ty in self._area_tiles(spec["survey_area"]):
                if self.terrain_blocked(tx, ty):
                    return {
                        "ok": False,
                        "reason": "block_terrain_or_immovable_obstruction",
                        "entity": spec["name"],
                        "position": spec["position"],
                    }
                if not mining and not spec.get("resource_virtual_tunnel") and self.ore(tx, ty):
                    return {
                        "ok": False,
                        "reason": "resource_policy_entity_on_ore",
                        "entity": spec["name"],
                        "position": spec["position"],
                    }
                other = occupied.get((tx + 0.5, ty + 0.5))
                same = other is not None and other["name"] == spec["name"] and other["position"] == spec["position"]
                if other is not None and not same and other.get("unit_number") not in ignored:
                    return {
                        "ok": False,
                        "reason": "block_protected_entity_obstruction",
                        "entity": other["name"],
                        "position": other["position"],
                    }
                for e in self.nature(tx, ty):
                    key = (e["name"], e["position"]["x"], e["position"]["y"])
                    if key not in seen:
                        seen.add(key)
                        obstacles.append(e)
        return {"ok": True, "clear_obstacles": obstacles}

    def _can_place(self, specs):
        blocked = []
        occupied = self.occupied()
        for spec in specs:
            for p in self.builder._occupied_by_plan([spec]):
                tx, ty = _tile(*p)
                other = occupied.get(p)
                same = other is not None and other["name"] == spec["name"] and other["position"] == spec["position"]
                if self.terrain_blocked(tx, ty) or (other is not None and not same):
                    blocked.append(
                        {"name": spec["name"], "position": spec["position"], "reason": "terrain_or_entity_collision"}
                    )
                    break
        return {"ok": not blocked, "blocked": blocked}


def _builder():
    from .core.deterministic_builder import FactoryBuilder

    geometry = FactoryBuilder.__new__(FactoryBuilder)
    return SimpleNamespace(_occupied_by_plan=geometry._occupied_by_plan, can_place=None)


# ----- tidiness --------------------------------------------------------------------------------
def _inside(point, area) -> bool:
    (x0, y0), (x1, y1) = area
    return x0 <= point[0] <= x1 and y0 <= point[1] <= y1


def block_tidiness(entities, areas, routes=(), pairs=()) -> dict:
    """Belts of one block's plan and how many stand outside every one of ``areas``; route metrics."""
    belts = [e for e in entities if e.get("name") in BELTS]
    outside = [e for e in belts if not any(_inside((e["position"]["x"], e["position"]["y"]), a) for a in areas)]
    metrics = [r for r in routes if isinstance(r, dict) and "length" in r]
    return {
        "belts": len(belts),
        "outside": len(outside),
        "route_length": sum(int(m.get("length") or 0) for m in metrics),
        "turns": sum(int(m.get("turns") or 0) for m in metrics),
        "undergrounds": len(pairs),
        "longest_feed": max((int(m.get("length") or 0) for m in metrics), default=0),
    }


def _rect_area(rect) -> list:
    return [[rect.x0, rect.y0], [rect.x1, rect.y1]]


# ----- stages ----------------------------------------------------------------------------------
class Sweep:
    """One seed's pipeline. Each stage records ``{"ok", "reason", ...}`` in ``self.stages``."""

    def __init__(self, seed: int, root: Path, catalog, *, size: int = SIZE):
        from .site.fakeworld import make_world
        from .site.service import SiteService

        self.seed, self.root, self.catalog = seed, root, catalog
        self.world = make_world(seed, size=size)
        self.builder = _builder()
        self.game = OfflineGame(root, None, self.builder)
        self.builder.can_place = lambda entities: self.game._can_place(entities)
        self.site = SiteService(self.game, half_size=size // 2, surveyor=lambda game, rect: self.world)
        self.stages: dict[str, dict] = {}
        self.tidiness: dict[str, dict] = {}
        self.entities: list[dict] = []
        self.blocks: dict[str, dict] = {}
        self.unit = 1
        self.clock = time.perf_counter()

    # -- bookkeeping --
    def record(self, name, result, **extra) -> bool:
        ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
        reason = None if ok else _reason(result)
        now = time.perf_counter()
        self.stages[name] = {"ok": ok, "reason": reason, "seconds": round(now - self.clock, 2), **extra}
        self.clock = now
        return ok

    def skip(self, name, after):
        self.stages[name] = {"ok": None, "reason": f"skipped: {after} failed"}

    def build(self, entities) -> None:
        """Entities enter the "built" world (units numbered); one standing on a tile of a new entity
        is replaced (tap splitters replace trunk belts, branches replace port belts)."""
        new = [dict(e) for e in entities]
        tiles = set()
        for e in new:
            tiles |= self.builder._occupied_by_plan([e])
        kept = [e for e in self.entities if not (self.builder._occupied_by_plan([e]) & tiles)]
        for e in new:
            e["unit_number"] = self.unit
            self.unit += 1
        self.entities = kept + new
        self.game.set_entities(self.entities)

    def obs(self, **extra) -> dict:
        names = ("automation-science-pack", "iron-gear-wheel", "logistic-science-pack", "fast-inserter")
        recipes = {n: True for n in self.catalog.recipes} if hasattr(self.catalog, "recipes") else {}
        recipes.update({n: True for n in names})
        return {
            "world_id": WORLD_ID,
            "tick": 100,
            "entities": list(self.entities),
            "enabled_recipes": recipes,
            "technologies": {"logistics": True},
            "inventory": {},
            **extra,
        }

    def write_board(self) -> None:
        board = {"world_id": WORLD_ID, "catalog_fingerprint": self.catalog.fingerprint, "connections": []}
        board["blocks"] = {k: v for k, v in self.blocks.items() if k != "shared_main_bus"}
        if "shared_main_bus" in self.blocks:
            board["shared_main_bus"] = self.blocks["shared_main_bus"]
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")

    # -- 1. master plan --
    def masterplan(self) -> bool:
        from .site.planmodel import SiteError

        self.game.world = self.world
        try:
            self.plan = self.site.plan(WORLD_ID)
        except SiteError as error:
            return self.record("masterplan", {"ok": False, "reason": f"SiteError: {error}"})
        self.world = self.site.world(WORLD_ID)
        self.game.world = self.world
        problems = self.plan.validate(self.world)
        warnings = [_warning_kind(w) for w in self.plan.warnings]
        if problems:
            return self.record("masterplan", {"ok": False, "reason": "invalid: " + _warning_kind(problems[0])})
        from .site.corridors import count_crossings

        rects = [c.rects for c in self.plan.corridors]
        crossings = sum(count_crossings(rects[i], rects[i + 1 :]) for i in range(len(rects)))
        self.tidiness["masterplan"] = {"corridor_crossings": crossings, "bus_length": self.plan.bus.length}
        return self.record("masterplan", {"ok": True}, warnings=warnings)

    # -- 2. mining banks --
    def snapshot(self, resource, zone_rect) -> dict:
        tiles = []
        for x, y in zone_rect.tiles():
            found = self.world.resource_at(x, y) if self.world.is_surveyed(x, y) else None
            if found and found[0] in SOLIDS:
                tiles.append({"name": found[0], "position": {"x": x + 0.5, "y": y + 0.5}, "amount": found[1]})
        drill = self.catalog.entities.get("electric-mining-drill", {})
        return {
            "ok": True,
            "tick": 0,
            "world_id": WORLD_ID,
            "catalog_fingerprint": self.catalog.fingerprint,
            "resource": resource,
            "area": _rect_area(zone_rect),
            "tiles": tiles,
            "native": {
                "mining_speed": drill.get("mining_speed", 0.5),
                "mining_radius": drill.get("mining_drill_radius", 2.49),
                "mining_time": 1,
                "belt_speed": 0.03125,
                "force_productivity_bonus": 0.0,
            },
        }

    def mine(self, resource) -> dict | None:
        from .mine_layout import choose_bank, frame
        from .mining_site import plan_exit, plan_zone_areas
        from .mine_layout import MineGenome

        name = "mine:" + resource
        record = plan_exit(self.plan, resource)
        if record is None:
            self.record(name, {"ok": False, "reason": "mining_plan_exit_missing"})
            return None
        zone = self.plan.zone("mining:" + resource)
        snapshot = self.snapshot(resource, zone.rect)
        if not any(t["name"] == resource for t in snapshot["tiles"]):
            self.record(name, {"ok": False, "reason": "mining_zone_has_no_resource"})
            return None
        reservations = [area for _owner, area in plan_zone_areas(self.site, WORLD_ID, exclude_prefix=zone.name)]
        reservations = [a for a in reservations if _overlaps(a, snapshot["area"])]
        forbidden = [(x + 0.5, y + 0.5) for x, y in zone.rect.tiles() if self.game.terrain_blocked(x, y)]
        chosen = choose_bank(
            self.catalog,
            snapshot,
            reservations,
            flow=record["flow"],
            pairs=2,
            root=self.root,
            forbidden=forbidden,
            tick=0,
        )
        if chosen is None:
            from .mine_layout import candidates

            found = candidates(
                self.catalog, snapshot, reservations, forbidden=forbidden, edges=(record["flow"],), buildable_only=True
            )
            why = found["skipped"][0]["why"] if found["skipped"] else "no_candidate"
            self.record(name, {"ok": False, "reason": "mine_no_frame: " + str(why).split(":")[0]})
            return None
        genome = MineGenome.from_dict(chosen["record"]["genome"])
        built = frame(snapshot, genome, reservations, forbidden=forbidden)
        ports = (built or {}).get("ports") or []
        self.record(name, {"ok": True}, drills=(built or {}).get("drills"), flow=record["flow"])
        return {"record": record, "ports": ports, "frame": built}

    # -- 3. smelting district with its ore and coal feed routes --
    def bank_port(self, resource, mine) -> dict:
        """The bank's output port: the frame's collector port nearest the planned exit point."""
        start = mine["record"]["corridor_start"]
        point = min(mine["ports"], key=lambda p: abs(p["x"] - start[0]) + abs(p["y"] - start[1]))
        return {
            "kind": "item",
            "item": resource,
            "direction": "output",
            "position": dict(point),
            "facing": mine["record"]["facing"],
        }

    def district(self, mines) -> bool:
        """``SmeltingDistrict.prepare`` on the mining banks' ports (``mining-outposts.json``, as a bank build writes it)."""
        from .smelting import SmeltingDistrict

        banks = []
        for resource in FEEDS:
            mine = mines.get(resource)
            if mine is None or not mine["ports"]:
                return self.record("smelting_district", {"ok": False, "reason": f"no_{resource}_bank"})
            plan = {"resource": resource, "ports": [self.bank_port(resource, mine)]}
            banks.append({"ready": True, "plan": plan, "nominal_rate_per_minute": 60})
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": WORLD_ID, "banks": banks}), "utf-8")
        self.write_board()
        district = SmeltingDistrict(self.game, None, self.builder, self.catalog, site=self.site)
        result = district.prepare(self.obs())
        if not self.record("smelting_district", result):
            return False
        plan = result["plan"]
        growth = {_key(e) for e in plan.get("growth_entities") or ()}
        self.blocks["smelting_district"] = plan
        self.build([e for e in plan["entities"] if _key(e) not in growth])
        state = {"world_id": WORLD_ID, "plan": plan, "banks": result["banks"]}
        (self.root / "smelting-district.json").write_text(json.dumps(state), encoding="utf-8")
        self.smelting = SimpleNamespace(state=state, path=self.root / "smelting-district.json")
        self.write_board()
        routes = [r.get("metrics") or {} for r in result.get("routes") or ()]
        areas = [plan["reserved_area"]] + self.corridor_areas(FEEDS)
        self.tidiness["smelting_district"] = block_tidiness(
            plan["entities"], areas, routes, plan.get("underground_pairs") or ()
        )
        return True

    def corridor_areas(self, items) -> list:
        """Reserved rects of the plan's feeder corridors and mining zones of ``items``."""
        areas = []
        for corridor in self.plan.corridors:
            if corridor.kind == "feeder" and any(corridor.source == "mining:" + item for item in items):
                areas += [_rect_area(r) for r in corridor.rects]
        for item in items:
            zone = self.plan.zone("mining:" + item)
            if zone is not None:
                areas.append(_rect_area(zone.rect))
        return areas

    # -- 5. planned main bus --
    def main_bus(self) -> bool:
        from . import planned_bus as pb

        bus = pb.PlannedBus(self.game, self.builder, self.catalog, self.smelting, self.site)
        state = bus.prepare(self.obs())
        if not self.record("main_bus", state):
            return False
        state["phase"] = "verified"
        (self.root / pb.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
        entities = state["staged_plan"]["entities"]
        self.build(entities)
        self.blocks["shared_main_bus"] = pb.publication(state)
        self.write_board()
        band = _rect_area(self.plan.bus.rect())
        corridor = self.plan.corridor(pb.HEADER_CORRIDOR)
        areas = [band] + ([_rect_area(r) for r in corridor.rects] if corridor is not None else [])
        routes = [r for r in state.get("routes") or () if isinstance(r, dict)]
        self.tidiness["main_bus"] = block_tidiness(
            entities, areas, routes, state["staged_plan"].get("underground_pairs") or ()
        )
        return True

    def mining_state(self) -> None:
        mining = {
            "world_id": WORLD_ID,
            "banks": [
                {"ready": True, "plan": {"resource": "iron-ore"}, "nominal_rate_per_minute": 60},
                {"ready": True, "plan": {"resource": "copper-ore"}, "nominal_rate_per_minute": 60},
            ],
        }
        (self.root / "mining-outposts.json").write_text(json.dumps(mining), encoding="utf-8")

    # -- 6. science blocks --
    def red(self) -> bool:
        from . import planned_tap as pt
        from .science import AutomaticScience
        from .transport import xy

        self.mining_state()
        red = AutomaticScience(self.game, self.builder, self.catalog, site=self.site)
        result = red.prepare(self.obs(), {"rate_per_minute": 30})
        if not self.record("automation_science", result):
            return False
        removed = {xy({"position": p}) for tap in result["taps"] for p in tap["remove"]}
        self.entities = [e for e in self.entities if not (e["name"] == "transport-belt" and xy(e) in removed)]
        # Only the start stage stands (labs and machines beyond it are reserved growth, unbuilt).
        growth = {_key(e) for e in (result.get("stage") or {}).get("growth_entities", ())}
        self.build([e for e in result["plan"]["entities"] if _key(e) not in growth])
        for tap in result["taps"]:
            tap["built"] = True
        pt.publish(self.root, WORLD_ID, result["taps"])
        self.blocks.update(automation_science=result["science"], laboratories=result["labs"])
        self.write_board()
        self.red_state = result
        areas = [result["science"]["reserved_area"], result["labs"]["reserved_area"]]
        allocation = self.site.allocation(result["allocation"], WORLD_ID) if result.get("allocation") else None
        if allocation is not None:
            areas.append(_rect_area(allocation.rect))
        self.tidiness["automation_science"] = block_tidiness(
            result["plan"]["entities"],
            areas,
            result.get("route_metrics") or (),
            result["plan"].get("underground_pairs") or (),
        )
        return True

    def green(self) -> bool:
        from .green_science import LogisticScience

        state = self.red_state
        science = SimpleNamespace(
            proof=lambda obs: True, state={"labs": state["labs"], "nominal_machine_rate": state["nominal_machine_rate"]}
        )
        green = LogisticScience(self.game, self.builder, self.catalog, self.smelting, science, site=self.site)
        result = green.prepare(self.obs(), {"rate_per_minute": 30})
        if not self.record("logistic_science", result):
            return False
        plan = result.get("plan") or {}
        entities = plan.get("entities") or []
        self.build(entities)
        areas = [a for a in (plan.get("reserved_area"), (result.get("core") or {}).get("reserved_area")) if a]
        allocation = self.site.allocation(result["allocation"], WORLD_ID) if result.get("allocation") else None
        if allocation is not None:
            areas.append(_rect_area(allocation.rect))
        routes = result.get("route_metrics") or [r.get("route_metrics") or {} for r in result.get("routes") or ()]
        self.tidiness["logistic_science"] = block_tidiness(entities, areas, routes, plan.get("underground_pairs") or ())
        return True

    # -- 7. later fixed cores: allocation only --
    def cores(self) -> None:
        from . import block_sites

        for name, spec in _core_builders(self.catalog):
            stage = "block:" + name
            returns_to_bus = False
            if spec[0] == "size":
                _kind, width, height, returns_to_bus = spec
            else:
                try:
                    width, height, _dx, _dy = block_sites.footprint(spec[1])
                except Exception as error:  # noqa: BLE001 - a builder that cannot probe is a finding
                    self.record(stage, {"ok": False, "reason": f"footprint_probe_failed: {type(error).__name__}"})
                    continue
            try:
                found = self.site.allocate_candidates(
                    block_sites.KIND,
                    width,
                    height,
                    name=name,
                    count=1,
                    returns_to_bus=returns_to_bus,
                    world_id=WORLD_ID,
                )
            except Exception as error:  # noqa: BLE001
                self.record(stage, {"ok": False, "reason": f"allocation_error: {error}"})
                continue
            if not found:
                self.record(stage, {"ok": False, "reason": f"no_district_block_{width}x{height}"})
                continue
            self.site.keep_only(name + "#", found[0].name, WORLD_ID)
            self.record(stage, {"ok": True}, size=[width, height])

    # -- driver --
    def run(self, upto: str | None = None) -> dict:
        """Every stage in order; ``upto`` (a stage group of ``GROUPS``) stops after that group."""
        groups = GROUPS[: GROUPS.index(upto) + 1] if upto else GROUPS
        mines: dict = {}
        steps = {
            "masterplan": lambda: self.masterplan(),
            "mine": lambda: mines.update({r: self.mine(r) for r in SOLIDS}) or True,
            "smelting_district": lambda: self.district(mines),
            "main_bus": lambda: self.main_bus(),
            "automation_science": lambda: self.red(),
            "logistic_science": lambda: self.green(),
        }
        failed = None
        for group in groups:
            if group == "blocks":
                if self.stages.get("masterplan", {}).get("ok"):
                    self.cores()
                else:
                    for name in _CORE_NAMES:
                        self.skip("block:" + name, "masterplan")
                continue
            if failed is not None:
                for stage in _group_stages(group):
                    self.skip(stage, failed)
                continue
            if not steps[group]():
                failed = group
        return self.row()

    def row(self) -> dict:
        return {
            "seed": self.seed,
            "stages": self.stages,
            "tidiness": self.tidiness,
            "unsupported_queries": dict(self.game.unsupported),
        }


def _core_builders(catalog):
    """``(owner, spec)`` of the later district blocks in roadmap order.

    ``spec`` is ``("build", build(x, y))`` for a fixed core sited by ``block_sites`` (footprint
    probed from the real builder) or ``("size", along, depth, returns_to_bus)`` for a planner
    that asks the allocator itself (``planned_furnaces``, ``rocket_stages``).
    """
    from . import planned_furnaces as pf
    from . import rocket_stages as rs
    from .advanced_circuits import OWNER as ADVANCED_CIRCUITS, advanced_circuit_core
    from .advanced_petroleum import OWNER as ADVANCED_PETROLEUM, advanced_refinery_core
    from .chemical_production import chemical_core
    from .chemical_science import OWNER as CHEMICAL_SCIENCE, chemical_science_core
    from .petroleum import OWNER as PETROLEUM, refinery_core

    furnace = ("size", pf.LENGTH, pf.BANK_DEPTH, True)
    silo = rs.SILO_TILES + 2 * rs.RING
    return (
        (pf.KINDS["steel"].owner, furnace),
        (pf.KINDS["brick"].owner, furnace),
        (PETROLEUM, ("build", lambda x, y: refinery_core(catalog, x, y))),
        ("chemical_production_plastic-bar", ("build", lambda x, y: chemical_core(catalog, "plastic-bar", x, y))),
        (ADVANCED_PETROLEUM, ("build", lambda x, y: advanced_refinery_core(catalog, x, y))),
        (ADVANCED_CIRCUITS, ("build", lambda x, y: advanced_circuit_core(catalog, x, y, 2))),
        ("chemical_production_sulfur", ("build", lambda x, y: chemical_core(catalog, "sulfur", x, y))),
        (CHEMICAL_SCIENCE, ("build", lambda x, y: chemical_science_core(catalog, x, y, 2))),
        (rs.BLOCK, ("size", silo, silo, False)),
    )


def _key(e) -> tuple:
    return (e["name"], e["position"]["x"], e["position"]["y"])


def _overlaps(a, b) -> bool:
    return a[0][0] < b[1][0] and b[0][0] < a[1][0] and a[0][1] < b[1][1] and b[0][1] < a[1][1]


_NUMBERS = re.compile(r"-?\d+(\.\d+)?")


def _warning_kind(text: str) -> str:
    """A warning or problem without its numbers (so equal kinds aggregate)."""
    return _NUMBERS.sub("#", str(text))[:90]


def _reason(result) -> str:
    if not isinstance(result, dict):
        return "failed"
    reason = str(result.get("reason") or "failed")
    inner = result.get("failures")
    if isinstance(inner, list) and inner:
        first = next((f for f in inner if isinstance(f, dict) and f.get("reason")), None)
        if first is not None:
            reason += " <- " + str(first["reason"])
            if first.get("item"):
                reason += f" ({first['item']})"
    elif isinstance(result.get("failure"), dict) and result["failure"].get("reason"):
        reason += " <- " + str(result["failure"]["reason"])
    return reason


# ----- one seed, many seeds --------------------------------------------------------------------
_CATALOGS: dict = {}


def load_catalog(path=None):
    import gzip

    from .core.world_catalog import WorldCatalog

    path = Path(path or DEFAULT_CATALOG)
    key = str(path)
    if key not in _CATALOGS:
        raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
        catalog = WorldCatalog.from_dict(json.loads(raw))
        _CATALOGS[key] = catalog
    return _CATALOGS[key]


def _quiet_power(*_args, **_kwargs):
    return {"ok": True, "entities": []}


ROUTE_BUDGET = 1_500_000  # router skeleton checks per seed: a count, not a clock (deterministic)


class RouteBudget:
    """A per-seed work budget of the router, counted in ``routing.clear_skeleton`` calls.

    The router is the sweep's whole cost: a failing district search on a hard seed runs the
    skeleton and long-route search for every candidate site (minutes of CPU on one seed). The
    live district cuts each pass at a wall-clock budget and resumes later; the sweep counts
    the router's inner skeleton checks instead. Past the budget every check fails at once and
    the route reports ``sweep_route_budget_exhausted`` (a sweep limit, not a planner verdict).
    """

    def __init__(self, limit: int):
        from . import routing, transport

        self.limit, self.work, self.calls, self.tripped = limit, 0, 0, False
        self.inner_check, self.inner_route = routing.clear_skeleton, transport.route_trunks
        self.inner_turns = routing.min_turn_paths

    def check(self, *args, **kwargs):
        self.work += 1
        if self.limit and self.work > self.limit:
            self.tripped = True
            return None
        return self.inner_check(*args, **kwargs)

    def turns(self, *args, **kwargs):
        if self.tripped:
            return []
        return self.inner_turns(*args, **kwargs)

    def route(self, *args, **kwargs):
        self.calls += 1
        if self.tripped:
            return {"ok": False, "reason": "sweep_route_budget_exhausted", "path": [], "segments": [], "visited": 0}
        result = self.inner_route(*args, **kwargs)
        if self.tripped and not result.get("ok"):
            return dict(result, reason="sweep_route_budget_exhausted")
        return result


def run_seed(
    seed: int,
    catalog_path=None,
    *,
    size: int = SIZE,
    keep: Path | None = None,
    route_budget: int = ROUTE_BUDGET,
    upto: str | None = None,
) -> dict:
    """The sweep row of one seed (deterministic). ``keep``: write the runtime files there."""
    catalog = load_catalog(catalog_path)
    started = time.perf_counter()
    with ExitStack() as stack:
        if keep is None:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="plan-sweep-")))
        else:
            root = Path(keep)
            root.mkdir(parents=True, exist_ok=True)
        for target in ("science.power_connection", "green_science.connect_power_groups", "smelting.power_connection"):
            stack.enter_context(patch("factorio_agent." + target, _quiet_power))
        budget = RouteBudget(route_budget)
        stack.enter_context(patch("factorio_agent.transport.route_trunks", budget.route))
        stack.enter_context(patch("factorio_agent.routing.clear_skeleton", budget.check))
        stack.enter_context(patch("factorio_agent.routing.min_turn_paths", budget.turns))
        # The district's wall-clock planning budget would make the sweep machine-dependent.
        stack.enter_context(patch("factorio_agent.industrial.PlanningBudget.expired", lambda self: False))
        sweep = Sweep(seed, root, catalog, size=size)
        try:
            row = sweep.run(upto)
        except Exception as error:  # noqa: BLE001 - a crash is a finding of its stage
            row = sweep.row()
            stage = next((s for s in _order() if s not in row["stages"]), "crash")
            frame = traceback.extract_tb(error.__traceback__)[-1]
            where = f"{Path(frame.filename).name}:{frame.lineno}"
            row["stages"][stage] = {"ok": False, "reason": f"crash: {type(error).__name__} at {where}"}
        row["route_calls"] = budget.calls
        row["route_work"] = budget.work
        row["seconds"] = round(time.perf_counter() - started, 3)
        row["_sweep"] = sweep if keep is not None else None
    return row


def _order() -> list[str]:
    return [
        "masterplan",
        *("mine:" + r for r in SOLIDS),
        "smelting_district",
        "main_bus",
        "automation_science",
        "logistic_science",
        *("block:" + name for name in _CORE_NAMES),
    ]


_CORE_NAMES = (
    "steel_production",
    "brick_production",
    "basic_petroleum",
    "chemical_production_plastic-bar",
    "advanced-petroleum",
    "advanced_circuit_production",
    "chemical_production_sulfur",
    "chemical_science_production",
    "rocket_silo",
)
GROUPS = ("masterplan", "mine", "smelting_district", "main_bus", "automation_science", "logistic_science", "blocks")


def _group_stages(group: str) -> list[str]:
    if group == "mine":
        return ["mine:" + r for r in SOLIDS]
    if group == "blocks":
        return ["block:" + name for name in _CORE_NAMES]
    return [group]


def _worker(args):
    seed, catalog_path, size, route_budget, upto = args
    row = run_seed(seed, catalog_path, size=size, route_budget=route_budget, upto=upto)
    row.pop("_sweep", None)
    return row


def sweep(
    seeds,
    catalog_path=None,
    *,
    size: int = SIZE,
    workers: int = 1,
    route_budget: int = ROUTE_BUDGET,
    upto: str | None = None,
) -> list[dict]:
    """Rows of every seed, in seed order (``workers`` > 1: a process pool)."""
    jobs = [(int(s), str(catalog_path) if catalog_path else None, size, route_budget, upto) for s in seeds]
    if workers <= 1 or len(jobs) <= 1:
        return [_worker(job) for job in jobs]
    from multiprocessing import get_context

    with get_context("spawn").Pool(workers) as pool:
        rows = list(pool.imap_unordered(_worker, jobs, chunksize=1))
    return sorted(rows, key=lambda r: r["seed"])


# ----- aggregate -------------------------------------------------------------------------------
def _median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def aggregate(rows) -> dict:
    """Failure rate per stage, top reasons (count, example seed), tidiness medians."""
    order = _order()
    names = order + sorted({s for r in rows for s in r["stages"]} - set(order))
    stages = {}
    reasons: dict = {}
    for name in names:
        ran = [r for r in rows if name in r["stages"] and r["stages"][name]["ok"] is not None]
        failed = [r for r in ran if not r["stages"][name]["ok"]]
        skipped = sum(1 for r in rows if name in r["stages"] and r["stages"][name]["ok"] is None)
        stages[name] = {"ran": len(ran), "failed": len(failed), "skipped": skipped}
        for r in failed:
            key = (name, r["stages"][name]["reason"])
            entry = reasons.setdefault(key, {"stage": name, "reason": key[1], "count": 0, "seed": r["seed"]})
            entry["count"] += 1
    warnings: dict = {}
    for r in rows:
        for w in (r["stages"].get("masterplan") or {}).get("warnings", ()):
            entry = warnings.setdefault(w, {"warning": w, "count": 0, "seed": r["seed"]})
            entry["count"] += 1
    tidy: dict = {}
    for r in rows:
        for block, values in r["tidiness"].items():
            for key, value in values.items():
                tidy.setdefault(block, {}).setdefault(key, []).append(value)
    per_seed = [_seed_tidiness(r) for r in rows]
    overall = {k: _median([t[k] for t in per_seed]) for k in per_seed[0]} if per_seed else {}
    unsupported: dict = {}
    for r in rows:
        for key, count in r.get("unsupported_queries", {}).items():
            unsupported[key] = unsupported.get(key, 0) + count
    return {
        "seeds": len(rows),
        "stages": stages,
        "reasons": sorted(reasons.values(), key=lambda e: (-e["count"], e["stage"], e["reason"])),
        "warnings": sorted(warnings.values(), key=lambda e: (-e["count"], e["warning"])),
        "tidiness": {b: {k: _median(v) for k, v in values.items()} for b, values in tidy.items()},
        "tidiness_per_seed_median": overall,
        "unsupported_queries": unsupported,
        "seconds": round(sum(r.get("seconds", 0) for r in rows), 1),
    }


def _seed_tidiness(row) -> dict:
    blocks = [v for k, v in row["tidiness"].items() if k != "masterplan"]
    return {
        "belts": sum(v.get("belts", 0) for v in blocks),
        "outside": sum(v.get("outside", 0) for v in blocks),
        "route_length": sum(v.get("route_length", 0) for v in blocks),
        "turns": sum(v.get("turns", 0) for v in blocks),
        "undergrounds": sum(v.get("undergrounds", 0) for v in blocks),
        "longest_feed": max((v.get("longest_feed", 0) for v in blocks), default=0),
        "corridor_crossings": (row["tidiness"].get("masterplan") or {}).get("corridor_crossings", 0),
    }


def failure_counts(rows) -> dict:
    """``{stage: failed}`` (the gate test pins these as upper bounds)."""
    return {name: entry["failed"] for name, entry in aggregate(rows)["stages"].items()}


def report(summary: dict, *, top: int = 8, wall: float | None = None) -> str:
    lines = [f"plan sweep: {summary['seeds']} seeds" + (f" in {wall:.1f} s wall" if wall is not None else "")]
    lines.append(f"{'stage':<28}{'ran':>5}{'fail':>6}{'rate':>7}{'skip':>6}")
    for name, s in summary["stages"].items():
        rate = f"{100 * s['failed'] / s['ran']:.0f}%" if s["ran"] else "-"
        lines.append(f"{name:<28}{s['ran']:>5}{s['failed']:>6}{rate:>7}{s['skipped']:>6}")
    lines.append(f"top failure reasons (of {len(summary['reasons'])}):")
    for e in summary["reasons"][:top]:
        lines.append(f"  {e['count']:>4}  {e['stage']:<22} {e['reason'][:110]}  (seed {e['seed']})")
    if summary["warnings"]:
        lines.append("master plan warnings:")
        for e in summary["warnings"][:5]:
            lines.append(f"  {e['count']:>4}  {e['warning']}  (seed {e['seed']})")
    lines.append("tidiness medians per block:")
    for block, values in summary["tidiness"].items():
        lines.append("  " + f"{block:<22}" + " ".join(f"{k}={_fmt(v)}" for k, v in values.items()))
    lines.append(
        "tidiness per seed (median): "
        + " ".join(f"{k}={_fmt(v)}" for k, v in summary["tidiness_per_seed_median"].items())
    )
    if summary["unsupported_queries"]:
        lines.append("queries the offline game cannot answer (stage coverage gap):")
        for key, count in sorted(summary["unsupported_queries"].items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"  {count:>5}  {key}")
    lines.append("not offline-capable (needs the game):")
    for stage, where, why in NOT_OFFLINE:
        lines.append(f"  {stage}: {where} - {why}")
    return "\n".join(lines)


def _fmt(value) -> str:
    if value is None:
        return "-"
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)
