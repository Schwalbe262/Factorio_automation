"""Wood burner feed: carried wood reaches belt-fed boilers through a wood-first splitter on the coal feed.

Owner's rule (2026-11-13): wood is burned in the power plant. A base boiler
has one fuel slot (``fuel_inventory_size = 1``, Factorio 2.x
``data/base/prototypes/entity/entities.lua``), so wood never fits while the
belt keeps coal in it. Owner's design: on the coal feed belt upstream of the
boilers a wooden chest holding wood -> a burner inserter -> a splitter that
replaced one feed belt, its INPUT priority on the wood side: wood is taken
first, coal fills only what wood does not. The actor deposits carried wood
into that chest (``carried_surplus``: the chest is the wood depot).

The geometry, splice, arm seed, chest registration and standing check are the
shared return feed (``return_feed.py``, column shape); this module keeps the
wood-specific site search, trigger and power-flow proof.

Layout (``layouts``), flow direction ``v``, feed belt tile ``T`` on a straight
run (its upstream and downstream neighbours are feed belts of the same
direction), side tile ``S = T + s`` (``s`` one of the two lateral vectors)::

    S - 3v  wooden-chest         (the wood depot)
    S - 2v  burner-inserter      (pickup: the chest; drops onto S - v; fuels itself from the wood it moves)
    S - v   transport-belt (v)   (the splitter's wood input)
    T + s/2 splitter (v)         (replaces the feed belt at T; input priority S side, output priority T side)
    S + v   stays free           (the splitter's unused output; never a belt)

Candidates are ordered by distance to the plant's coal input (wood enters as
close to the boilers as the straight run allows) and keep away from the feed
cell (drill, fuel arm). Tiles must be free of board keepouts
(``board.starter_keepouts`` without the feed's own block) and pass
``planning.preflight`` with the feed belt at ``T`` ignored. The site is
derived from the reserved feed's own geometry, like the bus feed branches
(``planned_bus``); it is published as board block ``wood_burner_feed``.

Phases (``wood-burner-feed.json``): ``side`` (chest, arm, input belt),
``splice`` (the builder's ``coal_plan`` gets the splitter in place of ``T``
first, then ``T`` is mined (exact unit) and the splitter built and
configured by ``ensure_plan``; the feed proof ``power_evidence`` counts the
remaining belts), ``seed`` (two wood into the burner arm), ``stock`` (carried
wood, then depot wood, into the chest), ``done``.

Proposed by ``wood_burner_feed_nodes`` once: the feed is belt-fed
(``power_coal_feed`` succeeded), splitters are enabled (logistics) and the
receipt ``wood_burner_feed`` shows at least ``WOOD_BATCH`` wood carried or
in depots. Before that wood goes to hand-fed boilers (``power_coal_feed``),
free boiler slots, burner drills and furnaces, then depots.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from . import return_feed
from .core.deterministic_builder import BELT_FED, HAND_FED, coal_feed_mode, power_flow_ready
from .core.deterministic_state import _atomic_json

CAPABILITY = "wood_burner_feed"
BLOCK = "wood_burner_feed"
STATE_FILE = "wood-burner-feed.json"
NODE_ID = "auto_wood_burner_feed"
# Wood worth one disposal trip: about 50 MJ (half a minute of one boiler at full load).
WOOD_BATCH = 25
ARM_SEED = return_feed.ARM_SEED  # wood put into the burner arm once; it then fuels itself from the wood it moves
CELL_CLEARANCE = 2.5  # feed tiles this close to the feed drill or its fuel arm are not candidates
CHEST = return_feed.CHEST
ARM = return_feed.BURNER_ARM
VECTORS = return_feed.VECTORS
BELTS = return_feed.BELTS
PRIORITY_KEYS = return_feed.PRIORITY_KEYS
splitter_side = return_feed.splitter_side
reserved_area = return_feed.reserved_area


def _xy(entity) -> tuple:
    p = entity["position"]
    return (p["x"], p["y"])


def layouts(coal_plan: dict) -> list[dict]:
    """Every wood feed layout (column shape) on a straight run of the feed belt (module doc), unsorted."""
    entities = coal_plan.get("entities") or []
    cell = [_xy(e) for e in entities if e["name"] in (ARM, "inserter", "burner-mining-drill", "electric-mining-drill")]
    return return_feed.layouts(entities, cell=cell, clearance=CELL_CLEARANCE)


def spliced_coal_plan(coal_plan: dict, layout: dict) -> dict:
    """The feed plan with the belt at ``T`` replaced by the wood splitter (idempotent)."""
    return return_feed.spliced_plan(coal_plan, layout)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _wood(entity) -> int:
    return int((entity.get("inventory") or {}).get("wood", 0) or 0)


def receipt(root, observation) -> dict | None:
    """Observe receipt ``wood_burner_feed``: ``{"wood", "built"}`` once the feed is belt-fed, else None.

    wood  = wood carried + wood in registered storage chests (depots)
    built = the wood feed of this world reached its ``stock`` or ``done`` phase
    """
    root, world = Path(root), observation.get("world_id")
    builder = _read(root / "factory-builder.json")
    if builder.get("world_id") != world or not builder.get("coal_plan") or coal_feed_mode(builder) == HAND_FED:
        return None
    state = _read(root / STATE_FILE)
    built = state.get("world_id") == world and state.get("phase") in ("stock", "done")
    storage = _read(root / "inventory-storage.json")
    units = {c.get("unit_number") for c in storage.get("chests") or ()} if storage.get("world_id") == world else set()
    stored = sum(_wood(e) for e in observation.get("entities", []) if e.get("unit_number") in units)
    carried = int((observation.get("inventory") or {}).get("wood", 0) or 0)
    return {"wood": carried + stored, "built": built}


def wood_burner_feed_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One node, once: belt-fed feed, splitters enabled and wood accumulating (module doc)."""
    if CAPABILITY not in capabilities:
        return []
    jobs = list(jobs)
    if any(row.get("capability") == CAPABILITY for row in jobs + list(queue_nodes)):
        return []
    succeeded = {j.get("capability") for j in jobs if j.get("status") == "succeeded"}
    if not set(capabilities[CAPABILITY].get("requires", [])) <= succeeded:
        return []
    row = observation.get("wood_burner_feed")
    if not isinstance(row, dict) or row.get("built") or not isinstance(row.get("wood"), int):
        return []
    if row["wood"] < WOOD_BATCH or "splitter" not in set(observation.get("enabled_recipes") or ()):
        return []
    from .obsolete import RETIRE_PRIORITY

    return [
        {
            "id": NODE_ID,
            "capability": CAPABILITY,
            "parameters": {},
            "priority": RETIRE_PRIORITY,
            "depends_on": [],
            "reason": f"{row['wood']} wood waits for the boilers: a wood-first splitter on the coal feed",
        }
    ]


def validate_parameters(parameters) -> None:
    if parameters not in (None, {}):
        raise ValueError("wood_burner_feed takes no parameters")


class WoodBurnerFeed:
    """Plan, build and stock the wood feed (module doc); ``chest(obs)`` is the wood depot for carried surplus."""

    def __init__(self, game, builder, catalog, *, surplus=None):
        self.game, self.builder, self.catalog, self.surplus = game, builder, catalog, surplus
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE
        self.state = _read(self.path)

    def _save(self):
        _atomic_json(self.path, self.state)

    # ----- the wood depot ---------------------------------------------------------
    def chest(self, obs) -> dict | None:
        """The built, registered wood chest standing in this world, else None."""
        if self.state.get("world_id") != obs.get("world_id"):
            return None
        return return_feed.find_chest(obs, self.state.get("chest"))

    # ----- planning -----------------------------------------------------------------
    def prepare(self, obs) -> dict:
        from .board import keepout_tiles, starter_keepouts
        from .planning import preflight

        builder = getattr(self.builder, "state", None) or {}
        if builder.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "wood_feed_builder_world_changed"}
        coal = builder.get("coal_plan")
        if coal_feed_mode(builder) != BELT_FED or not coal:
            return {"ok": False, "reason": "wood_feed_requires_belt_fed_coal_feed"}
        port = next(
            (p for p in (builder.get("power_plan") or {}).get("ports", []) if p.get("item") == "coal"),
            None,
        )
        target = _xy(port) if port else (0, 0)
        keep = keepout_tiles(starter_keepouts(self.root, obs, exclude=("power_coal_feed", BLOCK)))
        live = {(e["name"], *_xy(e)): e for e in obs.get("entities", [])}
        occupied = {_xy(e) for e in obs.get("entities", []) if e.get("name") in BELTS}
        rows = sorted(
            layouts(coal),
            key=lambda r: (
                (math.hypot(r["belt"]["position"]["x"] - target[0], r["belt"]["position"]["y"] - target[1]),)
                + _xy(r["splitter"])
            ),
        )
        reasons = []
        for row in rows:
            belt = live.get(("transport-belt", *_xy(row["belt"])))
            if belt is None or belt.get("direction") != row["belt"].get("direction"):
                reasons.append("feed_belt_not_built_as_planned")
                continue
            free, side = (row["free"]["x"], row["free"]["y"]), (row["side"]["x"], row["side"]["y"])
            if any(_xy(e) in keep for e in row["entities"]) or {free, side} & keep or free in occupied:
                reasons.append("wood_feed_site_reserved")
                continue
            check = preflight(
                self.game, self.builder, obs, [row["splitter"], *row["entities"]], ignored_units=(belt["unit_number"],)
            )
            if not check.get("ok"):
                reasons.append(str(check.get("reason", "wood_feed_preflight_failed")))
                continue
            return {
                "ok": True,
                "world_id": obs["world_id"],
                "catalog_fingerprint": getattr(self.catalog, "fingerprint", None),
                "phase": "side",
                "layout": dict(row, belt_unit=belt["unit_number"]),
                "clear_obstacles": list(check.get("clear_obstacles") or []),
            }
        return {"ok": False, "reason": "no_wood_feed_site_on_the_coal_feed", "rejected": sorted(set(reasons))[:8]}

    def _side_plan(self) -> dict:
        return return_feed.side_plan(
            self.state["layout"],
            clear_obstacles=self.state.get("clear_obstacles") or [],
            world_id=self.state["world_id"],
            template=BLOCK,
        )

    def _publish(self, obs) -> dict | None:
        """Board block ``wood_burner_feed`` (entities, reserved area, owner ``power``); idempotent."""
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
            return {"status": "blocked", "reason": "wood_feed_board_world_changed"}
        wanted = return_feed.block(self.state["layout"], template=BLOCK, owner="power")
        blocks = board.setdefault("blocks", {})
        if blocks.get(BLOCK) != wanted:
            blocks[BLOCK] = wanted
            _atomic_json(path, board)
        return None

    def _splice(self, obs) -> dict | None:
        """Splitter in hand, then the feed plan gets the splitter, the old belt is mined (exact unit), the splitter built.

        The plan is republished right before the mine, so no other job's feed
        repair sees a planned splitter while the old belt still stands.
        """
        return return_feed.splice(
            self.builder,
            obs,
            self.state["layout"],
            self._republish_feed,
            belt_unit=self.state["layout"]["belt_unit"],
            reason="normally recover one owned coal feed belt for the paid wood feed splitter",
            identity_reason="wood_feed_belt_identity_changed",
        )

    def _republish_feed(self) -> None:
        builder = self.builder.state
        spliced = spliced_coal_plan(builder["coal_plan"], self.state["layout"])
        if spliced != builder["coal_plan"]:
            builder["coal_plan"] = spliced
            self.builder._save()

    def _seed(self, obs) -> dict | None:
        return return_feed.seed_arm(
            self.builder,
            obs,
            self.state["layout"],
            "wood",
            "wood feed: start the burner arm once; it then fuels itself from the wood it moves",
        )

    def _register(self, obs) -> dict | None:
        found = return_feed.register_chest(obs, self.state["layout"])
        if "chest" not in found:
            return found
        self.state["chest"] = found["chest"]
        return None

    def _stock(self, obs) -> dict | None:
        """Carried wood, then depot wood, into the chest; None once nothing is left to move or the chest is full."""
        from .carried_surplus import chest_room

        chest = self.chest(obs)
        if chest is None:
            return {"status": "blocked", "reason": "wood_feed_chest_missing"}
        room = chest_room(chest, "wood", self.catalog)
        if room <= 0:
            return None
        if int((obs.get("inventory") or {}).get("wood", 0)) > 0:
            return return_feed.stock_action(
                self.builder,
                obs,
                chest,
                "wood",
                self.catalog,
                reason="wood feed: carried wood into the boiler wood chest",
            )
        return self.surplus.depot_take(obs, "wood", room) if self.surplus is not None else None

    # ----- controller contract ----------------------------------------------------
    def step(self, obs, parameters=None) -> dict:
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            prepared.pop("ok")
            self.state = prepared
            self._save()
        if self.state.get("world_id") != obs["world_id"] or self.state.get("catalog_fingerprint") != getattr(
            self.catalog, "fingerprint", None
        ):
            return {"status": "blocked", "reason": "wood_feed_world_or_catalog_changed"}
        blocked = self._publish(obs)
        if blocked:
            return blocked
        if self.state["phase"] == "side":
            result = self.builder.ensure_plan(obs, self._side_plan())
            if result.get("status") != "succeeded":
                return result
            self.state["phase"] = "splice"
            self._save()
        if self.state["phase"] == "splice":
            action = self._splice(obs)
            if action is not None:
                return action
            self.state["phase"] = "seed"
            self._save()
        if self.state["phase"] == "seed":
            for action in (self._seed(obs), self._register(obs)):
                if action is not None:
                    return action
            self.state["phase"] = "stock"
            self._save()
        action = self._stock(obs)
        if action is not None:
            return action
        evidence = self.proof(obs)
        if not evidence:
            return {"status": "waiting", "reason": "waiting for wood feed proof and belt-fed power flow"}
        if self.state["phase"] != "done":
            self.state["phase"] = "done"
            self._save()
        return {"status": "succeeded", "reason": "carried wood feeds the boilers first", "evidence": evidence}

    def proof(self, obs) -> dict | None:
        """Fresh evidence: wood side and splitter as planned (wood-side input priority), feed belt replaced, power flowing."""
        state = self.state
        if not state or state.get("world_id") != obs.get("world_id") or state.get("phase") not in ("stock", "done"):
            return None
        split = return_feed.standing(obs, state["layout"])
        if split is None or self.chest(obs) is None:
            return None
        builder = self.builder.state
        coal, power = builder.get("coal_plan") or {}, builder.get("power_plan")
        if not power or not any(e["name"] == "splitter" and _xy(e) == _xy(split) for e in coal.get("entities", [])):
            return None
        flow = self.builder.power_evidence(power, coal)
        if not power_flow_ready(flow):
            return None
        return {
            "tick": obs.get("tick"),
            "splitter": dict(split["position"]),
            "input_priority": split.get("splitter_input_priority"),
            "chest": dict(state["chest"]),
            "chest_wood": _wood(self.chest(obs)),
            "boiler_fuel": flow.get("boiler_fuel"),
            "coal_on_belts": flow.get("coal_on_belts"),
            "scope": "wood side and splitter as planned, feed belt replaced, belt-fed power flowing; no wood rate",
        }
