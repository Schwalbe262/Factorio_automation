"""Remote smelting block: plates made at another ore patch when the home patch can grow no more.

When the plate's ore cannot grow at home (the home patch has no bank site left, or a new
module's ore cannot be routed to the district) the factory grows by itself at the next
patch: one mining block on that patch, a furnace block beside it, and the plates onto the
planned bus (owner rules: plate supply short -> the factory grows; build the permanent
factory; one mining block per patch; trees never move a site).

Pieces (each reuses the shared planner; nothing has fixed coordinates):

* **Patch** (:func:`choose_patches`): the site plan's other patches of the ore, nearest the
  plan start first, each judged by its ore against the block's planned draw: a patch whose
  ore lasts less than ``MIN_LIFE_MINUTES`` at the planned rate, or farther than
  ``MAX_PATCH_DISTANCE``, is refused with that reason (receipt ``patches``).
* **Bank**: ``outposts.MiningOutposts.prepare_on_patch`` (the policy bank, every side of the
  patch, its own power bridge); its output port feeds the furnace block's raw port.
* **Furnace block**: ``planned_furnaces.furnace_block`` (paired bank, reserved growth slots,
  flow-model feed head) on a free site of the site service's surveyed map near the bank's
  port (``site.candidates.free_sites``, trees allowed), turned with ``site.fit.orientations``
  so the raw port faces the bank port, the coal port the coal source and the product port
  the bus feed.
* **Plates**: a *feed* on the built bus trunk of the plate (``planned_tap.feed_layout``),
  nearest the bus head first so every tap of the lane is downstream; the product route
  ends at the feed's port outside the band. The feed is recorded in the bus state with its
  ``supply_per_minute`` and cut over last (``planned_tap.cutover``).
* **Coal**: a splitter tap on a coal patch bank's output trunk (``mine_trunk.plan_tap``,
  demand recorded in ``mine-trunks.json`` so the coal bank grows for it), else a free board
  coal port.
* **Power**: the bank's own bridge; the block's inserter pole by ``power_grid.plan_connection``.

Phases (state ``remote-smelting-<ore>.json``): planned (saved before anything is built and
published on the board), bank, block (furnaces, routes, power), coal tap, bus feed, verified
(the ``planned_furnaces`` production proof plus both cut-overs standing).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json

from . import mine_trunk, pipe_bridge, planned_tap, remedy
from .board import blocks_without_bus, hardware_conflicts, planned_unbuilt_hardware, surface_conflict
from .planned_furnaces import (
    GEOMETRY,
    LAYOUT_POLICY as FURNACE_POLICY,
    SLOTS,
    LENGTH,
    Kind,
    PlannedFurnaces,
    furnace_block,
    head_bandwidth,
    head_choice,
    _connection,
    _route_failure,
)
from .planning import overlap
from .production_sources import free_board_ports
from .smelting import BANK_DEPTH, RESERVED_TILE, growth_keepouts, merge_entities
from .steel_production import smelting_rates
from .transport import VECTORS, xy

PLATES = {"iron-ore": "iron-plate", "copper-ore": "copper-plate"}
CAPABILITY = "remote_smelting"
OWNER_PREFIX = "remote_smelting:"
LAYOUT_POLICY = "remote-smelting-block-v1"
MIN_LIFE_MINUTES = 240  # a patch whose ore feeds the planned bank for less is refused
MAX_PATCH_DISTANCE = 400  # tiles from the plan start (the plan surveys about this far)
MAX_PATCHES = 2  # accepted patches tried per prepare
MAX_DRILLS = 8
BANK_COUNTS_TRIED = 3  # drill counts asked per patch (pairs, down from the ask)
SITES = 3  # free furnace sites tried per orientation size
ALLOCATIONS = 3  # district blocks along the bus tried
FEED_ROWS = 4  # bus feed rows planned per side
ORIENTATIONS = 2  # best turns tried per site
FEEDS = 2  # bus feed rows routed per placement (nearest the block's product port first)
COAL_SOURCES = 2
SITE_STEP = 2
DRILL_RATE_LUA = (
    'return {rate=prototypes.entity["electric-mining-drill"].mining_speed*60/prototypes.entity[%s]'
    ".mineable_properties.mining_time}"
)
# Wait-vs-spend: actor seconds per planned entity and per tile of travel (throughput_growth's scale).
SECONDS_PER_ENTITY = 1.5


def owner(resource: str) -> str:
    return OWNER_PREFIX + resource


def bank_block(resource: str) -> str:
    return OWNER_PREFIX + resource + ":bank"


def state_file(resource: str) -> str:
    return "remote-smelting-%s.json" % resource


def validate_parameters(parameters) -> None:
    if not isinstance(parameters, dict) or set(parameters) - {"resource", "rate_per_minute"}:
        raise ValueError("remote_smelting takes resource and rate_per_minute")
    if parameters.get("resource") not in PLATES:
        raise ValueError("remote_smelting resource must be one of " + ", ".join(sorted(PLATES)))
    rate = parameters.get("rate_per_minute")
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 960:
        raise ValueError("remote_smelting rate_per_minute must be in (0, 960]")


# ----- patch rule (pure) ----------------------------------------------------------------
def choose_patches(patches, rate_per_minute: float, *, min_life=MIN_LIFE_MINUTES, max_distance=MAX_PATCH_DISTANCE):
    """``(accepted, rows)``: patches whose ore lasts and that lie within reach, nearest first.

    ``patches`` are ``mining_site.other_patches`` rows (``amount``, ``distance``). Life is the
    patch's ore over the block's planned ore draw; the nearest acceptable patch wins (its
    belts, poles and walking are the cheapest), a larger amount breaks ties. ``rows`` is the
    receipt: every patch with its life, distance and verdict.
    """
    rows, accepted = [], []
    for p in sorted(patches, key=lambda p: (p.get("distance", math.inf), -(p.get("amount") or 0))):
        amount = p.get("amount")
        life = (amount / rate_per_minute) if isinstance(amount, (int, float)) and rate_per_minute > 0 else None
        row = {
            "centroid": [round(c, 1) for c in p["centroid"]],
            "distance": round(p.get("distance", math.inf), 1),
            "amount": amount,
            "life_minutes": None if life is None else round(life),
        }
        if p.get("distance", math.inf) > max_distance:
            row["verdict"] = "beyond_reach"
        elif life is None or life < min_life:
            row["verdict"] = "ore_too_short_lived"
        else:
            row["verdict"] = "accepted"
            accepted.append(p)
        rows.append(row)
    return accepted, rows


def wait_vs_spend(plan_entities: int, route_tiles: int, *, distance: float) -> dict:
    """Receipt: the block's one-off actor cost against the wait it replaces (no home growth: unbounded)."""
    seconds = plan_entities * SECONDS_PER_ENTITY + 2 * distance / 8.9  # build + a round trip at running speed
    return {
        "wait_seconds": None,
        "wait_reason": "home patch has no growth: without this block the plate deficit stays",
        "spend_seconds": round(seconds),
        "entities": plan_entities,
        "route_tiles": route_tiles,
        "rule": "spend when the wait is unbounded",
    }


def failure_needs(failures, resource: str, product: str) -> dict:
    """``plan_needs`` of a prepare that found no block: the stage that failed most precisely.

    A feed blocked by standing hardware names that hardware (``preflight``: the change is to bridge
    or move it); a route failure names the item (``route``); else no site fitted (``site``)."""
    from . import plan_needs

    reasons = Counter(str(f.get("reason")) for f in failures)
    feed = next((f for f in failures if f.get("reason") == "no_bus_feed_row_for_remote_plates"), None)
    if feed is not None:
        rows = feed.get("feeds") or {}
        top = max(rows, key=lambda k: rows[k].get("count", 0)) if rows else "no_feed_row_on_the_bank_side"
        blockers = (rows.get(top) or {}).get("blockers") or []
        names = ", ".join(sorted({b["entity"] for b in blockers})) or "planned hardware"
        return plan_needs.make(
            "preflight",
            top,
            item=product,
            blockers=blockers,
            rejected_by={k: v.get("count", 0) for k, v in rows.items()},
            change="the %s feed rows on the %s side of the bus are covered by %s: bridge it "
            "(a pipe under a pipe-to-ground pair) or free one feed row" % (product, feed.get("side"), names),
        )
    routed = [f for f in failures if f.get("item")]
    if routed:
        item = Counter(f["item"] for f in routed).most_common(1)[0][0]
        return plan_needs.make(
            "route",
            "remote_smelting_%s_route_failed" % item.replace("-", "_"),
            item=item,
            rejected_by=dict(reasons),
            tried=len(failures),
            change="a %s corridor between the remote %s block and its %s"
            % (item, resource, "bus feed" if item == product else "source"),
        )
    return plan_needs.make(
        "site",
        "no_remote_smelting_block_with_complete_connections",
        item=product,
        rejected_by=dict(reasons),
        tried=len(failures),
        change="a free furnace site beside a remote %s bank with routes to coal and the bus" % resource,
    )


def _feed_hardware(feed) -> list:
    """The feed's entities plus its pipe bridge's pair (built at cut-over, not by the block plan)."""
    return list(feed["entities"]) + list((feed.get("pipe_bridge") or {}).get("add", []))


def _feed_units(feed) -> tuple:
    """Units the feed replaces: the trunk belts, and the bridged pipes."""
    return tuple(feed["remove_units"]) + tuple(
        e["unit_number"] for e in (feed.get("pipe_bridge") or {}).get("remove", [])
    )


def _rect_area(rect) -> list:
    return [[rect.x0, rect.y0], [rect.x1, rect.y1]]


def _point(port) -> tuple:
    p = port["position"] if "position" in port else port
    return (p["x"], p["y"])


def _distance(a, b) -> float:
    a, b = _point(a), _point(b)
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _bus_side(view, point) -> str:
    """``right`` or ``left``: the side of the bus band ``point`` lies on (bus-local lateral)."""
    lateral = view.bus.local_tile(math.floor(_point(point)[0]), math.floor(_point(point)[1]))[1]
    return "right" if lateral >= 0 else "left"


class RemoteSmelting(PlannedFurnaces):
    """One remote smelting block of ``resource``'s plate (module doc)."""

    def __init__(self, game, builder, catalog, *, site, outposts, resource, route=None, power=None, check=None):
        product = PLATES[resource]
        kind = Kind("remote-" + product, owner(resource), resource, product, None, "coal", state_file(resource))
        self.game, self.builder, self.catalog, self.site = game, builder, catalog, site
        self.outposts, self.resource = outposts, resource
        self.kind = kind
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / kind.state_file
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        from .power_grid import plan_connection as power_connection
        from .planning import preflight
        from .transport import plan_connection

        self.route = route or plan_connection
        self.power = power or power_connection
        self.check = check or preflight
        self.last_proof_failure = None

    # ----- planning -----------------------------------------------------------------
    def _drill_rate(self) -> float | None:
        reply = self.game.query(DRILL_RATE_LUA % json.dumps(self.resource))
        rate = reply.get("rate") if isinstance(reply, dict) else None
        return float(rate) if isinstance(rate, (int, float)) and rate > 0 else None

    def prepare(self, obs, parameters=None) -> dict:
        """Read-only plan of the whole block (module doc): a state dict or a failure with ``needs``."""
        from .mining_site import other_patches

        parameters = parameters or {}
        target = float(parameters.get("rate_per_minute", 0)) or 1.0
        kind = self.kind
        if self.site is None:
            return self._fail("remote_smelting_requires_site_service")
        board = self._board(obs)
        if board is None:
            return self._fail("remote_smelting_board_identity_changed")
        bus = planned_tap.published(board, obs["world_id"])
        if bus is None or not planned_tap.carries(bus, kind.product):
            return self._fail("remote_smelting_requires_a_built_bus_trunk", item=kind.product)
        span = planned_tap.max_span(self.game)
        if span is None:
            return self._fail("planned_bus_underground_span_unknown")
        per_drill = self._drill_rate()
        if per_drill is None:
            return self._fail("mining_rate_query_failed")
        drills = remedy.scaled_count(min(MAX_DRILLS, max(2, math.ceil(target / per_drill))))
        patches = other_patches(self.site, obs["world_id"], self.resource)
        accepted, patch_rows = choose_patches(patches, drills * per_drill)
        if not accepted:
            return self._fail(
                "no_remote_patch_with_lasting_ore_within_reach",
                patches=patch_rows,
                plan_needs={
                    "kind": "site",
                    "reason": "no_remote_patch_with_lasting_ore_within_reach",
                    "item": self.resource,
                    "change": "a surveyed patch of %s with %d+ minutes of ore within %d tiles"
                    % (self.resource, MIN_LIFE_MINUTES, MAX_PATCH_DISTANCE),
                },
            )
        failures = []
        for patch in accepted[:MAX_PATCHES]:
            bank = self._bank_on(obs, patch, drills)
            if not bank.get("ok"):
                failures.append({"patch": [round(c, 1) for c in patch["centroid"]], "reason": bank.get("reason")})
                continue
            planned = self._block_for_bank(obs, board, bus, bank, per_drill, span, patch, failures)
            if planned is not None:
                planned["patches"] = patch_rows
                return planned
        distinct = [f for i, f in enumerate(failures) if f not in failures[:i]][:8]
        return self._fail(
            "no_remote_smelting_block_with_complete_connections",
            patches=patch_rows,
            failures=distinct,
            plan_needs=failure_needs(failures, self.resource, kind.product),
        )

    def _bank_on(self, obs, patch, drills: int) -> dict:
        """The bank with the most drills up to ``drills`` on ``patch`` (``BANK_COUNTS_TRIED`` counts, pairs).

        A small patch may hold no frame for the full count; the policy planner then falls back to a
        smaller frame (eval-1575138435: 8 drills asked on the 115-tile patch gave 2 built, 6 asked
        gave 6), so fewer pairs are asked for until the built count reaches the ask."""
        best = None
        count = drills + drills % 2
        for _ in range(BANK_COUNTS_TRIED):
            bank = self.outposts.prepare_on_patch(obs, self.resource, count, patch)
            if bank.get("ok"):
                built = sum(e["name"] == "electric-mining-drill" for e in bank["plan"]["entities"])
                if best is None or built > best[0]:
                    best = (built, bank)
                if built >= count:
                    break
            elif best is None:
                best = (0, bank)
            count -= 2
            if count < 2:
                break
        return best[1]

    def _sites(self, obs, port, avoid):
        """Free rects for the block in both turns, nearest ``port`` first, off every reserved area."""
        from .site.candidates import free_sites

        try:
            world = self.site.world(obs["world_id"])
        except Exception:  # noqa: BLE001 - no surveyed map: no site
            return []
        point = (port["position"]["x"], port["position"]["y"])
        found = []
        for size in ((LENGTH, BANK_DEPTH), (BANK_DEPTH, LENGTH)):
            rects = free_sites(world, [point], *size, step=SITE_STEP, limit=4 * SITES, margin=1)
            kept = [r for r in rects if not any(overlap(_rect_area(r), area) for area in avoid)]
            found += kept[:SITES]
        return sorted(found, key=lambda r: abs(r.x0 + r.width / 2 - point[0]) + abs(r.y0 + r.height / 2 - point[1]))

    def _avoid(self, board, bus, bank) -> list:
        from .mining_site import plan_zone_areas

        areas = [bank["reserved_area"]] + list(bank.get("reserved_rects") or [])
        areas += [
            b["reserved_area"]
            for b in board.get("blocks", {}).values()
            if isinstance(b, dict) and b.get("reserved_area")
        ]
        areas += [r["area"] for r in bus.get("reservations", []) if r.get("area")]
        areas += [area for _, area in plan_zone_areas(self.site, board.get("world_id"))]
        return [a for a in areas if isinstance(a, (list, tuple)) and len(a) == 2]

    def _feeds(self, obs, bus, span, blocked=None, sides=("right", "left")):
        """Feed rows of the plate on both sides whose tiles are free (only the trunk belts they replace).

        ``blocked`` (a dict) collects ``reason -> {count, blockers}`` of the refused rows: a feed whose
        port or branch meets standing hardware (eval-1575138435: the crude pipeline along the east
        edge of the bus sat on every east port tile) names that hardware."""
        view = planned_tap.BusView(bus)
        live = {(e["name"], *xy(e)): e for e in obs["entities"]}
        tiles = {}
        for e in obs["entities"]:
            for tile in GEOMETRY._occupied_by_plan([e]):
                tiles[tile] = e
        blocks = planned_tap._board_blocks(self.game, obs)
        name = planned_tap.tap_name(self.kind.owner, self.kind.product)
        blocked = {} if blocked is None else blocked
        rows = []

        def refuse(reason, blocker=None):
            row = blocked.setdefault(reason, {"count": 0, "blockers": []})
            row["count"] += 1
            if blocker is not None and len(row["blockers"]) < 4:
                row["blockers"].append({"entity": blocker["name"], "position": dict(blocker["position"])})
            return False

        axis = VECTORS[view.facing]

        def accept_with(bridging):
            def accept(row):
                units = [
                    live.get(("transport-belt", *xy({"position": p})), {}).get("unit_number") for p in row["remove"]
                ]
                if not all(type(u) is int for u in units):
                    return refuse("feed_trunk_belt_not_observed")
                removed = {tuple(xy({"position": p})) for p in row["remove"]}
                hits = {}
                for e in row["entities"]:
                    for tile in GEOMETRY._occupied_by_plan([e]):
                        other = tiles.get(tile)
                        if other is not None and tuple(xy(other)) not in removed:
                            hits[tuple(xy(other))] = other
                bridge = None
                if hits:
                    if not bridging or len(hits) != 1 or next(iter(hits.values()))["name"] != pipe_bridge.PIPE:
                        return refuse("feed_meets_standing_hardware", next(iter(hits.values())))
                    bridge = pipe_bridge.plan(obs, next(iter(hits)), axis)
                    if not bridge.get("ok"):
                        return refuse(bridge["reason"], next(iter(hits.values())))
                extra = bridge["add"] if bridge else []
                if surface_conflict(self.builder, obs, blocks, row["entities"] + extra, exclude=(self.kind.owner,)):
                    return refuse("feed_meets_planned_hardware")
                row["remove_units"] = units
                if bridge is not None:
                    row["pipe_bridge"] = bridge
                return True

            return accept

        for side in sides:
            # A port beyond a pipe beside the band is reached once the pipe is bridged (``pipe_bridge``).
            for reach, bridging in ((0, False), (1, True)):
                found, _ = planned_tap.feed_candidates(
                    view, self.kind.product, side, span=span, limit=FEED_ROWS, accept=accept_with(bridging), reach=reach
                )
                rows += [dict(row, name=name, owner=self.kind.owner, demand_per_minute=0, built=False) for row in found]
                if found:
                    break
        return rows

    def _coal_sources(self, obs, board, demand):
        """Coal trunk taps (patch banks of coal) first, then free board coal ports: ``[(kind, source, tap)]``."""
        sources = []
        mining = self.outposts.state if self.outposts.state.get("world_id") == obs["world_id"] else {}
        bank = mine_trunk.patch_bank(mining.get("banks"), "coal", mine_trunk.patch_seed(obs, "coal"))
        name = "%s:coal" % self.kind.owner
        if bank is not None:
            key = mine_trunk.trunk_key(bank)
            blocks = blocks_without_bus(board)

            def accept(tap):
                if surface_conflict(self.builder, obs, blocks, tap["entities"]) is not None:
                    return False
                check = self.check(
                    self.game, self.builder, obs, tap["entities"], ignored_units=tuple(tap["remove_units"])
                )
                return bool(check.get("ok"))

            planned = mine_trunk.plan_tap(
                bank,
                obs,
                owner=self.kind.owner,
                name=name,
                demand=demand,
                serve="after",
                stations=mine_trunk.stations(self.root, obs["world_id"], key, exclude=name),
                accept=accept,
            )
            for tap in (planned.get("taps") or [])[:COAL_SOURCES]:
                sources.append(("mine_trunk_tap", dict(tap["port"], name=name), tap))
        for row in free_board_ports(board, "coal", exclude=(self.kind.owner,))[:COAL_SOURCES]:
            sources.append(("board_port", row["port"], None))
        return sources

    def _block_for_bank(self, obs, board, bus, bank, per_drill, span, patch, failures):
        kind = self.kind
        drills = sum(e["name"] == "electric-mining-drill" for e in bank["plan"]["entities"])
        ore_rate = drills * per_drill
        try:
            probe = smelting_rates(self.catalog, kind.raw, kind.product, 2)
        except (KeyError, TypeError, ValueError) as exc:
            return self._fail_row(failures, "remote_smelting_recipe_unsupported", detail=str(exc)[:200])
        per_furnace = float(probe["raw_per_minute"]) / 2
        per_row = max(1, min(SLOTS, math.ceil(ore_rate / (2 * per_furnace) - 1e-9)))
        rates = smelting_rates(self.catalog, kind.raw, kind.product, 2 * per_row)
        choice = head_choice(kind, rates)
        source = bank["plan"]["ports"][0]
        # No belt crosses the bus band: the block, its feed and the bank share one side of it.
        side = _bus_side(planned_tap.BusView(bus), source)
        blocked = {}
        feeds = self._feeds(obs, bus, span, blocked, sides=(side,))
        if not feeds:
            return self._fail_row(
                failures, "no_bus_feed_row_for_remote_plates", item=kind.product, side=side, feeds=blocked
            )
        coal_rate = float(rates.get("coal_per_minute_at_max_catalog_usage") or 0)
        coal = self._coal_sources(obs, board, coal_rate)
        if not coal:
            return self._fail_row(failures, "no_coal_source_for_remote_furnaces")
        sites = self._candidate_sites(obs, board, bus, bank, feeds)
        if not sites:
            return self._fail_row(failures, "no_free_remote_furnace_site", port=source["position"])
        others = blocks_without_bus(board)
        unbuilt = planned_unbuilt_hardware(GEOMETRY, obs, others, exclude=(kind.owner, bank_block(self.resource)))
        coal_point = _point(coal[0][1])
        bank_point = _point(source)

        def build(x0, y0):
            return furnace_block(kind, x0, y0, per_row, head=choice["head"])

        from .site.fit import orientations

        for rect, allocation, feed_rows in sites:
            feed_point = _point(feed_rows[0]["port"])

            def wants(plan, feed_point=feed_point):
                raw_port, coal_port, out_port = plan["ports"]
                return [(raw_port, bank_point, "in"), (coal_port, coal_point, "in"), (out_port, feed_point, "out")]

            for placement in orientations(build, (LENGTH, BANK_DEPTH), rect, wants)[:ORIENTATIONS]:
                block = dict(placement.plan, site_transform=placement.transform.to_dict())
                out = block["ports"][2]
                ranked = sorted(feed_rows, key=lambda row: _distance(row["port"], out))[:FEEDS]
                result = self._connect_remote(obs, bank, block, ranked, coal, others, unbuilt)
                if not result["ok"]:
                    failures.append(
                        {
                            "site": [rect.x0, rect.y0],
                            "allocation": allocation,
                            **{k: result.get(k) for k in ("reason", "item") if result.get(k)},
                        }
                    )
                    continue
                if self.site is not None:
                    from .site.placement import settle

                    settle(self.site, obs["world_id"], kind.owner, allocation)
                plan_entities = len(result["plan"]["entities"]) + len(bank["plan"]["entities"])
                return {
                    "allocation": allocation,
                    **result,
                    "ok": True,
                    "world_id": obs["world_id"],
                    "catalog_fingerprint": self.catalog.fingerprint,
                    "kind": kind.name,
                    "owner": kind.owner,
                    "resource": self.resource,
                    "target_per_minute": round(drills * per_drill, 3),
                    "patch": {k: patch.get(k) for k in ("centroid", "bbox", "distance", "amount", "tile_count")},
                    "mining_bank": bank,
                    "ore_per_minute": round(ore_rate, 3),
                    "transform": placement.transform.to_dict(),
                    "bank": block,
                    "rated": rates,
                    "raw_per_craft": next(
                        i["amount"] for i in self.catalog.recipes[kind.product]["ingredients"] if i["name"] == kind.raw
                    ),
                    "per_row": per_row,
                    "bandwidth": {
                        **choice,
                        "findings": head_bandwidth(kind, rates, choice["head"])["findings"],
                        "scope": "canonical block at full reserved growth through belt_flow",
                    },
                    "wait_vs_spend": wait_vs_spend(
                        plan_entities, result["route_tiles"], distance=float(patch.get("distance") or 0)
                    ),
                    "phase": "planned",
                }
        if self.site is not None and any(allocation for _, allocation, _ in sites):
            from .site.placement import settle

            settle(self.site, obs["world_id"], kind.owner, None)
        return None

    def _candidate_sites(self, obs, board, bus, bank, feeds):
        """``[(rect, allocation name or None, feed rows on the block's side)]``, shortest belts first.

        Two sources, both from the site service: district blocks along the bus (``allocate_candidates``,
        short plate belts, a long ore belt) and free land beside the bank's port (``free_sites`` on the
        surveyed map, a short ore belt), on the bank's side of the bus band only (no belt crosses it).
        Ranked by ore plus plate belt length (Manhattan)."""
        source = bank["plan"]["ports"][0]
        view = planned_tap.BusView(bus)
        side = _bus_side(view, source)
        rows = []
        try:
            blocks = self.site.allocate_candidates(
                "production",
                LENGTH,
                BANK_DEPTH,
                name=self.kind.owner,
                count=ALLOCATIONS,
                returns_to_bus=True,
                world_id=obs["world_id"],
            )
        except Exception:  # noqa: BLE001 - the allocator reports, never crashes the job
            blocks = []
        candidates = [(b.rect, b.name) for b in blocks]
        candidates += [(r, None) for r in self._sites(obs, source, self._avoid(board, bus, bank))]
        for rect, name in candidates:
            centre = {"x": rect.x0 + rect.width / 2, "y": rect.y0 + rect.height / 2}
            if _bus_side(view, centre) != side:
                continue
            mine = [row for row in feeds if row["side"] == side]
            if not mine:
                continue
            belts = _distance(centre, source["position"]) + min(
                _distance(centre, row["port"]["position"]) for row in mine
            )
            rows.append((belts, rect, name, mine))
        rows.sort(key=lambda row: row[0])
        return [(rect, name, mine) for _, rect, name, mine in rows[: SITES + ALLOCATIONS]]

    def _fail_row(self, failures, reason, **extra):
        failures.append({"reason": reason, **extra})
        return None

    def _connect_remote(self, obs, bank, block, feeds, coal, others, unbuilt) -> dict:
        """Raw route, coal tap/route, bus feed route, power and preflight for one turned block."""
        kind = self.kind
        growth = block["growth_entities"]
        bank_entities = list(bank["plan"]["entities"])
        if hardware_conflicts(GEOMETRY, block["entities"] + growth, unbuilt):
            return {"ok": False, "reason": "furnace_block_meets_planned_hardware"}
        if overlap(block["reserved_area"], bank["reserved_area"]) or any(
            overlap(block["reserved_area"], b["reserved_area"])
            for name, b in others.items()
            if isinstance(b, dict) and b.get("reserved_area") and name not in (kind.owner, bank_block(self.resource))
        ):
            return {"ok": False, "reason": "board_reservation_overlap"}
        keepouts = growth_keepouts(GEOMETRY, [block])
        reserved_growth = {xy(e) for e in keepouts}
        raw_port, coal_port, out_port = block["ports"]
        entities = list(block["entities"])
        routes, connections = [], []
        source = bank["plan"]["ports"][0]
        raw_route = self.route(self.game, self.builder, obs, source, raw_port, entities + keepouts + bank_entities)
        if not raw_route.get("ok"):
            return _route_failure(raw_route, kind.raw)
        entities = merge_entities(entities, raw_route["entities"])
        routes.append(raw_route)
        connections.append(_connection(kind.raw, source, raw_port, kind.owner))
        feed, out_route, last = None, None, None
        for row in feeds:
            reserved = entities + keepouts + bank_entities + _feed_hardware(row)
            found = self.route(
                self.game, self.builder, obs, out_port, row["port"], reserved, ignored_units=_feed_units(row)
            )
            if found.get("ok"):
                feed, out_route = row, found
                break
            last = found
        if feed is None:
            return _route_failure(last or {}, kind.product)
        entities = merge_entities(entities, out_route["entities"])
        routes.append(out_route)
        connections.append(_connection(kind.product, out_port, feed["port"], kind.owner))
        ignored = list(_feed_units(feed))
        coal_row, coal_route, last = None, None, None
        for row in coal:
            source_kind, coal_from, tap = row
            extra = list(tap["entities"]) if tap else []
            found = self.route(
                self.game,
                self.builder,
                obs,
                coal_from,
                coal_port,
                entities + keepouts + bank_entities + _feed_hardware(feed) + extra,
                ignored_units=tuple(ignored + list((tap or {}).get("remove_units", []))),
            )
            if found.get("ok"):
                coal_row, coal_route = row, found
                break
            last = found
        if coal_row is None:
            return _route_failure(last or {}, "coal")
        source_kind, coal_from, coal_tap = coal_row
        entities = merge_entities(entities, coal_route["entities"])
        routes.append(coal_route)
        connections.append(_connection("coal", coal_from, coal_port, kind.owner))
        if coal_tap:
            ignored += list(coal_tap["remove_units"])
        pole = next(e for e in block["entities"] if e["name"] == "small-electric-pole")
        power = self.power(
            self.game,
            self.builder,
            obs,
            pole["position"],
            [e for e in entities if e["name"] != "small-electric-pole"] + keepouts + bank_entities,
        )
        if not power.get("ok"):
            return {"ok": False, "reason": power.get("reason", "power_failed")}
        entities = merge_entities(entities, [e for e in power["entities"] if e["name"] != RESERVED_TILE])
        cut = _feed_hardware(feed) + (list(coal_tap["entities"]) if coal_tap else [])
        try:  # a route's end belt is the feed's (or the coal tap's) port belt: the same entity once
            whole = merge_entities(entities, cut)
        except ValueError:
            return {"ok": False, "reason": "whole_prospective_plan_overlaps"}
        boxes = [GEOMETRY._occupied_by_plan([e]) for e in whole]
        if sum(map(len, boxes)) != len(set().union(*boxes)):
            return {"ok": False, "reason": "whole_prospective_plan_overlaps"}
        if set().union(*boxes) & reserved_growth:
            return {"ok": False, "reason": "plan_entity_in_reserved_growth_slot"}
        check = self.check(self.game, self.builder, obs, whole, ignored_units=tuple(ignored))
        if not check.get("ok"):
            return {"ok": False, "reason": check.get("reason", "preflight_failed")}
        obstacles = {
            (e["name"], *xy(e)): e
            for r in routes + [check]
            for e in r.get("clear_obstacles", [])
            if isinstance(e, dict)
        }
        pairs = [p for r in routes for p in r.get("underground_pairs", [])]
        plan = _plan(
            entities,
            [raw_port, coal_port, out_port],
            reserved_area=block["reserved_area"],
            underground_pairs=pairs + list(block.get("underground_pairs", [])),
            layout_policy=FURNACE_POLICY,
            recipe=kind.product,
            product=kind.product,
            raw=kind.raw,
            growth_area=block["growth_area"],
            growth_entities=growth,
            site_transform=block["site_transform"],
            clear_obstacles=list(obstacles.values()),
            clearance_world_id=obs["world_id"],
        )
        rated = smelting_rates(self.catalog, kind.raw, kind.product, max(2, 2 * block["per_side_count"]))
        feed = dict(feed, supply_per_minute=round(float(rated["product_per_minute"]), 3))
        return {
            "ok": True,
            "plan": plan,
            "taps": [feed],
            "coal": {"kind": source_kind, "source": coal_from, "tap": coal_tap},
            "connections": connections,
            "sources": {
                "raw": {"item": kind.raw, "kind": "remote_bank", "position": dict(source["position"])},
                "coal": {"item": "coal", "kind": source_kind, "position": dict(coal_from["position"])},
            },
            "route_metrics": [r.get("route_metrics") for r in routes],
            "route_tiles": sum(len(r.get("entities", [])) for r in routes),
        }

    # ----- execution ----------------------------------------------------------------
    def _ensure(self, obs, parameters) -> dict | None:
        """Plan once per world (saved before anything is built); a blocked status or None."""
        if not self.state:
            prepared = self.prepare(obs, parameters)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        if not self._current(obs):
            return {"status": "blocked", "reason": "remote_smelting_world_or_catalog_changed", "kind": self.kind.name}
        return None

    def _coal_block(self) -> tuple[str, dict] | None:
        tap = (self.state.get("coal") or {}).get("tap")
        if not tap:
            return None
        plan = mine_trunk.tap_plan(tap)
        return self.kind.owner + ":coal_tap", dict(
            plan, reserved_area=mine_trunk.tap_area([e for e in tap["entities"] if e is not tap["splitter"]])
        )

    def _publish(self, obs) -> dict | None:
        """Board blocks (furnace block, bank), connections, the coal tap and the bus feed (idempotent)."""
        path = self.root / "pcb-board.json"
        board = self._board(obs)
        if board is None:
            return {"status": "blocked", "reason": "remote_smelting_board_identity_changed"}
        bank = self.state["mining_bank"]
        wanted = {
            self.kind.owner: dict(self.state["plan"], owner=CAPABILITY),
            bank_block(self.resource): dict(
                bank["plan"], reserved_area=bank["reserved_area"], owner=CAPABILITY, mining_resource=self.resource
            ),
        }
        changed = False
        for name, plan in wanted.items():
            if board["blocks"].get(name) != plan:
                board["blocks"][name] = plan
                changed = True
        for connection in self.state["connections"]:
            if connection not in board.setdefault("connections", []):
                board["connections"].append(connection)
                changed = True
        if changed:
            _atomic_json(path, board)
        tap = (self.state.get("coal") or {}).get("tap")
        if tap:
            blocked = mine_trunk.record_tap(self.root, obs["world_id"], tap, block=self._coal_block())
            if blocked is not None:
                return blocked
        return planned_tap.publish(self.root, obs["world_id"], self.state["taps"])

    def step(self, obs, parameters=None) -> dict:
        """Plan, publish, then build: bank, block, coal tap, pipe bridge, bus feed; then the proof."""
        parameters = parameters or {}
        if not (obs.get("enabled_recipes") or {}).get(self.kind.product):
            return {"status": "blocked", "reason": "native_furnace_recipe_locked", "recipe": self.kind.product}
        blocked = self._ensure(obs, parameters)
        if blocked is None:
            blocked = self._publish(obs)
        if blocked is not None:
            return blocked
        bank = self.state["mining_bank"]
        for plan in (bank["plan"], self.state["plan"]):
            result = self.builder.ensure_plan(obs, plan)
            if result.get("status") != "succeeded":
                return result
        tap = (self.state.get("coal") or {}).get("tap")
        if tap and not tap.get("built"):
            mine_trunk.release_trunk_belts(self.root, obs["world_id"], tap)
            action = mine_trunk.cutover(self.builder, obs, tap)
            if tap.get("built"):
                mine_trunk.record_tap(self.root, obs["world_id"], tap)
                self._save()
            if action is not None:
                return action
        feed = self.state["taps"][0]
        bridge = feed.get("pipe_bridge")
        if bridge and not feed.get("built"):
            blocked = pipe_bridge.release(self.root, obs["world_id"], bridge)
            if blocked is not None:
                return blocked
            action = pipe_bridge.step(self.builder, obs, bridge)
            if action is not None:
                return action
            pipe_bridge.bind(self.root, obs["world_id"], bridge, obs)
        was_built = bool(feed.get("built"))
        action = planned_tap.cutover(self.root, self.builder, obs, self.state["taps"])
        if feed.get("built") and not was_built:
            self._save()
        if action is not None:
            return action
        evidence = self.proof(obs)
        if evidence is None:
            return {
                "status": "waiting",
                "reason": f"waiting for remote {self.kind.product} on the bus feed",
                "detail": self.last_proof_failure,
            }
        if self.state.get("phase") != "verified":
            self.state["phase"] = "verified"
            self._save()
        return {
            "status": "succeeded",
            "reason": f"remote smelting block feeds {self.kind.product} onto the planned bus",
            "evidence": evidence,
        }

    def proof(self, obs, parameters=None) -> dict | None:
        """The furnace block's production proof, the bank powered, the coal tap and the bus feed standing."""
        evidence = super().proof(obs)
        if evidence is None:
            return None
        bank = self.state["mining_bank"]
        if not self.outposts.bank_powered(obs, bank["plan"]):
            self.last_proof_failure = {"reason": "remote_bank_not_powered"}
            return None
        tap = (self.state.get("coal") or {}).get("tap")
        if tap and not mine_trunk.tap_standing(obs, tap):
            self.last_proof_failure = {"reason": "remote_coal_tap_not_standing"}
            return None
        if not planned_tap._present(obs, self.state["taps"]):
            self.last_proof_failure = {"reason": "remote_bus_feed_not_standing"}
            return None
        feed = self.state["taps"][0]
        return dict(
            evidence,
            resource=self.resource,
            bus_feed={"name": feed["name"], "along": feed["along"], "supply_per_minute": feed.get("supply_per_minute")},
            ore_per_minute=self.state.get("ore_per_minute"),
            scope="remote furnace block commissioning onto the planned bus; nameplate is not a measured rate",
        )


# ----- planner: when the home patch cannot grow -------------------------------------------
NODE_PREFIX = "auto_remote_smelting_"
NODE_PRIORITY = -52  # throughput_growth.CRITICAL_PRIORITY: it replaces the plate package that cannot land
HOME_EXHAUSTED = frozenset(
    {
        "no_clean_extra_bank_connection_to_owned_ore_trunk",
        "paired_mining_reserved_columns_exhausted",
        "no_policy_compliant_paired_bank_with_growth_and_power",
    }
)
MIN_RATE, MAX_RATE = 60.0, 240.0
OPEN = frozenset({"pending", "ready", "running", "waiting"})


def remote_smelting_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One ``remote_smelting`` node per plate ore whose home growth is blocked for want of a site.

    Trigger: a ``mining_outpost`` job of a plate ore blocked with a home-exhausted reason (no bank
    site left on the patch, or the new module's ore has no corridor to the district). The node
    asks for the missing ore draw (target minus what the job proved, within ``MIN_RATE`` ..
    ``MAX_RATE``). Never twice: an open or unfinished job / queued node of the capability and ore
    stops it (the ladder and auto-retry own a blocked one)."""
    if CAPABILITY not in capabilities:
        return []
    jobs = list(jobs)
    succeeded = {j.get("capability") for j in jobs if j.get("status") == "succeeded"}
    if not set(capabilities[CAPABILITY].get("requires", ())) <= succeeded:
        return []  # no planned bus or mine yet: the remote block has nowhere to put its plates
    nodes = []
    for resource in sorted(PLATES):
        blocked = [
            j
            for j in jobs
            if j.get("capability") == "mining_outpost"
            and (j.get("parameters") or {}).get("resource") == resource
            and j.get("status") == "blocked"
            and (
                j.get("reason") in HOME_EXHAUSTED
                or (j.get("evidence") or {}).get("upstream_extension_failure") in HOME_EXHAUSTED
            )
        ]
        if not blocked:
            continue

        def mine(row, resource=resource):
            return row.get("capability") == CAPABILITY and (row.get("parameters") or {}).get("resource") == resource

        if any(mine(n) and n.get("status", "pending") in OPEN for n in queue_nodes):
            continue
        if any(mine(j) and j.get("status") != "succeeded" for j in jobs):
            continue
        job = max(blocked, key=lambda j: float((j.get("parameters") or {}).get("rate_per_minute", 0)))
        target = float(job["parameters"].get("rate_per_minute", 0))
        proven = float((job.get("evidence") or {}).get("dry_run_active_rate") or 0) or target / 2
        rate = round(min(MAX_RATE, max(MIN_RATE, target - proven)))
        node_id = f"{NODE_PREFIX}{resource}_{rate}"
        if any(n.get("id") == node_id for n in queue_nodes):
            continue
        nodes.append(
            {
                "id": node_id,
                "capability": CAPABILITY,
                "parameters": {"resource": resource, "rate_per_minute": rate},
                "priority": NODE_PRIORITY,
                "depends_on": [],
                "reason": f"{resource} cannot grow at home ({job.get('reason')}): smelt at the next patch",
            }
        )
    return nodes
