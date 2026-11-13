"""``produce``: a generic recipe-driven assembler line ending in a bounded stock chest.

Contract (``jobs.CAPABILITIES["produce"]``):

* parameters ``{"item": <recipe product>, "rate_per_minute": (0, MAX_RATE]}``;
* the product recipe, an assembling machine, the row inserter (fastest
  enabled of ``RowLimits.inserter_items_per_minute``), long-handed inserters
  for a two-belt line, a chest and belts/poles must be enabled (checked at
  step time, reported as ``production_row_recipes_locked``);
* layout: ``production_layout`` (main row, optional one-level sub-row, stock
  tail), sized for ``min(rate, stock tail rate)`` with ``blocks.size_row``
  derating; placement, routing, power and preflight: ``production_site``;
  input sources: ``production_sources`` (bus taps, free board ports); on the
  planned bus a plate tap is planned with the block (``planned_tap``) and
  built first in ``step``; on the development-map bus a missing tap is
  installed through ``bus_tap.BusTap``;
* build: the normal builder (``ensure_plan``, real paid items); then the
  chest bar is set to ``bar_slots`` (2 stacks) with the game's ``bar`` action;
* proof: every planned entity present, machines powered with the right
  recipe, every machine has finished products, and the stock chest holds the
  product (machine output arrived through the belt and the tail inserter);
* a later request for the same item with a higher rate fills reserved slots
  in place (``production_layout.extend_line``); beyond them it is blocked.

State: ``production-row-<item>.json`` in the runtime dir; board block
``production_row_<item>`` with the input ports and the chest output port.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from factorio_agent.core.deterministic_state import _atomic_json

from . import planned_tap
from .blocks.params import DEFAULT_LIMITS, inserter_rate
from .production_layout import audit_line, design_line, extend_line, input_demand, rows, unsourced_raw
from .production_site import place_line, turned
from .production_sources import resolve, sourced_items, tap_parameters
from .transport import underground_proof, xy

MAX_RATE = 600
STATE_PREFIX = "production-row-"
BLOCK_PREFIX = "production_row_"
CHESTS = ("steel-chest", "iron-chest", "wooden-chest")
ORIENTED = ("transport-belt", "underground-belt", "inserter", "fast-inserter", "long-handed-inserter")
BAR_LUA = """local x=helpers.json_to_table(PAYLOAD);local e=target(x.position,x.name)
if not e then return {ok=false,reason="production_row_chest_missing"} end
local inv=e.get_inventory(defines.inventory.chest)
if not inv or not inv.supports_bar() then return {ok=false,reason="production_row_chest_bar_unsupported"} end
return {ok=true,slots=inv.get_bar()-1,size=#inv}"""


def validate_parameters(parameters) -> None:
    """Structure only; recipe support is checked against the catalog at step time."""
    if not isinstance(parameters, dict) or set(parameters) != {"item", "rate_per_minute"}:
        raise ValueError("produce requires item and rate_per_minute")
    item, rate = parameters["item"], parameters["rate_per_minute"]
    if not isinstance(item, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item):
        raise ValueError("produce item must be a catalog item name")
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= MAX_RATE:
        raise ValueError(f"produce rate_per_minute must be finite in (0,{MAX_RATE}]")


def _enabled(obs) -> list[str]:
    return sorted(name for name, on in (obs.get("enabled_recipes") or {}).items() if on)


def choose_inserter(enabled: list[str], limits=DEFAULT_LIMITS) -> str | None:
    """Fastest enabled plain inserter of the rate table (never the long-handed one)."""
    rates = {k: v for k, v in limits.inserter_items_per_minute.items() if k != limits.long_inserter and k in enabled}
    return max(rates, key=lambda name: (rates[name], name)) if rates else None


def choose_chest(enabled: list[str]) -> str | None:
    return next((name for name in CHESTS if name in enabled), None)


class ProductionRows:
    """One state file and one board block per produced item; see the module docstring."""

    def __init__(self, game, builder, catalog, *, site=None, tap_factory=None):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.site = site
        self.root = Path(game.cfg.runtime_dir)
        self.tap_factory = tap_factory
        self._tap = None
        self.last_proof_failure = None

    # ----- state ---------------------------------------------------------
    def path(self, item: str) -> Path:
        return self.root / f"{STATE_PREFIX}{item}.json"

    def state(self, item: str) -> dict:
        path = self.path(item)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save(self, item: str, state: dict) -> None:
        _atomic_json(self.path(item), state)

    def _board(self, obs) -> dict | None:
        path = self.root / "pcb-board.json"
        if not path.exists():
            return None
        board = json.loads(path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return None
        return board

    # ----- planning --------------------------------------------------------
    def _requirements(self, obs, item: str) -> dict:
        enabled = _enabled(obs)
        recipe = (self.catalog.recipe_for_product(item) or {}).get("name")
        inserter, chest = choose_inserter(enabled), choose_chest(enabled)
        missing = [name for name in (recipe, "transport-belt", "small-electric-pole") if name and name not in enabled]
        if recipe is None:
            return {"ok": False, "reason": "production_row_no_recipe", "item": item}
        if missing or inserter is None or chest is None:
            return {
                "ok": False,
                "reason": "production_row_recipes_locked",
                "item": item,
                "missing": missing + ([] if inserter else ["inserter"]) + ([] if chest else ["chest"]),
            }
        return {"ok": True, "enabled": enabled, "inserter": inserter, "chest": chest, "recipe": recipe}

    def design(self, obs, board: dict, parameters: dict) -> dict:
        """Canonical line for the request, or a failure dict (tap needed, unsupported, locked)."""
        item = parameters["item"]
        owner = BLOCK_PREFIX + item
        needs = self._requirements(obs, item)
        if not needs["ok"]:
            return needs
        rate = min(parameters["rate_per_minute"], inserter_rate(self.catalog, needs["inserter"]))
        available = sourced_items(board, item, owner=owner)

        def sourced(name: str) -> bool:
            return available(name) or tap_parameters(board, item, name, 0) is not None

        arguments = dict(
            product=item,
            rate=rate,
            inserter=needs["inserter"],
            enabled=needs["enabled"],
            sourced=sourced,
            chest=needs["chest"],
        )
        try:
            line = design_line(self.catalog, **arguments)
        except ValueError as exc:
            missing = unsourced_raw(self.catalog, needs["recipe"], sourced)
            for name in self.catalog.recipes[needs["recipe"]]["ingredients"]:
                inner = (self.catalog.recipe_for_product(name["name"]) or {}).get("name")
                if inner and not sourced(name["name"]) and inner != needs["recipe"]:
                    missing += unsourced_raw(self.catalog, inner, sourced)
            if missing:
                return {"ok": False, **resolve(board, item, missing[0], owner=owner)}
            return {"ok": False, "reason": "production_row_unsupported", "item": item, "detail": str(exc)[:300]}
        if any(name.startswith("long-handed") for name in line["main"]["required_items"]) and (
            "long-handed-inserter" not in needs["enabled"]
        ):
            return {"ok": False, "reason": "production_row_recipes_locked", "missing": ["long-handed-inserter"]}
        for port in line["inputs"]:
            found = resolve(board, item, port["item"], owner=owner)
            if not found["ok"]:
                tap = tap_parameters(board, item, port["item"], input_demand(self.catalog, line, port["item"]))
                if tap is None:
                    return {"ok": False, **found}
                return {"ok": False, "reason": "production_row_needs_bus_tap", "tap": tap, "item": port["item"]}
        return {"ok": True, "line": line, "arguments": arguments, "rate": rate}

    def prepare(self, obs, parameters: dict) -> dict:
        validate_parameters(parameters)
        board = self._board(obs)
        if board is None:
            return {"ok": False, "reason": "production_row_board_identity_changed"}
        designed = self.design(obs, board, parameters)
        if not designed["ok"]:
            return designed
        design = dict(designed["line"], arguments=designed["arguments"])
        owner = BLOCK_PREFIX + parameters["item"]
        placed = place_line(self.game, self.builder, obs, self.catalog, self.site, board, design, owner=owner)
        if not placed["ok"]:
            return placed
        return {
            **placed,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "item": parameters["item"],
            "requested_rate": parameters["rate_per_minute"],
            "rate": designed["rate"],
            "phase": "build",
        }

    # ----- bus taps ---------------------------------------------------------
    def _install_tap(self, obs, parameters: dict) -> dict:
        """Drive one ``BusTap`` install like the construction mall; one tap at a time."""
        if self.tap_factory is None:
            return {"status": "blocked", "reason": "production_row_bus_tap_unavailable", "tap": parameters}
        tap = self._tap = self._tap or self.tap_factory()
        wanted = {**parameters, "line": 0}
        if tap.state and tap.state.get("parameters") != wanted and tap.state.get("phase") != "verified":
            return {"status": "waiting", "reason": "production_row_other_bus_tap_unfinished"}
        if not tap.state or tap.state.get("parameters") != wanted:
            fresh = tap.prepare(obs, parameters)
            if not fresh.get("ok"):
                return dict(fresh, status="blocked")
            tap.state = fresh
            tap._save()
        result = tap.step(obs, parameters)
        if result.get("status") == "succeeded":
            return {"status": "waiting", "reason": "production_row_tap_commissioned_fresh_observation_required"}
        return result

    # ----- execution ------------------------------------------------------
    def _publish(self, obs, state: dict) -> dict | None:
        path = self.root / "pcb-board.json"
        board = self._board(obs)
        if board is None:
            return {"status": "blocked", "reason": "production_row_board_identity_changed"}
        name = BLOCK_PREFIX + state["item"]
        changed = False
        if board["blocks"].get(name) != state["plan"]:
            board["blocks"][name] = state["plan"]
            changed = True
        for connection in state["connections"]:
            if connection not in board.setdefault("connections", []):
                board["connections"].append(connection)
                changed = True
        if changed:
            _atomic_json(path, board)
        return None

    def _bar(self, obs, state: dict) -> dict | None:
        if state.get("bar_set"):
            return None
        chest = state["line"]["tail"]["chest"]
        payload = json.dumps(json.dumps({"name": chest["name"], "position": chest["position"]}))
        native = self.game.query(BAR_LUA.replace("PAYLOAD", payload))
        if not isinstance(native, dict) or not native.get("ok"):
            return {"status": "blocked", **(native if isinstance(native, dict) else {"reason": "bar_query_failed"})}
        slots = state["line"]["bar_slots"]
        if native.get("slots") != slots:
            return {"type": "bar", "name": chest["name"], "position": chest["position"], "slots": slots}
        state["bar_set"] = True
        self.save(state["item"], state)
        return None

    def _extend(self, obs, state: dict, rate: float) -> dict | None:
        """Fill reserved slots for a higher rate; None when nothing changes."""
        line = state["line"]
        per = line["main"]["rated_per_minute"] / line["main"]["count"]
        target = min(rate, inserter_rate(self.catalog, line["tail"]["arm"]["name"]))
        extra = math.ceil(target / per - 1e-9) - line["main"]["count"]
        if extra <= 0:
            return None
        if extra > line["main"]["growth"]["remaining_slots"]:
            return {"status": "blocked", "reason": "production_row_extension_exceeds_reserved_slots", "extra": extra}
        try:
            canonical = extend_line(self.catalog, state["canonical"], extra)
        except ValueError as exc:
            return {"status": "blocked", "reason": "production_row_extension_rejected", "detail": str(exc)[:200]}
        world = turned(state, canonical)
        if audit_line(self.catalog, world):
            return {"status": "blocked", "reason": "production_row_extension_audit_failed"}
        old = {(e["name"], *xy(e)) for e in state["line"]["entities"]}
        added = [e for e in world["entities"] if (e["name"], *xy(e)) not in old]
        state["plan"] = dict(state["plan"], entities=state["plan"]["entities"] + added)
        state.update(line=world, canonical=canonical, requested_rate=rate, phase="build", extended=extra)
        self.save(state["item"], state)
        return None

    def step(self, obs, parameters: dict) -> dict:
        validate_parameters(parameters)
        item = parameters["item"]
        state = self.state(item)
        if not state:
            prepared = self.prepare(obs, parameters)
            if prepared.get("reason") == "production_row_needs_bus_tap":
                return self._install_tap(obs, prepared["tap"])
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            state = prepared
            self.save(item, state)
        if state["world_id"] != obs["world_id"] or state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "production_row_world_or_catalog_changed"}
        needs = self._requirements(obs, item)
        if not needs["ok"]:
            return {"status": "blocked", **needs}
        if parameters["rate_per_minute"] > state["requested_rate"]:
            extended = self._extend(obs, state, parameters["rate_per_minute"])
            if extended is not None:
                return extended
        published = self._publish(obs, state)
        if published is not None:
            return published
        taps = state.get("taps", [])
        blocked = planned_tap.publish(self.root, obs["world_id"], taps)
        if blocked:
            return blocked
        clear = state["plan"].get("clear_obstacles", [])
        cut = planned_tap.cutover(self.root, self.builder, obs, taps, clear_obstacles=clear)
        if cut is not None:
            return cut
        result = self.builder.ensure_plan(obs, state["plan"])
        if result.get("status") != "succeeded":
            return result
        bar = self._bar(obs, state)
        if bar is not None:
            return bar
        evidence = self.proof(obs, parameters)
        if evidence is None:
            return {"status": "waiting", "reason": f"waiting for machine-made {item} in the stock chest"}
        state["phase"] = "verified"
        self.save(item, state)
        return {
            "status": "succeeded",
            "reason": f"belt-fed assemblers stock {item} in a capped chest",
            "evidence": evidence,
        }

    def proof(self, obs, parameters: dict) -> dict | None:
        self.last_proof_failure = None
        state = self.state(parameters["item"])
        if not state or state["world_id"] != obs["world_id"] or not underground_proof(obs, state["plan"]):
            return None
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        for wanted in state["plan"]["entities"]:
            live = actual.get((wanted["name"], *xy(wanted)))
            if live is None:
                self.last_proof_failure = {"reason": "planned_entity_missing", "entity": wanted["name"]}
                return None
            if wanted["name"] in ORIENTED and live.get("direction") != wanted["direction"]:
                self.last_proof_failure = {"reason": "planned_entity_direction_changed", "entity": wanted["name"]}
                return None
            if wanted.get("recipe") and live.get("recipe") != wanted["recipe"]:
                self.last_proof_failure = {"reason": "machine_recipe_changed", "position": wanted["position"]}
                return None
        line = state["line"]
        machines = [actual[(e["name"], *xy(e))] for row in rows(line) for e in row["entities"] if e.get("recipe")]
        if any(not m.get("electric_network_connected") or m.get("energy", 0) <= 0 for m in machines):
            self.last_proof_failure = {"reason": "machine_unpowered"}
            return None
        if any(m.get("products_finished", 0) < 1 for m in machines):
            self.last_proof_failure = {"reason": "machine_has_not_produced"}
            return None
        chest = actual[(line["tail"]["chest"]["name"], *xy(line["tail"]["chest"]))]
        stock = (chest.get("inventory") or {}).get(parameters["item"], 0)
        if stock < 1:
            self.last_proof_failure = {"reason": "stock_chest_empty"}
            return None
        return {
            "tick": obs["tick"],
            "machine_units": [m.get("unit_number") for m in machines],
            "machine_products_finished": [m.get("products_finished") for m in machines],
            "chest_unit": chest.get("unit_number"),
            "chest_stock": stock,
            "rated_per_minute": line["rated_per_minute"],
            "stock_rate_per_minute": line["stock_rate_per_minute"],
            "bar_slots": line["bar_slots"],
            "scope": "machine output reached the capped stock chest; nameplate rate is not a measured rate",
        }
