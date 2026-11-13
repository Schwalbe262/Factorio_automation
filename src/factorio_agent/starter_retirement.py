"""Starter cells stop being served once a permanent source exists, then are retired by paid mining.

The early base is a set of hand-served burner cells (burner drill -> stone
furnace or chest, ``core/deterministic_bootstrap``). Once the factory has a
permanent source for a resource they are only busywork: on seed 2099054105
the character kept fuelling and emptying them after the district and the bus
stood (43% of 818 actions hauling). Rules (PLANNERS.md "Starter retirement"):

1. ``retired_resources``: a resource is retired when every group of
   ``PERMANENT_SOURCES`` has a succeeded job (verified job evidence) and the
   board shows the source (``_board_supports``). Coal cells fuel every other
   starter cell, so coal also waits for the retirement of ``COAL_LAST_AFTER``.
2. A retired resource's unlinked starter cells get no fuel
   (``V2Bootstrap.maintain_fuel`` via ``cell_keys``), no new cell
   (``V2Bootstrap._ensure_cell``) and no capacity node (``progression.capacity_nodes``).
3. Job ``starter_retirement {"resource"}`` (``starter_retirement_nodes``, priority
   ``RETIRE_PRIORITY``): final collection of the remaining stock in large
   trips, then paid guarded ``mine`` of every cold, empty, unlinked cell (drill
   first); contents and fuel return to the inventory, the tiles are free.
   A cell an inserter serves is kept (and keeps its fuel). Coal chest stock moves
   in trips no larger than ``carried_surplus.CarriedSurplus.trip_room``: the
   excess over the carry allowance goes into consumers or a depot first.

Construction collection point (``collection_points``): once the planned bus
is published, the trunk tiles past the last tap window of each lane hold only
surplus plates; ``V2Bootstrap._take_output`` may empty them (guarded
``take_many``) and prefers them over every other plate source.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import obsolete
from factorio_agent.core.deterministic_bootstrap import SURVEY_LAYOUT_TICKS
from .core.deterministic_state import _atomic_json

CAPABILITY = "starter_retirement"
STATE_FILE = "starter-retirement.json"
NODE_PREFIX = "auto_starter_retirement_"
RETIRE_PRIORITY = obsolete.RETIRE_PRIORITY  # below every roadmap step: background demolition
RESOURCES = ("iron-ore", "copper-ore", "stone", "coal")
PLATE_OF = {"iron-ore": "iron-plate", "copper-ore": "copper-plate"}
RECEIVERS = {
    "iron-ore": ("stone-furnace",),
    "copper-ore": ("stone-furnace",),
    "stone": ("wooden-chest", "iron-chest"),
    "coal": ("wooden-chest", "iron-chest"),
}
# Every group needs one succeeded job; (capability, resource parameter or None).
PERMANENT_SOURCES = {
    "iron-ore": ((("smelting_district", None),), (("planned_main_bus", None),)),
    "copper-ore": ((("smelting_district", None),), (("planned_main_bus", None),)),
    "stone": ((("mining_outpost", "stone"), ("dedicated_materials", "stone")),),
    "coal": ((("electric_coal", None),), (("mining_outpost", "coal"), ("dedicated_materials", "coal"))),
}
COAL_LAST_AFTER = ("iron-ore", "copper-ore", "stone")
COLLECT_BATCH = 50  # final collection: one trip once this much waits, or whatever is left in cold cells
TAKE_LIMIT = 50  # take_many / take envelope
MAX_SOURCES = 16
LINK_RADIUS = 4  # inserters searched around a cell entity
FUELS = ("coal", "wood")
CARRY_BOUNDED = frozenset({"coal"})  # chest stock moved within carried_surplus.trip_room
SURPLUS_GAP = 1  # trunk rows kept free behind the last tap window
# Trunk rows at the lane end that are never collection points: the bus proof
# (``planned_bus.proof``) and growth evidence read plates on the tail tile, and
# a take_many sweep takes the fullest tiles (the tail) first.
TAIL_KEEP = 2


def _succeeded(jobs):
    return {
        (job.get("capability"), (job.get("parameters") or {}).get("resource"))
        for job in jobs
        if job.get("status") == "succeeded"
    }


def permanent_by_jobs(jobs) -> set[str]:
    """Resources whose permanent source is proven by succeeded jobs."""
    done = _succeeded(jobs)
    capabilities = {capability for capability, _ in done}
    found = set()
    for resource, groups in PERMANENT_SOURCES.items():
        if all(
            any((cap in capabilities) if res is None else ((cap, res) in done) for cap, res in group)
            for group in groups
        ):
            found.add(resource)
    return found


def retired_by_jobs(jobs) -> set[str]:
    """``permanent_by_jobs``; coal only after the retirement of every ``COAL_LAST_AFTER`` resource succeeded."""
    jobs = list(jobs)
    found = permanent_by_jobs(jobs)
    retired = {res for cap, res in _succeeded(jobs) if cap == CAPABILITY}
    if "coal" in found and not set(COAL_LAST_AFTER) <= retired:
        found.discard("coal")
    return found


def _board_supports(resource, board, world_id) -> bool:
    """The board of this world shows the permanent source: the bus carries the plate, or a mining bank outputs it."""
    if not isinstance(board, dict) or board.get("world_id") != world_id:
        return False
    if resource in PLATE_OF:
        from .planned_tap import carries, published

        return carries(published(board, world_id), PLATE_OF[resource])
    for block in (board.get("blocks") or {}).values():
        if any(e.get("name") == "electric-mining-drill" for e in block.get("entities", [])) and any(
            p.get("direction") == "output" and p.get("item") == resource for p in block.get("ports", [])
        ):
            return True
    return False


def retired_resources(jobs, board, world_id) -> set[str]:
    """Resources whose starter cells are no longer served (module rule 1)."""
    return {r for r in retired_by_jobs(jobs) if _board_supports(r, board, world_id)}


def collection_points(bus) -> dict[tuple, str]:
    """``(name, x, y) -> plate`` of the trunk belts past the last tap window of each built lane."""
    if not isinstance(bus, dict) or bus.get("template") != "planned_main_bus":
        return {}
    from .planned_tap import WINDOW_ROWS, BusView

    try:
        view = BusView(bus)
    except (KeyError, TypeError, ValueError):
        return {}
    points = {}
    for item, lane in view.items.items():
        stations = [s.get("along") for s in view.stations if s.get("lane") == lane]
        start = max((a + WINDOW_ROWS + SURPLUS_GAP for a in stations if isinstance(a, int)), default=0)
        offset = view.lanes[lane].offset
        for row in range(start, view.lengths.get(lane, 0) - TAIL_KEEP):
            spot = view.world(row, offset)
            belt = view.tiles.get((spot["x"], spot["y"]))
            if belt and belt.get("name") == "transport-belt" and belt.get("direction", view.facing) == view.facing:
                points[("transport-belt", spot["x"], spot["y"])] = item
    return points


def validate_parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"resource"} or parameters["resource"] not in RESOURCES:
        raise ValueError("starter_retirement requires resource iron-ore, copper-ore, stone or coal")


def starter_retirement_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One node per retired resource, once (a blocked job is auto-retried, never proposed twice)."""
    if CAPABILITY not in capabilities:
        return []
    jobs = list(jobs)
    succeeded = {job.get("capability") for job in jobs if job.get("status") == "succeeded"}
    if not set(capabilities[CAPABILITY].get("requires", [])) <= succeeded:
        return []
    taken = {
        (row.get("parameters") or {}).get("resource")
        for row in list(jobs) + list(queue_nodes)
        if row.get("capability") == CAPABILITY
    }
    retired = retired_by_jobs(jobs)
    for resource in RESOURCES:
        if resource in retired and resource not in taken:
            return [
                obsolete.retire_node(
                    NODE_PREFIX + resource,
                    CAPABILITY,
                    {"resource": resource},
                    f"{resource} has a permanent source: collect the starter cells' stock and mine them",
                    RETIRE_PRIORITY,
                )
            ]
    return []


def _key(spec):
    p = spec.get("position") or {}
    return (spec.get("name"), p.get("x"), p.get("y"))


def _fuel(entity):
    inventory = entity.get("inventory") or {}
    return sum(int(inventory.get(f, 0)) for f in FUELS)


LINK_LUA = """
local x=helpers.json_to_table(%s)
local linked={}
for i,spec in ipairs(x) do
 local e=target(spec.position,spec.name)
 if e and e.valid then
  for _,ins in pairs(s.find_entities_filtered{position=e.position,radius=%d,type="inserter"}) do
   if ins.valid and (ins.pickup_target==e or ins.drop_target==e) then linked[#linked+1]=i;break end
  end
 end
end
return {ok=true,linked=linked}
"""


class StarterRetirement:
    def __init__(self, game, bootstrap, builder=None):
        self.game, self.bootstrap, self.builder = game, bootstrap, builder
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE
        self._cache = None

    # ----- inputs -------------------------------------------------------------
    def _inputs(self, obs):
        """(same-world jobs, board) from ``jobs.json`` / ``pcb-board.json``, re-read only when a file changed."""
        world = obs.get("world_id")
        files = [self.root / "jobs.json", self.root / "pcb-board.json"]
        stamp = (world, *((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in files))
        if self._cache is None or self._cache[0] != stamp:
            jobs, board = [], {}
            if files[0].exists():
                ledger = json.loads(files[0].read_text(encoding="utf-8"))
                if ledger.get("world_id") == world:
                    jobs = [j for j in (ledger.get("jobs") or {}).values() if j.get("world_id", world) == world]
            if files[1].exists():
                board = json.loads(files[1].read_text(encoding="utf-8"))
                if board.get("world_id") != world:
                    board = {}
            self._cache = (stamp, jobs, board)
        return self._cache[1], self._cache[2]

    def resources(self, obs) -> set[str]:
        """Retired resources of the observed world (empty before any permanent source: early game unchanged)."""
        if not obs.get("world_id"):
            return set()
        jobs, board = self._inputs(obs)
        return retired_resources(jobs, board, obs["world_id"])

    def _state(self, world):
        state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if state.get("world_id") != world:
            state = {"world_id": world, "resources": {}}
        return state

    # ----- cells --------------------------------------------------------------
    def _survey(self, obs, resource):
        """Unregistered burner drill -> starter receiver cells of ``resource`` (None when the survey failed)."""
        report = self.bootstrap._existing_cells(resource, max_age_ticks=SURVEY_LAYOUT_TICKS)
        if not isinstance(report, dict) or not report.get("ok"):
            return None
        _, board = self._inputs(obs)
        registered = set()
        for block in list((board.get("blocks") or {}).values()) + [board.get("shared_main_bus") or {}]:
            registered.update(_key(e) for e in block.get("entities", []))
        owns = getattr(self.builder, "owns_automated_burner", None)
        cells = []
        for c in report.get("cells", []):
            drill, receiver = c.get("drill") or {}, c.get("receiver")
            if drill.get("name") != "burner-mining-drill" or c.get("electric") or c.get("direct_belt"):
                continue
            if not receiver or receiver.get("name") not in RECEIVERS[resource]:
                continue
            if _key(drill) in registered or _key(receiver) in registered:
                continue
            if owns is not None and owns({"name": drill["name"], "position": drill["position"]}):
                continue
            cells.append(
                {
                    "drill": {"name": drill["name"], "position": dict(drill["position"])},
                    "receiver": {"name": receiver["name"], "position": dict(receiver["position"])},
                }
            )
        return cells

    def _linked(self, specs) -> set[int]:
        """Indexes of ``specs`` an inserter picks from or drops into (a cell that feeds something)."""
        if not specs:
            return set()
        payload = json.dumps(json.dumps([{"name": s["name"], "position": s["position"]} for s in specs]))
        reply = self.game.query(LINK_LUA % (payload, LINK_RADIUS))
        if not isinstance(reply, dict) or not reply.get("ok"):
            return set(range(len(specs)))  # unknown links: keep everything
        rows = reply.get("linked") or []
        if isinstance(rows, dict):  # Factorio encodes an empty Lua array as an object
            rows = list(rows.values())
        return {int(i) - 1 for i in rows}

    def _record(self, obs, resource, state, *, refresh=False):
        """The recorded cells of ``resource`` (survey + link check once; ``refresh`` adds cells built since)."""
        entry = state["resources"].get(resource)
        if entry is not None and not refresh:
            return entry
        cells = self._survey(obs, resource)
        if cells is None:
            return entry
        known = {_key(c["drill"]) for c in (entry or {}).get("cells", [])}
        fresh = [c for c in cells if _key(c["drill"]) not in known]
        if entry is not None and not fresh:
            return entry
        links = self._linked([part for c in fresh for part in (c["drill"], c["receiver"])])
        for i, c in enumerate(fresh):
            c["linked"] = 2 * i in links or 2 * i + 1 in links
        entry = entry or {"cells": [], "recorded_tick": obs.get("tick"), "phase": "retiring"}
        entry["cells"] = entry["cells"] + fresh
        state["resources"][resource] = entry
        _atomic_json(self.path, state)
        return entry

    def cell_keys(self, obs) -> set[tuple]:
        """``(name, x, y)`` of the unlinked starter cells of every retired resource: no fuel, no upkeep."""
        retired = self.resources(obs)
        if not retired:
            return set()
        state = self._state(obs["world_id"])
        keys = set()
        for resource in sorted(retired):
            entry = self._record(obs, resource, state)
            for c in (entry or {}).get("cells", []):
                if not c.get("linked"):
                    keys.update({_key(c["drill"]), _key(c["receiver"])})
        return keys

    # ----- job ------------------------------------------------------------------
    def step(self, obs, parameters):
        resource = parameters["resource"]
        world = obs.get("world_id")
        if resource not in self.resources(obs):
            jobs, board = self._inputs(obs)
            return {
                "status": "blocked",
                "reason": "starter_retirement_without_permanent_source",
                "evidence": {
                    "resource": resource,
                    "jobs_prove": resource in retired_by_jobs(jobs),
                    "board_shows": _board_supports(resource, board, world),
                },
            }
        state = self._state(world)
        entry = self._record(obs, resource, state, refresh=True)
        if entry is None:
            return {
                "status": "blocked",
                "reason": "starter_retirement_cell_survey_failed",
                "evidence": {"resource": resource},
            }
        by_key = {_key(e): e for e in obs.get("entities", [])}
        live = []
        for c in entry["cells"]:
            if c.get("linked"):
                continue
            drill, receiver = by_key.get(_key(c["drill"])), by_key.get(_key(c["receiver"]))
            if drill is not None or receiver is not None:
                live.append((c, drill, receiver))
        if not live:
            if obsolete.mark_retired(entry, obs.get("tick")):
                _atomic_json(self.path, state)
            return {
                "status": "succeeded",
                "reason": "starter cells collected and mined; their tiles are free",
                "evidence": self.proof(obs, parameters),
            }
        collect = self._collect(obs, resource, live)
        if collect is not None:
            return collect
        for c, drill, receiver in live:
            target = None
            if drill is not None:
                if _fuel(drill) == 0 and drill.get("status_name") != "working":
                    target = (c, drill)
            elif receiver is not None and self._cold_and_empty(receiver, resource):
                target = (c, receiver)
            if target is None:
                continue
            cell, entity = target
            if self._linked([entity]):  # an inserter was added since recording: keep the cell
                cell["linked"] = True
                _atomic_json(self.path, state)
                return {
                    "status": "waiting",
                    "reason": "starter_retirement_kept_a_linked_cell",
                    "evidence": _key(entity),
                }
            action = obsolete.guarded_mine(entity, world, f"retire cold {resource} starter cell")
            if action is None:
                continue
            return action
        return {
            "status": "waiting",
            "reason": "starter_retirement_waits_for_cells_to_cool",
            "evidence": {
                "resource": resource,
                "cells": len(live),
                "fuel": sum(_fuel(d) for _, d, _ in live if d is not None),
            },
        }

    @staticmethod
    def _stock(receiver, resource):
        if receiver.get("name") == "stone-furnace":
            return int((receiver.get("output_inventory") or {}).get(PLATE_OF.get(resource, ""), 0))
        return int((receiver.get("inventory") or {}).get(resource, 0))

    def _cold_and_empty(self, receiver, resource):
        if receiver.get("name") == "stone-furnace":
            return receiver.get("status_name") != "working" and not sum(
                (receiver.get("output_inventory") or {}).values()
            )
        return self._stock(receiver, resource) == 0

    def _collect(self, obs, resource, live):
        """One large trip of the remaining stock: at COLLECT_BATCH, or whatever is left once the drill stopped."""
        rows = []
        for _, drill, receiver in live:
            if receiver is None or type(receiver.get("unit_number")) is not int:
                continue
            stock = self._stock(receiver, resource)
            stopped = drill is None or (_fuel(drill) == 0 and drill.get("status_name") != "working")
            if stock > 0:
                rows.append((stock, stopped, receiver))
        if not rows:
            return None
        rows.sort(key=lambda r: (-r[0], r[2]["unit_number"]))
        if resource in PLATE_OF:
            total = sum(r[0] for r in rows)
            if total < COLLECT_BATCH and not all(r[1] for r in rows):
                return None
            sources = [
                {
                    "name": r[2]["name"],
                    "position": dict(r[2]["position"]),
                    "unit_number": r[2]["unit_number"],
                    "kind": "output",
                }
                for r in rows[:MAX_SOURCES]
            ]
            return {
                "type": "take_many",
                "item": PLATE_OF[resource],
                "count": min(TAKE_LIMIT, total),
                "world_id": obs["world_id"],
                "sources": sources,
                "reason": f"collect retiring {resource} starter cell stock",
            }
        surplus = getattr(self, "surplus", None)  # carried_surplus.CarriedSurplus
        bounded = surplus is not None and resource in CARRY_BOUNDED
        if bounded:
            from .carried_surplus import allowance, carried

            excess = carried(obs, resource) - allowance(resource, surplus.catalog)
            if excess > 0:  # the previous trip goes straight into consumers or a depot
                action = surplus.sink_action(obs, resource, excess, f"put retiring {resource} starter stock to use")
                if action is not None:
                    return action
        for stock, stopped, chest in rows:
            if stock >= COLLECT_BATCH or stopped:
                count = min(TAKE_LIMIT, stock)
                reason = f"collect retiring {resource} starter cell stock"
                if bounded:
                    room = surplus.trip_room(obs, resource)
                    if room > 0:
                        count = min(count, room)
                    elif not obs.get("inventory_empty_slots"):
                        return {
                            "status": "waiting",
                            "reason": "starter_retirement_waits_for_a_stock_sink",
                            "evidence": {"resource": resource, "carried": carried(obs, resource), "stock": stock},
                        }
                    else:  # nothing accepts it: inventory storage turns the overflow into a depot chest
                        reason += " (no consumer or depot room)"
                return {
                    "type": "take",
                    "name": chest["name"],
                    "position": dict(chest["position"]),
                    "item": resource,
                    "count": count,
                    "reason": reason,
                }
        return None

    def proof(self, obs, parameters):
        """Fresh observation: no unlinked recorded cell of the resource stands any more."""
        resource = parameters["resource"]
        state = self._state(obs.get("world_id"))
        entry = state["resources"].get(resource)
        if entry is None:
            return None
        standing = {_key(e) for e in obs.get("entities", [])}
        left = [
            c["drill"]["position"]
            for c in entry["cells"]
            if not c.get("linked") and (_key(c["drill"]) in standing or _key(c["receiver"]) in standing)
        ]
        if left:
            return None
        return {
            "tick": obs.get("tick"),
            "resource": resource,
            "retired_cells": sum(1 for c in entry["cells"] if not c.get("linked")),
            "kept_linked": sum(1 for c in entry["cells"] if c.get("linked")),
        }
