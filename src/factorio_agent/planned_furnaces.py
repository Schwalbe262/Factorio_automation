"""Steel and stone-brick furnace blocks from the site plan, on any seed.

Capabilities on a world with a site service and a published planned bus
(``IndustrialOperations`` and ``SharedCoalTap`` delegate here):
``steel_supply``, ``steel_production``, ``steel_coal_supply`` (kind
``steel``) and ``brick_production`` (kind ``brick``). The development-map
path (``industrial_site_anchors``, ``bus_tap.BusTap``, ``SharedCoalTap``)
stays for worlds without a planned bus only.

Layout: one paired furnace bank, ``smelting.paired_furnace_bank`` with the
steel or brick recipe (the bank the smelting district uses; the raw item
takes the ore's place). Canonical frame, band ``[x0, x0 + LENGTH) x
[y0, y0 + 12)``, ``LENGTH = 30``, ``SLOTS = 6`` furnace slots per row, slot
``i`` at ``x0 + 9 + 3 i`` (R raw port facing east, C coal port facing
north, P product port facing east with both lanes, S splitter, F built
furnace, i inserter, + pole, ``:`` reserved growth slot)::

    R>>>v.........................
    ..>>>S>>>>:::::::::::::::.....
    ...^.S>v.i..:..:..:..:..:.....
    ...C...vFF.::.::.::.::.::.....
    .......vFF+:::::::::::::::....
    .......v.i..:..:..:..:..:.....
    .......v.>>>>>>>>>>>>>>>>>>>>P
    .......v.i..:..:..:..:..:.....
    .......vFF+:::::::::::::::....
    .......vFF.::.::.::.::.::.....
    .......v.i..:..:..:..:..:.....
    .......>>>:::::::::::::::.....

That is the shared feed head (raw and coal side-loaded onto one belt, then split: a quarter
belt of raw per row). A new block takes the head the lane flow model rates best at full
reserved growth (``head_choice``; ``split_feed_head`` splits raw and coal first and gives each
row a full lane of both; docs/v2/LAYOUT_PRINCIPLES.md "Belt lanes and bandwidth"). Growth stops
where the head cannot feed the next slot (``feed_shortfalls``).

Site: one ``SiteService`` district block ``LENGTH`` along the bus by 12 deep
(``returns_to_bus=True``: the product leaves at the downstream end on the bus
edge, ready for a later steel bus lane), turned with ``site.fit.orientations``
so the raw port faces where its material comes from (the tap band for
steel, the stone port for bricks), the coal port faces the coal outlet and
the product port the block's output point. Mirrored turns are allowed: a
mirrored bank only swaps which lane carries coal (``feed_lanes_ok``).

Inputs: steel's iron plates come from a planned-bus tap planned with the
block (``planned_tap.connect_inputs``, the call red science and ``produce``
use); stone and coal come from the dedicated material banks' published
ports (``material-sources.json``: ``coal`` -> steel, ``stone`` and
``brick-coal`` -> bricks), routed with ``transport.plan_connection`` and
recorded as board ``connections`` (the port is then taken). The bank's
reserved growth tiles are hard for every route and the power bridge
(``smelting.growth_keepouts``); a plan entity on one is refused.

Staging: both rows reserve ``SLOTS`` slots (``growth_entities`` on the
board block, so other planners treat them as planned hardware);
``START_PER_ROW`` per row are built. ``grown`` adds reserved slots in place
(nothing moves, no route changes); ``growth_evidence`` is the rule (every
built furnace holds raw for two crafts, raw backs up at the raw port, the
product port is not backed up: someone takes the product).

Publication: the complete plan (bank, routes, power bridge) is written to
board block ``<owner>`` and the taps to the bus state (``planned_tap.publish``)
whenever ``step`` runs with a saved plan, before anything is built.

Phases (state ``planned-<kind>.json``): steel ``supply`` (plan, tap cut-over,
iron route; proof: iron at the raw port), ``production`` (needs the
steel-plate recipe; the whole plan; proof: every furnace made steel, steel
on the product port, one raw item and coal on the two feed lanes), coal
(proof: the coal port belt carries coal from the registered ``coal``
outlet, unchanged identity). Bricks: one ``production`` phase.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.core.deterministic_state import _atomic_json

from . import planned_tap
from .bandwidth_audit import SHORTFALL as BANDWIDTH_SHORTFALL
from .bandwidth_audit import furnace_bank, rated_per_furnace
from .feed_head import rank_heads, split_feed_head, with_head  # noqa: F401 - split_feed_head re-exported
from .board import blocks_without_bus, hardware_conflicts, planned_unbuilt_hardware
from .planning import overlap, preflight
from .power_grid import plan_connection as power_connection
from .site.fit import orientations
from .site.placement import settle
from .smelting import (
    BANK_DEPTH,
    RESERVED_TILE,
    feed_lanes_ok,
    grown_bank,
    growth_keepouts,
    merge_entities,
    native_feed_lanes,
    paired_furnace_bank,
)
from .steel_production import IDLE_FURNACE_WITNESS_LUA, automatic_furnace_witness, smelting_rates
from .transport import plan_connection, underground_proof, xy

SLOTS = 6  # reserved furnace slots per row
LENGTH = 3 * SLOTS + 12  # the shortest paired bank with SLOTS slots (smelting.paired_furnace_bank)
START_PER_ROW = 1  # two furnaces built at first
CANDIDATES = 4  # allocator blocks tried
ORIENTATIONS = 2  # best turns tried per block
LAYOUT_POLICY = "planned-furnace-block-v1"
ORIENTED = ("transport-belt", "underground-belt", "splitter", "inserter", "fast-inserter")
# Growth evidence: items on one belt tile (both lanes) that count as backed up.
BELT_BACKUP = 6


@dataclass(frozen=True)
class Kind:
    name: str
    owner: str  # board block and tap owner
    raw: str  # input item of the recipe
    product: str
    raw_key: str | None  # material-sources.json key of the raw input; None: a planned-bus tap
    coal_key: str  # material-sources.json key of the coal outlet
    state_file: str


KINDS = {
    "steel": Kind("steel", "steel_production", "iron-plate", "steel-plate", None, "coal", "planned-steel.json"),
    "brick": Kind("brick", "brick_production", "stone", "stone-brick", "stone", "brick-coal", "planned-brick.json"),
}
GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


def furnace_block(kind: Kind, x0: int, y0: int, per_row: int = START_PER_ROW, head: str = "shared") -> dict:
    """Canonical block: the paired bank with ``kind``'s recipe and its reserved area (module doc).

    ``head`` "split" replaces the bank's shared feed head by ``split_feed_head``; which head
    a new block gets is decided by the flow model (``head_choice``)."""
    bank = paired_furnace_bank(x0, y0, per_row, kind.raw, slots=SLOTS, length=LENGTH, product=kind.product)
    with_head(bank, x0, y0, head)
    bank["reserved_area"] = [[x0, y0], [x0 + LENGTH, y0 + BANK_DEPTH]]
    bank["kind"] = kind.name
    return bank


def head_bandwidth(kind: Kind, rates: dict, head: str) -> dict:
    """Flow-model audit of one feed head at full reserved growth (every slot built)."""
    bank = furnace_block(kind, 0, 0, SLOTS, head)
    count = max(1, int(rates.get("furnace_count") or 1))
    return furnace_bank(
        bank["entities"],
        bank["ports"],
        raw=kind.raw,
        raw_per_furnace=float(rates["raw_per_minute"]) / count,
        coal_per_furnace=float(rates.get("coal_per_minute_at_max_catalog_usage") or 0) / count,
        product=kind.product,
        product_per_furnace=float(rates.get("product_per_minute") or 0) / count,
    )


def head_choice(kind: Kind, rates: dict) -> dict:
    """The feed head whose flow model meets the full block's demand x margin without waste.

    Ranked by (shortfalls, wasted capacity findings, entities): the split head wins whenever
    the shared head halves a lane twice; the shared head stays the choice only if the split
    head is no better by the flow model.
    """
    return rank_heads(
        lambda head: head_bandwidth(kind, rates, head),
        lambda head: len(furnace_block(kind, 0, 0, START_PER_ROW, head)["entities"]),
    )


def _taken(board: dict, position, owner: str) -> bool:
    key = (position["x"], position["y"])
    return any(
        isinstance(row, dict)
        and isinstance(row.get("source"), dict)
        and (row["source"].get("x"), row["source"].get("y")) == key
        and row.get("owner") != owner
        for row in board.get("connections", [])
    )


def registered_source(root: Path, world_id: str, fingerprint, key: str, item: str) -> tuple[dict | None, str]:
    """The proven dedicated-material output ``key`` of this world, or ``(None, reason)``."""
    path = Path(root) / "material-sources.json"
    if not path.exists():
        return None, "dedicated_material_sources_missing"
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved.get("world_id") != world_id or saved.get("catalog_fingerprint") != fingerprint:
        return None, "dedicated_material_sources_world_or_catalog_changed"
    source = (saved.get("sources") or {}).get(key)
    if (
        not isinstance(source, dict)
        or source.get("item") != item
        or source.get("direction") != "output"
        or source.get("world_id") != world_id
        or type(source.get("unit_number")) is not int
        or source.get("facing") not in (0, 4, 8, 12)
        or not isinstance(source.get("position"), dict)
    ):
        return None, f"dedicated_material_{key}_output_missing"
    return copy.deepcopy(source), ""


def grown(state: dict, per_row: int) -> dict:
    """``state`` with ``per_row`` slots built per row: reserved slot entities move into the plan (pure).

    Ports, routes, the output belt and the feed stay; only ``growth_entities``
    shrink. Raises when ``per_row`` exceeds the reserved slots.
    """
    bank = grown_bank(state["bank"], per_row)
    old = {(e["name"], *xy(e)) for e in state["bank"]["entities"]}
    added = [e for e in bank["entities"] if (e["name"], *xy(e)) not in old]
    bank["reserved_area"] = state["bank"]["reserved_area"]
    plan = dict(
        state["plan"],
        entities=state["plan"]["entities"] + added,
        growth_entities=bank["growth_entities"],
    )
    plan["required_items"] = _plan(plan["entities"], [])["required_items"]
    return dict(state, bank=bank, plan=plan, per_row=per_row)


def growth_evidence(state: dict, obs: dict) -> dict:
    """``{"grow": bool, "reason"}``: grow only while built furnaces are busy and the product is taken."""
    bank = state["bank"]
    if bank["per_side_count"] >= bank["capacity_slots"]:
        return {"grow": False, "reason": "every_reserved_slot_built"}
    actual = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
    raw, product = bank["resource"], bank["product"]
    need = 2 * state.get("raw_per_craft", 1)
    furnaces = [actual.get((e["name"], *xy(e))) for row in bank["producer_rows"].values() for e in row]
    if any(f is None or (f.get("inventory") or {}).get(raw, 0) < need for f in furnaces):
        return {"grow": False, "reason": "a_built_furnace_waits_for_raw"}
    raw_port, _coal, out_port = bank["ports"]

    def on_belt(port, item):
        belt = actual.get(("transport-belt", *xy(port))) or {}
        return (belt.get("belt_inventory") or {}).get(item, 0)

    if on_belt(raw_port, raw) < BELT_BACKUP:
        return {"grow": False, "reason": "raw_supply_not_backed_up"}
    if on_belt(out_port, product) >= BELT_BACKUP:
        return {"grow": False, "reason": "product_not_taken"}
    short = feed_shortfalls(state, bank["per_side_count"] + 1)
    if short:
        # The head cannot feed one more furnace per row (belt_flow): rework the head first
        # (observe receipt belt_bandwidth.rework) instead of building furnaces that would starve.
        return {"grow": False, "reason": "feed_head_bandwidth_below_next_slot_demand", "findings": short[:4]}
    return {
        "grow": True,
        "reason": "busy_furnaces_backed_up_raw_and_taken_product",
        "per_row": bank["per_side_count"] + 1,
    }


def feed_shortfalls(state: dict, per_row: int) -> list:
    """Flow-model shortfalls of the saved block with ``per_row`` furnaces built per row."""
    bank = grown_bank(state["bank"], min(per_row, state["bank"]["capacity_slots"]))
    raw, coal, made = rated_per_furnace(state)
    audit = furnace_bank(
        bank["entities"],
        bank["ports"],
        raw=bank["resource"],
        raw_per_furnace=raw,
        coal_per_furnace=coal,
        product=bank.get("product"),
        product_per_furnace=made,
    )
    return [f for f in audit["findings"] if f["kind"] in BANDWIDTH_SHORTFALL]


def furnace_rate(state: dict) -> float:
    """Nameplate product/min of one built furnace of the saved block."""
    rated = state["rated"]
    return float(rated["product_per_minute"]) / int(rated["furnace_count"])


def built_rate(state: dict) -> float:
    return 2 * state["bank"]["per_side_count"] * furnace_rate(state)


def per_row_for(state: dict, rate: float) -> int:
    """Furnaces per row (both rows build alike) whose nameplate covers ``rate``."""
    return max(1, math.ceil(rate / (2 * furnace_rate(state)) - 1e-9))


def furnace_block_claims(root, obs) -> dict:
    """Observe receipt ``furnace_block_claims``: per verified furnace block, nameplate built and
    full (every reserved slot) rate and the demand consumers declared on its product port
    (board ``connections`` rows with ``demand_per_minute``, written by ``produce`` routes)."""
    root = Path(root)
    path = root / "pcb-board.json"
    if not path.exists():
        return {"blocks": []}
    board = json.loads(path.read_text(encoding="utf-8"))
    if board.get("world_id") != obs.get("world_id"):
        return {"status": "world_changed", "blocks": []}
    rows = []
    for kind in KINDS.values():
        state_path = root / kind.state_file
        if not state_path.exists():
            continue
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("world_id") != obs.get("world_id") or state.get("phase") not in ("verified", "growing"):
            continue
        bank, per = state["bank"], furnace_rate(state)
        out = bank["ports"][2]["position"]
        claimed = sum(
            float(row.get("demand_per_minute") or 0)
            for row in board.get("connections", [])
            if isinstance(row, dict)
            and row.get("item") == kind.product
            and isinstance(row.get("source"), dict)
            and (row["source"].get("x"), row["source"].get("y")) == (out["x"], out["y"])
        )
        rows.append(
            {
                "kind": kind.name,
                "product": kind.product,
                "phase": state["phase"],
                "per_furnace_per_minute": per,
                "built_per_row": bank["per_side_count"],
                "reserved_per_row": bank["capacity_slots"],
                "nominal_capacity_per_minute": 2 * bank["per_side_count"] * per,
                "full_block_capacity_per_minute": 2 * bank["capacity_slots"] * per,
                "claimed_port_demand_per_minute": round(claimed, 3),
            }
        )
    return {"blocks": rows, "scope": "nameplate furnace rates and declared consumer demand; not measured"}


class PlannedFurnaces:
    """One furnace block of ``kind`` ("steel" or "brick"); see the module docstring."""

    def __init__(self, game, builder, catalog, *, site, kind: str, route=None, power=None, check=None):
        self.game, self.builder, self.catalog, self.site = game, builder, catalog, site
        self.kind = KINDS[kind]
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / self.kind.state_file
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.route = route or plan_connection
        self.power = power or power_connection
        self.check = check or preflight
        self.last_proof_failure = None

    # ----- identity ---------------------------------------------------------
    def _save(self) -> None:
        _atomic_json(self.path, self.state)

    def _board(self, obs) -> dict | None:
        path = self.root / "pcb-board.json"
        if not path.exists():
            return None
        board = json.loads(path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return None
        return board

    def active(self, obs) -> bool:
        """True on a planned world: a site service and a planned bus published for this world."""
        if self.site is None:
            return False
        if self.state and self.state.get("world_id") == obs["world_id"]:
            return True
        board = self._board(obs)
        return board is not None and planned_tap.published(board, obs["world_id"]) is not None

    def _current(self, obs) -> bool:
        return (
            bool(self.state)
            and self.state.get("world_id") == obs["world_id"]
            and self.state.get("catalog_fingerprint") == self.catalog.fingerprint
        )

    def _fail(self, reason: str, **extra) -> dict:
        return {"ok": False, "reason": reason, "kind": self.kind.name, **extra}

    # ----- planning ---------------------------------------------------------
    def _sources(self, board: dict, obs) -> dict:
        """Registered material sources this block needs (raw for bricks, coal for both)."""
        found = {}
        for role, key, item in (("coal", self.kind.coal_key, "coal"), ("raw", self.kind.raw_key, self.kind.raw)):
            if key is None:
                continue
            source, reason = registered_source(self.root, obs["world_id"], self.catalog.fingerprint, key, item)
            if source is None:
                return self._fail(reason, source=key)
            if _taken(board, source["position"], self.kind.owner):
                return self._fail(f"dedicated_material_{key}_output_taken", source=key)
            found[role] = source
        return {"ok": True, **found}

    def _wants(self, block, raw_source, coal_source):
        tap = block.tap_rect.centre()

        def point(source):
            p = source["position"]
            return (p["x"], p["y"])

        def wants(plan):
            raw_port, coal_port, out_port = plan["ports"]
            rows = [
                (raw_port, tap, "in", block.input_side) if raw_source is None else (raw_port, point(raw_source), "in"),
                (coal_port, point(coal_source), "in"),
                (out_port, block.output_point, "out", block.output_side),
            ]
            return rows

        return wants

    def prepare(self, obs) -> dict:
        """Read-only plan: site, taps, routes, power and preflight; a state dict or a failure."""
        kind = self.kind
        if self.site is None:
            return self._fail("planned_furnaces_require_site_service")
        board = self._board(obs)
        if board is None:
            return self._fail("planned_furnaces_board_identity_changed")
        bus = planned_tap.published(board, obs["world_id"])
        if bus is None:
            return self._fail("planned_furnaces_require_planned_bus")
        sources = self._sources(board, obs)
        if not sources["ok"]:
            return sources
        span = None
        if kind.raw_key is None:
            if not planned_tap.carries(bus, kind.raw):
                return self._fail("planned_bus_has_no_trunk_for_raw_item", item=kind.raw)
            span = planned_tap.max_span(self.game)
            if span is None:
                return self._fail("planned_bus_underground_span_unknown")
        try:
            rates = smelting_rates(self.catalog, kind.raw, kind.product, 2 * START_PER_ROW)
        except (KeyError, TypeError, ValueError) as exc:
            return self._fail("planned_furnaces_recipe_unsupported", detail=str(exc)[:200])
        try:
            blocks = self.site.allocate_candidates(
                "production",
                LENGTH,
                BANK_DEPTH,
                name=kind.owner,
                count=CANDIDATES,
                returns_to_bus=True,
                world_id=obs["world_id"],
            )
        except Exception as exc:  # noqa: BLE001 - the allocator reports, never crashes the job
            return self._fail("planned_furnaces_site_allocation_failed", detail=str(exc)[:200])
        if not blocks:
            return self._fail("no_planned_furnace_district_block")
        others = blocks_without_bus(board)
        unbuilt = planned_unbuilt_hardware(GEOMETRY, obs, others, exclude=(kind.owner,))

        choice = head_choice(kind, rates)  # by the flow model at full reserved growth (belt_flow)

        def build(x0, y0):
            return furnace_block(kind, x0, y0, head=choice["head"])

        failures = []
        for block in blocks:
            wants = self._wants(block, sources.get("raw"), sources["coal"])
            for placement in orientations(build, (LENGTH, BANK_DEPTH), block.rect, wants)[:ORIENTATIONS]:
                bank = dict(placement.plan, site_transform=placement.transform.to_dict())
                result = self._connect(obs, board, bus, block, bank, sources, span, rates, others, unbuilt)
                if not result["ok"]:
                    failures.append({"allocation": block.name, **result})
                    continue
                settle(self.site, obs["world_id"], kind.owner, block.name)
                return {
                    **result,
                    "world_id": obs["world_id"],
                    "catalog_fingerprint": self.catalog.fingerprint,
                    "kind": kind.name,
                    "owner": kind.owner,
                    "allocation": block.name,
                    "transform": placement.transform.to_dict(),
                    "bank": bank,
                    "rated": rates,
                    "raw_per_craft": next(
                        i["amount"] for i in self.catalog.recipes[kind.product]["ingredients"] if i["name"] == kind.raw
                    ),
                    "per_row": START_PER_ROW,
                    "bandwidth": {
                        **choice,
                        "findings": head_bandwidth(kind, rates, choice["head"])["findings"],
                        "scope": "canonical block at full reserved growth through belt_flow",
                    },
                    "phase": "planned",
                }
        settle(self.site, obs["world_id"], kind.owner, None)
        rows = [
            {k: f[k] for k in ("allocation", "reason", "item", "detail", "port", "escape") if f.get(k) is not None}
            for f in failures
        ]
        # One row per allocation and reason (both turns usually fail alike), so the
        # job ledger shows which connection fails on every candidate.
        distinct = [r for i, r in enumerate(rows) if r not in rows[:i]][:8]
        return self._fail(
            f"no_{kind.name}_furnace_block_with_complete_connections",
            candidates=len(blocks),
            failures=distinct,
            evidence={
                "kind": kind.name,
                "candidates": len(blocks),
                "failures": distinct,
                "sources": {
                    role: {"key": key, **sources[role]["position"]}
                    for role, key in (("raw", kind.raw_key), ("coal", kind.coal_key))
                    if key is not None and role in sources
                },
                # Facts whose change can turn a failure into a plan (supervisor retry rule).
                "retry_when": ["material source published or moved", "neighbour plan built", "free site"],
            },
        )

    def _connect(self, obs, board, bus, block, bank, sources, span, rates, others, unbuilt) -> dict:
        """Taps, routes, power and preflight for one oriented bank on one block."""
        kind = self.kind
        growth = bank["growth_entities"]
        if hardware_conflicts(GEOMETRY, bank["entities"] + growth, unbuilt):
            return {"ok": False, "reason": "furnace_block_meets_planned_hardware"}
        if any(
            overlap(bank["reserved_area"], b["reserved_area"])
            for name, b in others.items()
            if isinstance(b, dict) and b.get("reserved_area") and name != kind.owner
        ):
            return {"ok": False, "reason": "board_reservation_overlap"}
        keepouts = growth_keepouts(GEOMETRY, [bank])
        reserved_growth = {xy(e) for e in keepouts}
        raw_port, coal_port, out_port = bank["ports"]
        entities = list(bank["entities"])
        routes, taps, ignored, connections, used = [], [], [], [], {}
        if sources.get("raw") is None:
            tapped = planned_tap.connect_inputs(
                self.game,
                self.builder,
                obs,
                bus,
                owner=kind.owner,
                allocation=block,
                inputs=[raw_port],
                entities=entities + keepouts,
                demand={kind.raw: rates["raw_per_minute"]},
                span=span,
                route=self.route,
            )
            if not tapped["ok"]:
                return {k: tapped.get(k) for k in ("ok", "reason", "item", "failures")}
            entities = [e for e in tapped["entities"] if e["name"] != RESERVED_TILE]
            raw_route = tapped["routes"][-1]
            taps, ignored = tapped["taps"], list(tapped["ignored_units"])
            raw_from = tapped["sources"][kind.raw]
            used["raw"] = {"item": kind.raw, "kind": "planned_bus_tap", "name": raw_from.get("name")}
        else:
            raw_from = sources["raw"]
            raw_route = self.route(self.game, self.builder, obs, raw_from, raw_port, entities + keepouts)
            if not raw_route.get("ok"):
                return _route_failure(raw_route, kind.raw)
            entities = merge_entities(entities, raw_route["entities"])
            used["raw"] = _source_record(kind.raw_key, raw_from)
        routes.append(raw_route)
        connections.append(_connection(kind.raw, raw_from, raw_port, kind.owner))
        coal_from = sources["coal"]
        coal_route = self.route(
            self.game, self.builder, obs, coal_from, coal_port, entities + keepouts, ignored_units=tuple(ignored)
        )
        if not coal_route.get("ok"):
            return _route_failure(coal_route, "coal")
        routes.append(coal_route)
        entities = merge_entities(entities, coal_route["entities"])
        connections.append(_connection("coal", coal_from, coal_port, kind.owner))
        used["coal"] = _source_record(kind.coal_key, coal_from)
        pole = next(e for e in bank["entities"] if e["name"] == "small-electric-pole")
        power = self.power(
            self.game,
            self.builder,
            obs,
            pole["position"],
            [e for e in entities if e["name"] != "small-electric-pole"] + keepouts,
        )
        if not power.get("ok"):
            return {"ok": False, "reason": power.get("reason", "power_failed")}
        entities = merge_entities(entities, [e for e in power["entities"] if e["name"] != RESERVED_TILE])
        boxes = [GEOMETRY._occupied_by_plan([e]) for e in entities]
        if sum(map(len, boxes)) != len(set().union(*boxes)):
            return {"ok": False, "reason": "whole_prospective_plan_overlaps"}
        if set().union(*boxes) & reserved_growth:
            return {"ok": False, "reason": "plan_entity_in_reserved_growth_slot"}
        check = self.check(self.game, self.builder, obs, entities, ignored_units=tuple(ignored))
        if not check.get("ok"):
            return {"ok": False, "reason": check.get("reason", "preflight_failed")}
        obstacles = {
            (e["name"], *xy(e)): e
            for r in routes + [check]
            for e in r.get("clear_obstacles", [])
            if isinstance(e, dict)
        }
        pairs = [p for r in routes for p in r.get("underground_pairs", [])]
        common = dict(
            clear_obstacles=list(obstacles.values()),
            clearance_world_id=obs["world_id"],
        )
        plan = _plan(
            entities,
            [raw_port, coal_port, out_port],
            reserved_area=bank["reserved_area"],
            underground_pairs=pairs + planned_tap.tap_pairs(taps) + list(bank.get("underground_pairs", [])),
            layout_policy=LAYOUT_POLICY,
            recipe=kind.product,
            product=kind.product,
            raw=kind.raw,
            growth_area=bank["growth_area"],
            growth_entities=growth,
            site_transform=bank["site_transform"],
            **common,
        )
        port_belt = [e for e in bank["entities"] if e["name"] == "transport-belt" and xy(e) == xy(raw_port)]
        supply = _plan(
            merge_entities(raw_route["entities"], port_belt),
            [raw_port],
            underground_pairs=list(raw_route.get("underground_pairs", [])),
            layout_policy=LAYOUT_POLICY + "-supply",
            **common,
        )
        return {
            "ok": True,
            "plan": plan,
            "supply_plan": supply,
            "taps": taps,
            "connections": connections,
            "sources": used,
            "route_metrics": [r.get("route_metrics") for r in routes],
        }

    # ----- execution ----------------------------------------------------------
    def _ensure_state(self, obs) -> dict | None:
        """Plan once per world (saved before anything is built); a blocked status or None."""
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        if not self._current(obs):
            return {"status": "blocked", "reason": "planned_furnaces_world_or_catalog_changed", "kind": self.kind.name}
        return None

    def _publish(self, obs) -> dict | None:
        """The whole plan on the board, the taps in the bus state (idempotent); a blocked status or None."""
        path = self.root / "pcb-board.json"
        board = self._board(obs)
        if board is None:
            return {"status": "blocked", "reason": "planned_furnaces_board_identity_changed"}
        name, plan = self.kind.owner, self.state["plan"]
        old = board["blocks"].get(name)
        changed = False
        if old != plan:
            if old is not None and old.get("layout_policy") != LAYOUT_POLICY:
                return {"status": "blocked", "reason": "planned_furnaces_reserved_block_changed", "block": name}
            board["blocks"][name] = plan
            changed = True
        for connection in self.state["connections"]:
            if connection not in board.setdefault("connections", []):
                board["connections"].append(connection)
                changed = True
        if changed:
            _atomic_json(path, board)
        return planned_tap.publish(self.root, obs["world_id"], self.state.get("taps", []))

    def _prepared(self, obs) -> dict | None:
        """Plan, publish and build the taps first; a status/action dict or None when all stand."""
        blocked = self._ensure_state(obs)
        if blocked is not None:
            return blocked
        blocked = self._publish(obs)
        if blocked is not None:
            return blocked
        clear = self.state["plan"].get("clear_obstacles", [])
        return planned_tap.cutover(self.root, self.builder, obs, self.state.get("taps", []), clear_obstacles=clear)

    def supply_step(self, obs) -> dict:
        """``steel_supply``: plan the block, cut over the tap and build the raw route; proof: raw at the port."""
        if self.kind.raw_key is not None:
            return {"status": "blocked", "reason": "planned_furnace_supply_is_a_bus_tap_phase", "kind": self.kind.name}
        pending = self._prepared(obs)
        if pending is not None:
            return pending
        result = self.builder.ensure_plan(obs, self.state["supply_plan"])
        if result.get("status") != "succeeded":
            return result
        evidence = self.supply_proof(obs)
        if evidence is None:
            return {"status": "waiting", "reason": f"waiting for bus {self.kind.raw} at the {self.kind.name} raw port"}
        if not self.state.get("supply_verified"):
            self.state["supply_verified"] = True
            self._save()
        return {
            "status": "succeeded",
            "reason": f"planned bus tap delivers {self.kind.raw} to the {self.kind.name} furnace block",
            "evidence": evidence,
        }

    def production_step(self, obs) -> dict:
        """Build the whole block; proof: every built furnace made the product, product on the port."""
        if not (obs.get("enabled_recipes") or {}).get(self.kind.product):
            return {"status": "blocked", "reason": "native_furnace_recipe_locked", "recipe": self.kind.product}
        pending = self._prepared(obs)
        if pending is not None:
            return pending
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        evidence = self.proof(obs)
        if evidence is None:
            return {
                "status": "waiting",
                "reason": f"waiting for belt-fed {self.kind.product} on the block's product port",
                "detail": self.last_proof_failure,
            }
        if self.state.get("phase") != "verified":
            self.state["phase"] = "verified"
            self._save()
        return {
            "status": "succeeded",
            "reason": f"belt-fed paired furnaces make {self.kind.product} onto a published port",
            "evidence": evidence,
        }

    def capacity_step(self, obs, rate: float) -> dict:
        """``steel_capacity`` on a planned world: fill reserved slots in place (``grown``) up to
        ``rate`` (absolute product/min), then the production step builds and proves them."""
        if not self._current(obs) or not (self.state.get("phase") == "verified" or self.state.get("capacity_target")):
            return {"status": "blocked", "reason": "planned_furnace_production_not_verified", "kind": self.kind.name}
        per_row = per_row_for(self.state, rate)
        bank = self.state["bank"]
        if per_row > bank["capacity_slots"]:
            return {
                "status": "blocked",
                "reason": "furnace_capacity_exceeds_reserved_slots",
                "per_row": per_row,
                "reserved_per_row": bank["capacity_slots"],
            }
        if per_row > bank["per_side_count"]:
            self.state = dict(grown(self.state, per_row), phase="growing", capacity_target=rate)
            self._save()
        elif self.state.get("capacity_target", 0) < rate:
            self.state["capacity_target"] = rate
            self._save()
        return self.production_step(obs)

    def capacity_proof(self, obs) -> dict | None:
        """The production proof with every furnace for the saved capacity target built and producing."""
        evidence = self.proof(obs)
        target = self.state.get("capacity_target") if self.state else None
        if evidence is None or target is None:
            return None
        built = built_rate(self.state)
        if built + 1e-9 < target:
            self.last_proof_failure = {"reason": "built_furnaces_below_capacity_target", "built": built}
            return None
        return dict(evidence, capacity_target_per_minute=target, built_rate_per_minute=built)

    def coal_supply_step(self, obs) -> dict:
        """``steel_coal_supply``: the block's coal comes straight from the registered coal outlet."""
        if not self._current(obs) or self.state.get("phase") != "verified":
            # A wait for steel_production's proof (the roadmap step may be proposed before it), not a block.
            return {
                "status": "waiting",
                "reason": "planned_furnace_production_not_verified",
                "kind": self.kind.name,
                "evidence": {"wait_for": f"{self.kind.name}_production verified"},
            }
        evidence = self.coal_proof(obs)
        if evidence is None:
            return {"status": "waiting", "reason": "waiting for registered coal on the block's coal port"}
        return {
            "status": "succeeded",
            "reason": "the furnace block's own route from the dedicated coal outlet feeds it; no shared header needed",
            "evidence": evidence,
        }

    # ----- proofs -------------------------------------------------------------
    def _live(self, obs):
        return {(e["name"], *xy(e)): e for e in obs["entities"]}

    def _standing(self, obs, plan, actual) -> bool:
        if not underground_proof(obs, plan):
            self.last_proof_failure = {"reason": "underground_pair_unverified"}
            return False
        from .bus_lane import route_tap_tiles, tapped_belt_stands

        tapped = route_tap_tiles(self.root, obs.get("world_id"))
        for wanted in plan["entities"]:
            live = actual.get((wanted["name"], *xy(wanted)))
            if live is None and tapped_belt_stands(actual, tapped, wanted):
                continue  # a bus lane's route tap replaced this belt (bus_lane.record_route_tap)
            if live is None:
                self.last_proof_failure = {"reason": "planned_entity_missing", "entity": wanted["name"]}
                return False
            if wanted["name"] in ORIENTED and live.get("direction") != wanted.get("direction"):
                self.last_proof_failure = {"reason": "planned_entity_direction_changed", "entity": wanted["name"]}
                return False
        return True

    def _belt_items(self, actual, position) -> dict:
        return dict((actual.get(("transport-belt", *xy({"position": position}))) or {}).get("belt_inventory") or {})

    def supply_proof(self, obs) -> dict | None:
        self.last_proof_failure = None
        if not self._current(obs) or "supply_plan" not in self.state:
            return None
        if any(not t.get("built") for t in self.state.get("taps", [])):
            self.last_proof_failure = {"reason": "planned_tap_not_built"}
            return None
        actual = self._live(obs)
        if not self._standing(obs, self.state["supply_plan"], actual):
            return None
        port = self.state["bank"]["ports"][0]
        items = self._belt_items(actual, port["position"])
        if set(items) - {self.kind.raw} or items.get(self.kind.raw, 0) <= 0:
            self.last_proof_failure = {"reason": "raw_port_without_pure_raw", "items": items}
            return None
        return {
            "tick": obs["tick"],
            "raw_port": port["position"],
            "raw_on_port_belt": items[self.kind.raw],
            "taps": [t["name"] for t in self.state.get("taps", [])],
            "scope": "tap-fed raw item reached the furnace block's raw port; no sustained rate",
        }

    def proof(self, obs) -> dict | None:
        self.last_proof_failure = None
        if not self._current(obs):
            return None
        actual = self._live(obs)
        plan, bank, kind = self.state["plan"], self.state["bank"], self.kind
        if not self._standing(obs, plan, actual):
            return None
        for wanted in plan["entities"]:
            live = actual.get((wanted["name"], *xy(wanted))) or {}
            if wanted["name"] in ("inserter", "fast-inserter") and (
                not live.get("electric_network_connected") or live.get("energy", 0) <= 0
            ):
                self.last_proof_failure = {"reason": "inserter_unpowered", "position": wanted["position"]}
                return None
        furnaces, idle = [], []
        for spec in (e for row in bank["producer_rows"].values() for e in row):
            live = actual[(spec["name"], *xy(spec))]
            witness = automatic_furnace_witness(live, kind.raw, kind.product)
            if not witness["ok"]:
                self.last_proof_failure = dict(witness, unit_number=live.get("unit_number"))
                return None
            if witness["requires_native_idle_witness"]:
                idle.append(
                    dict(
                        name=live["name"],
                        position=live["position"],
                        unit_number=live["unit_number"],
                        raw=kind.raw,
                        product=kind.product,
                    )
                )
            furnaces.append(live)
        if idle:
            payload = json.dumps(json.dumps(idle, separators=(",", ":")))
            checked = self.game.query(
                IDLE_FURNACE_WITNESS_LUA + "return idle_furnace_witness(helpers.json_to_table(" + payload + "))"
            )
            if not isinstance(checked, dict) or not checked.get("ok"):
                self.last_proof_failure = checked
                return None
        out_port = bank["ports"][2]
        output = self._belt_items(actual, out_port["position"])
        if set(output) - {kind.product} or output.get(kind.product, 0) <= 0:
            self.last_proof_failure = {"reason": "product_port_without_pure_product", "items": output}
            return None
        lanes = native_feed_lanes(self.game, [bank])
        if not feed_lanes_ok(lanes):
            self.last_proof_failure = {"reason": "feed_lanes_not_raw_and_coal", "lanes": lanes}
            return None
        return {
            "tick": obs["tick"],
            "product": kind.product,
            "furnace_units": [f.get("unit_number") for f in furnaces],
            "products_finished": [f.get("products_finished") for f in furnaces],
            "product_on_port_belt": output[kind.product],
            "product_port": out_port["position"],
            "native_feed_lanes": lanes["banks"],
            "rated": self.state["rated"],
            "built_per_row": bank["per_side_count"],
            "reserved_per_row": bank["capacity_slots"],
            "scope": "ordinary belt-fed furnace commissioning onto a published port; nameplate is not a measured rate",
        }

    def coal_proof(self, obs) -> dict | None:
        self.last_proof_failure = None
        if not self._current(obs) or self.state.get("phase") != "verified":
            return None
        record = self.state["sources"]["coal"]
        source, reason = registered_source(
            self.root, obs["world_id"], self.catalog.fingerprint, self.kind.coal_key, "coal"
        )
        if source is None or source["unit_number"] != record["unit_number"] or xy(source) != xy(record):
            self.last_proof_failure = {"reason": reason or "registered_coal_outlet_identity_changed"}
            return None
        actual = self._live(obs)
        live = actual.get(("transport-belt", *xy(source)))
        if live is None or live.get("unit_number") != source["unit_number"]:
            self.last_proof_failure = {"reason": "registered_coal_outlet_missing"}
            return None
        port = self.state["bank"]["ports"][1]
        items = self._belt_items(actual, port["position"])
        if set(items) - {"coal"} or items.get("coal", 0) <= 0:
            self.last_proof_failure = {"reason": "coal_port_without_pure_coal", "items": items}
            return None
        return {
            "tick": obs["tick"],
            "coal_source": {"key": self.kind.coal_key, "unit_number": source["unit_number"], **source["position"]},
            "coal_port": port["position"],
            "coal_on_port_belt": items["coal"],
            "scope": "registered coal outlet feeds the furnace block's coal port; no sustained rate",
        }


def _route_failure(route: dict, item: str) -> dict:
    """A failed route as a candidate failure row (reason, item and a sealed port's escape tile)."""
    row = {"ok": False, "reason": route.get("reason", "route_failed"), "item": item}
    for key in ("port", "escape"):
        if route.get(key) is not None:
            row[key] = route[key]
    return row


def _connection(item, source, destination, owner) -> dict:
    return {"item": item, "source": source["position"], "destination": destination["position"], "owner": owner}


def _source_record(key, source) -> dict:
    return {
        "key": key,
        "item": source["item"],
        "position": dict(source["position"]),
        "unit_number": source["unit_number"],
        "material_supply_id": source.get("material_supply_id"),
    }
