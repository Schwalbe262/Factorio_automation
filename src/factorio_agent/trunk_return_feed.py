"""Trunk return feed: carried surplus coal / ore goes back onto its patch trunk in one deposit.

Owner's idea (2026-11-13): when the actor carries a lot of surplus coal, dumping
it through a chest and a priority splitter at the start of the mine's output
trunk is easier than walking it to consumers a few items at a time. The
return feed (``return_feed``: chest -> inserter -> side belt -> splitter with
INPUT priority on the side, replacing one trunk belt) sits just downstream of
the trunk head (the bank's output port) and upstream of every tap, so the
returned items reach every trunk consumer before freshly mined ones.

Rules (docs/v2/PLANNERS.md "Carried surplus", LAYOUT_PRINCIPLES.md "Return feed"):

1. Trigger, wait vs spend (:func:`decide`, observe receipt ``trunk_return_feeds``):
   ``wait = (ceil(surplus / stack) + recent trips) * TRIP_SECONDS`` against
   ``spend = BUILD_SECONDS + 2 * distance(actor, trunk head) / RUN_TILES_PER_SECOND``;
   surplus = carried beyond ``carried_surplus.allowance`` + depot stock,
   recent trips = disposal trips of the item to consumers or depots in the
   last ``carried_surplus.TRIP_WINDOW_TICKS`` (``carried_surplus.log_trip``).
   Proposed (:func:`trunk_return_feed_nodes`, once per resource) only when
   ``wait > spend``, the patch has a proven bank whose trunk is observed, the
   trunk has spare belt capacity (``belt rate x built lanes > proven supply``)
   and splitters are enabled (logistics). Before that the carried surplus rule
   is unchanged (consumers with room, then depots).
2. Site (:meth:`TrunkReturnFeed.prepare`): trunk row ``r >= 1`` of the first
   straight run below every recorded tap / lane row, the
   nearest the head first; the elbow shape first (it stays beside the head),
   then the column; the side without taps first. Inserter: electric when a
   standing pole of the patch bank covers its tile (``blocks.params.pole_radius``),
   else a burner inserter, only for a fuel (coal fuels its own arm); ore
   without a pole has no site. Tiles free of live and planned hardware
   (``board.surface_conflict``), ``planning.preflight`` with the trunk belt ignored.
3. Build: the feed is recorded on the trunk ledger (``mine_trunk.record_return``:
   a station whose window keeps later taps and lanes at rows ``>= r + 3``) and
   published as board block ``trunk_return_feed:<item>``; side hardware first,
   then the splice: the trunk belt is released from its owner's plan
   (``mine_trunk.release_trunk_belts``) right before it is mined by unit, the
   splitter is built and configured. Trunk traces pass it
   (``mine_trunk.trunk_segments``, ``tap_passthrough``). A burner arm gets two
   coal once; the chest is registered as the item's surplus sink
   (``carried_surplus.CarriedSurplus.feed_chest``).
4. Proof: every entity as planned, the splitter's input priority on the side,
   the trunk belt gone, the chest standing and the observed trunk passing the
   splitter beyond it. No rate is claimed.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path

from . import mine_trunk, return_feed
from .core.deterministic_state import _atomic_json

CAPABILITY = "trunk_return_feed"
STATE_FILE = "trunk-return-feeds.json"
BLOCK_PREFIX = "trunk_return_feed:"
NAME_PREFIX = "return:"
NODE_PREFIX = "auto_trunk_return_feed_"
# Carried-surplus items with a carry allowance; stone has none (construction stock for furnaces).
RESOURCES = ("coal", "iron-ore", "copper-ore")
FUELS = frozenset({"coal"})  # a burner arm moving these fuels itself
# Wait vs spend (rule 1). Estimates, not measured: one disposal trip is a walk to a consumer or
# depot and back plus the transfer; the build is crafting chest, inserter, splitter (natively
# ~3 s), placing four entities, mining one belt and configuring the splitter.
TRIP_SECONDS = 12.0
BUILD_SECONDS = 40.0
RUN_TILES_PER_SECOND = 8.9  # character running_speed 0.15 tiles/tick x 60
STATION_BACK = 2  # station row r - 2: later tap/lane windows (WINDOW_ROWS + WINDOW_GAP apart) start at r + 3
SHAPES = ("elbow", "column")
PREPARE_ATTEMPTS = 8  # native preflights per prepare


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _xy(entity) -> tuple:
    p = entity["position"]
    return (p["x"], p["y"])


def feed_name(item: str) -> str:
    return NAME_PREFIX + item


# ----- rule 1: wait vs spend -------------------------------------------------------------
def decide(*, surplus: int, trips: int, stack: int, distance: float, spare_per_minute: float) -> dict:
    """Whether a return feed pays for itself now (module rule 1), with the numbers."""
    expected = math.ceil(max(0, int(surplus)) / max(1, int(stack))) + max(0, int(trips))
    wait = expected * TRIP_SECONDS
    spend = BUILD_SECONDS + 2 * max(0.0, float(distance)) / RUN_TILES_PER_SECOND
    if spare_per_minute <= 0:
        reason = "trunk_has_no_spare_belt_capacity"
    elif wait <= spend:
        reason = "surplus_trips_cheaper_than_the_feed"
    else:
        reason = "return_feed_pays"
    return {
        "expected_trips": expected,
        "wait_seconds": round(wait, 1),
        "spend_seconds": round(spend, 1),
        "ready": reason == "return_feed_pays",
        "reason": reason,
    }


def _bank(mining: dict, obs, item):
    return mine_trunk.patch_bank(mining.get("banks"), item, mine_trunk.patch_seed(obs, item))


def receipt(root, obs, catalog) -> dict | None:
    """Observe receipt ``trunk_return_feeds``: per resource with a patch bank, the trigger numbers, else None."""
    from .carried_surplus import allowance, carried, recent_trips, stack_size

    root, world = Path(root), obs.get("world_id")
    mining = _read(root / "mining-outposts.json")
    if not world or mining.get("world_id") != world:
        return None
    state = _read(root / STATE_FILE)
    feeds = state.get("feeds", {}) if state.get("world_id") == world else {}
    storage = _read(root / "inventory-storage.json")
    units = {c.get("unit_number") for c in storage.get("chests") or ()} if storage.get("world_id") == world else set()
    depots = [e for e in obs.get("entities", []) if e.get("unit_number") in units]
    ledger = mine_trunk.ledger(root, world)["trunks"]
    actor = obs.get("position") or {"x": 0, "y": 0}
    rows = {}
    for item in RESOURCES:
        bank = _bank(mining, obs, item)
        if bank is None:
            continue
        key = mine_trunk.trunk_key(bank)
        port = bank["plan"]["ports"][0]
        seed = mine_trunk.patch_seed(obs, item)
        supply = sum(
            float(b.get("nominal_rate_per_minute", 0))
            for b in mining.get("banks", [])
            if b.get("ready") and mine_trunk.on_patch(b, item, seed) and not b.get("material_supply_id")
        )
        capacity = mine_trunk.belt_rate() * mine_trunk.lanes_built(ledger.get(key, {}))
        excess = max(0, carried(obs, item) - int(allowance(item, catalog) or 0))
        stored = sum(int((e.get("inventory") or {}).get(item, 0) or 0) for e in depots)
        trips = recent_trips(root, obs, item)
        p = port["position"]
        distance = math.hypot(p["x"] - actor.get("x", 0), p["y"] - actor.get("y", 0))
        entry = feeds.get(item) or {}
        row = decide(
            surplus=excess + stored,
            trips=trips,
            stack=stack_size(catalog, item),
            distance=distance,
            spare_per_minute=capacity - supply,
        )
        row.update(
            trunk=key,
            surplus=excess + stored,
            recent_trips=trips,
            spare_per_minute=round(capacity - supply, 3),
            observed=bool(isinstance(obs.get("entities"), list) and mine_trunk.trace_trunk(obs, port)),
            built=entry.get("phase") in ("stock", "done"),
        )
        rows[item] = row
    return {"items": rows} if rows else None


def trunk_return_feed_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One node per resource, once: the receipt says the feed pays, the trunk is observed, splitters enabled."""
    if CAPABILITY not in capabilities:
        return []
    rows = (observation.get("trunk_return_feeds") or {}).get("items")
    if not isinstance(rows, dict) or "splitter" not in set(observation.get("enabled_recipes") or ()):
        return []
    jobs = list(jobs)
    succeeded = {j.get("capability") for j in jobs if j.get("status") == "succeeded"}
    if not set(capabilities[CAPABILITY].get("requires", [])) <= succeeded:
        return []
    from .obsolete import RETIRE_PRIORITY

    nodes = []
    for item in RESOURCES:
        row = rows.get(item)
        if not isinstance(row, dict) or not row.get("ready") or not row.get("observed") or row.get("built"):
            continue
        if any(
            r.get("capability") == CAPABILITY and (r.get("parameters") or {}).get("resource") == item
            for r in jobs + list(queue_nodes)
        ):
            continue  # one per trunk; a stalled feed is not proposed again
        nodes.append(
            {
                "id": NODE_PREFIX + item,
                "capability": CAPABILITY,
                "parameters": {"resource": item},
                "priority": RETIRE_PRIORITY,
                "depends_on": [],
                "reason": f"{row.get('surplus')} surplus {item}, {row.get('recent_trips')} recent trips: "
                f"{row.get('wait_seconds')}s of trips > {row.get('spend_seconds')}s to build a return feed "
                f"on trunk {row.get('trunk')}"[:300],
            }
        )
    return nodes


def validate_parameters(parameters) -> None:
    if not isinstance(parameters, dict) or set(parameters) != {"resource"}:
        raise ValueError("trunk_return_feed takes exactly resource")
    if parameters["resource"] not in RESOURCES:
        raise ValueError("trunk_return_feed resource must be one of " + ", ".join(RESOURCES))


# ----- controller --------------------------------------------------------------------------
class TrunkReturnFeed:
    """Plan, build and register one return feed per patch trunk (module doc); ``chest(obs, item)`` is the sink."""

    def __init__(self, game, builder, catalog, *, outposts=None, smelting=None, surplus=None):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.outposts, self.smelting, self.surplus = outposts, smelting, surplus
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE
        self.state = _read(self.path)

    def _save(self):
        _atomic_json(self.path, self.state)

    def _mining(self) -> dict:
        return _read(self.root / "mining-outposts.json")

    def _feeds(self, obs) -> dict:
        if self.state.get("world_id") != obs.get("world_id"):
            return {}
        return self.state.get("feeds") or {}

    # ----- the sink ------------------------------------------------------------------
    def chest(self, obs, item) -> dict | None:
        """The registered feed chest of ``item`` standing in this world, else None."""
        entry = self._feeds(obs).get(item) or {}
        return return_feed.find_chest(obs, entry.get("chest"))

    # ----- planning ---------------------------------------------------------------------
    def _arm(self, obs, bank, layout, item) -> str | None:
        """Electric inserter when a standing bank pole covers the arm tile, burner for a fuel, else None."""
        from .blocks.params import pole_radius

        arm = return_feed.arm_spec(layout)["position"]
        live = {(e.get("name"), *_xy(e)) for e in obs.get("entities", []) if isinstance(e.get("position"), dict)}
        for pole in bank["plan"].get("entities", []):
            if "electric-pole" not in pole["name"] or (pole["name"], *_xy(pole)) not in live:
                continue
            try:
                reach = pole_radius(self.catalog, pole["name"])
            except (KeyError, ValueError, TypeError):
                continue
            px, py = _xy(pole)
            if abs(arm["x"] - px) <= reach and abs(arm["y"] - py) <= reach:
                return return_feed.ELECTRIC_ARM
        return return_feed.BURNER_ARM if item in FUELS else None

    def prepare(self, obs, item) -> dict:
        from .board import blocks_without_bus, surface_conflict
        from .planning import preflight

        mining = self._mining()
        if mining.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "return_feed_no_patch_bank"}
        bank = _bank(mining, obs, item)
        if bank is None:
            return {"ok": False, "reason": "return_feed_no_patch_bank"}
        key = mine_trunk.trunk_key(bank)
        run = mine_trunk.trace_trunk(obs, bank["plan"]["ports"][0])
        if len(run) < 3:
            return {"ok": False, "reason": "return_feed_trunk_not_observed", "trunk": key}
        others = [s for s in mine_trunk.stations(self.root, obs["world_id"], key) if s.get("kind") != "return"]
        first = min((int(s["along"]) for s in others if s.get("segment", 0) == 0), default=len(run))
        tapped = {s.get("side") for s in others if s.get("kind") != "lane"}
        board = _read(self.root / "pcb-board.json")
        blocks = blocks_without_bus(board) if board.get("world_id") == obs["world_id"] else {}
        live = [e for e in obs.get("entities", []) if isinstance(e.get("position"), dict) and e.get("name")]
        planned = [e for s in others for e in s.get("entities") or ()]
        occupied = self.builder._occupied_by_plan(live + list(bank["plan"].get("entities", [])) + planned)
        for s in others:  # a lane's head port is where the next row lane joins: kept free
            port = (s.get("port") or {}).get("position")
            if isinstance(port, dict):
                occupied.add((math.floor(port["x"]) + 0.5, math.floor(port["y"]) + 0.5))
        reasons, attempts = [], 0
        # Row r of the first run: a trunk belt behind (r >= 1), r below every tap / lane row.
        for r in range(1, min(first, len(run) - 1)):
            ahead = None
            if run[r + 1].get("name") == "splitter" and run[r].get("direction") in return_feed.VECTORS:
                vx, vy = return_feed.VECTORS[run[r]["direction"]]  # the next tap's splitter, on tile T + v
                ahead = {(_xy(run[r])[0] + vx, _xy(run[r])[1] + vy): run[r + 1]}
            rows = return_feed.layouts(run[r - 1 : r + 2], taken=occupied, shapes=SHAPES, ahead=ahead)
            rows = [x for x in rows if _xy(x["belt"]) == _xy(run[r])]
            rows.sort(key=lambda x: (SHAPES.index(x.get("shape", "column")), self._side(x) in tapped))
            for row in rows:
                if attempts >= PREPARE_ATTEMPTS:
                    break
                sign = 1 if self._side(row) == "right" else -1
                arm = self._arm(obs, bank, row, item)
                if arm is None:
                    reasons.append("return_feed_inserter_unpowered")
                    continue
                row = return_feed.layout_at(row["belt"], sign, row.get("shape", "column"), arm=arm)
                hardware = [row["splitter"], *row["entities"]]
                if surface_conflict(self.builder, obs, blocks, row["entities"]) is not None:
                    reasons.append("return_feed_meets_planned_hardware")
                    continue
                attempts += 1
                check = preflight(self.game, self.builder, obs, hardware, ignored_units=(run[r]["unit_number"],))
                if not check.get("ok"):
                    reasons.append(str(check.get("reason", "return_feed_preflight_failed")))
                    continue
                return {
                    "ok": True,
                    "world_id": obs["world_id"],
                    "catalog_fingerprint": getattr(self.catalog, "fingerprint", None),
                    "phase": "side",
                    "trunk": key,
                    "row": r,
                    "layout": dict(row, belt_unit=run[r]["unit_number"]),
                    "clear_obstacles": list(check.get("clear_obstacles") or []),
                }
        return {
            "ok": False,
            "reason": "no_return_feed_site_at_the_trunk_head",
            "trunk": key,
            "rows_before_first_tap": max(0, min(first - 1, len(run) - 1) - 1),
            "rejected": sorted(set(reasons))[:8],
        }

    @staticmethod
    def _side(layout) -> str:
        """``left`` / ``right`` of the feed side seen along the trunk's facing."""
        return return_feed.splitter_side(layout["splitter"], layout["side"])

    # ----- records ------------------------------------------------------------------------
    def station(self, item, entry) -> dict:
        """The ledger record of the feed on its trunk (``mine_trunk.record_return``)."""
        layout = entry["layout"]
        return {
            "kind": "return",
            "name": feed_name(item),
            "item": item,
            "side": self._side(layout),
            "along": entry["row"] - STATION_BACK,
            "row": entry["row"],
            "segment": 0,
            "splitter": deepcopy(layout["splitter"]),
            "entities": deepcopy([layout["splitter"], *layout["entities"]]),
            "remove": [dict(layout["belt"]["position"])],
            "remove_units": [layout["belt_unit"]],
            "trunk": entry["trunk"],
        }

    def _publish(self, obs, item, entry) -> dict | None:
        mine_trunk.record_return(self.root, obs["world_id"], self.station(item, entry))
        path = self.root / "pcb-board.json"
        board = _read(path)
        if not board:
            board = {
                "schema_version": 1,
                "world_id": obs["world_id"],
                "catalog_fingerprint": getattr(self.catalog, "fingerprint", None),
                "blocks": {},
            }
        if board.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "return_feed_board_world_changed"}
        name = BLOCK_PREFIX + item
        wanted = return_feed.block(entry["layout"], template=CAPABILITY, owner="mine_trunk", trunk=entry["trunk"])
        blocks = board.setdefault("blocks", {})
        if blocks.get(name) != wanted:
            blocks[name] = wanted
            _atomic_json(path, board)
        return None

    def _release(self, obs, item, entry):
        """Republish: the trunk belt ``T`` leaves its owner's saved plan and board copy (before the mine)."""
        changed = mine_trunk.release_trunk_belts(self.root, obs["world_id"], self.station(item, entry))
        if "smelting_district" in changed and self.smelting is not None:
            self.smelting.state = _read(self.root / "smelting-district.json")
        if any(owner != "smelting_district" for owner in changed) and self.outposts is not None:
            self.outposts.state = self._mining()
        return None

    # ----- controller contract ------------------------------------------------------------
    def step(self, obs, parameters=None) -> dict:
        validate_parameters(parameters)
        item = parameters["resource"]
        if self.state and self.state.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "return_feed_world_changed"}
        feeds = self.state.setdefault("feeds", {})
        entry = feeds.get(item)
        if entry is None:
            prepared = self.prepare(obs, item)
            if not prepared.get("ok"):
                return {"status": "blocked", "reason": prepared["reason"], "evidence": prepared}
            prepared.pop("ok")
            self.state["world_id"] = obs["world_id"]
            entry = feeds[item] = prepared
            self._save()
        if entry.get("catalog_fingerprint") != getattr(self.catalog, "fingerprint", None):
            return {"status": "blocked", "reason": "return_feed_catalog_changed"}
        blocked = self._publish(obs, item, entry)
        if blocked:
            return blocked
        layout = entry["layout"]
        if entry["phase"] == "side":
            plan = return_feed.side_plan(
                layout,
                clear_obstacles=entry.get("clear_obstacles") or [],
                world_id=obs["world_id"],
                template=CAPABILITY,
            )
            result = self.builder.ensure_plan(obs, plan)
            if result.get("status") != "succeeded":
                return result
            entry["phase"] = "splice"
            self._save()
        if entry["phase"] == "splice":
            action = return_feed.splice(
                self.builder,
                obs,
                layout,
                lambda: self._release(obs, item, entry),
                belt_unit=layout["belt_unit"],
                reason=f"normally recover one {item} trunk belt for the paid return feed splitter",
                identity_reason="return_feed_trunk_belt_identity_changed",
            )
            if action is not None:
                return action
            entry["phase"] = "seed"
            self._save()
        if entry["phase"] == "seed":
            action = return_feed.seed_arm(
                self.builder, obs, layout, item, f"return feed: start the burner arm once with {item}"
            )
            if action is not None:
                return action
            found = return_feed.register_chest(obs, layout)
            if "chest" not in found:
                return found
            entry["chest"] = found["chest"]
            entry["phase"] = "stock"
            self._save()
        action = self._stock(obs, item)
        if action is not None:
            return action
        evidence = self.proof(obs, parameters)
        if not evidence:
            return {"status": "waiting", "reason": "waiting for the return feed to stand on the trunk"}
        if entry["phase"] != "done":
            entry["phase"] = "done"
            self._save()
        return {"status": "succeeded", "reason": f"surplus {item} returns to its patch trunk", "evidence": evidence}

    def _stock(self, obs, item) -> dict | None:
        """The carried surplus (above allowance and held-back fuel) into the new chest, once; depots drain later."""
        from .carried_surplus import allowance

        chest = self.chest(obs, item)
        if chest is None:
            return {"status": "blocked", "reason": "return_feed_chest_missing"}
        keep = int(allowance(item, self.catalog) or 0)
        if self.surplus is not None:
            keep += self.surplus.held_back(obs, item)
        return return_feed.stock_action(
            self.builder, obs, chest, item, self.catalog, keep=keep, reason=f"return feed: carried surplus {item}"
        )

    def proof(self, obs, parameters=None) -> dict | None:
        """Fresh evidence: feed as planned (side input priority), trunk belt replaced, chest standing, trunk passes."""
        item = (parameters or {}).get("resource")
        entry = self._feeds(obs).get(item) or {}
        if entry.get("phase") not in ("stock", "done"):
            return None
        layout = entry["layout"]
        split = return_feed.standing(obs, layout)
        chest = self.chest(obs, item)
        if split is None or chest is None:
            return None
        bank = mine_trunk.find_bank(self._mining().get("banks"), entry["trunk"])
        if bank is None:
            return None
        run = mine_trunk.trace_trunk(obs, bank["plan"]["ports"][0])
        r = entry["row"]
        if len(run) <= r + 1 or run[r].get("unit_number") != split.get("unit_number"):
            return None
        return {
            "tick": obs.get("tick"),
            "trunk": entry["trunk"],
            "row": r,
            "splitter": dict(split["position"]),
            "input_priority": split.get("splitter_input_priority"),
            "arm": return_feed.arm_spec(layout)["name"],
            "chest": dict(entry["chest"]),
            "chest_stock": int((chest.get("inventory") or {}).get(item, 0) or 0),
            "trunk_tiles_traced": len(run),
            "scope": "feed as planned, trunk belt replaced, the trunk passes the splitter; no return rate",
        }
