"""``produce``: a generic recipe-driven assembler line ending in a bounded stock chest.

Contract (``jobs.CAPABILITIES["produce"]``):

* parameters ``{"item": <recipe product>, "rate_per_minute": (0, MAX_RATE]}`` and
  optionally ``"output"``: ``stock_chest`` (default) or ``belt`` (the row's output
  belt end is published as a free belt port another row routes from; no chest,
  so it is no construction stock: ``machine_stock`` ignores it);
* the product recipe, an assembling machine, the row inserter (fastest
  enabled of ``RowLimits.inserter_items_per_minute``), long-handed inserters
  for a two-belt line, a chest and belts/poles must be enabled (checked at
  step time, reported as ``production_row_recipes_locked``);
* layout: ``production_layout`` (main row, optional one-level sub-row or a
  lane-merge adapter for three belt-fed ingredients, stock tail or belt output), sized for ``min(rate, stock tail rate)`` with ``blocks.size_row``
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
  in place (``production_layout.extend_line``); beyond them it is blocked;
* layout candidates: ``layout_variants.candidates`` (library + generated, one per
  niche) ranked by ``layout_selection.score`` against the demand (request, upcoming
  roadmap units, early-game plate scarcity); ``production_site.place_line`` probes
  them on the site and keeps the choice in the state's ``layout`` record
  (``chosen_tick``, ``verified_tick`` once the proof passes; exploration opt-in via
  ``layout_selection.settings``).

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
from .production_layout import (
    OUTPUT_MODES,
    audit_line,
    design_line,
    extend_line,
    fluid_inputs,
    input_demand,
    is_belt_output,
    rows,
    unsourced_raw,
)
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


OPTIONAL = ("output", "fluid_source")
SOURCE_KEY = re.compile(r"[a-z][a-z0-9-]{0,47}")


def validate_parameters(parameters) -> None:
    """Structure only; recipe support is checked against the catalog at step time."""
    if (
        not isinstance(parameters, dict)
        or not {"item", "rate_per_minute"} <= set(parameters)
        or set(parameters) - {"item", "rate_per_minute", *OPTIONAL}
    ):
        raise ValueError("produce requires item and rate_per_minute (optional: output)")
    if parameters.get("output", "stock_chest") not in OUTPUT_MODES:
        raise ValueError(f"produce output must be one of {', '.join(OUTPUT_MODES)}")
    if "fluid_source" in parameters and (
        not isinstance(parameters["fluid_source"], str) or not SOURCE_KEY.fullmatch(parameters["fluid_source"])
    ):
        raise ValueError("produce fluid_source must be a fluid-sources.json registry key")
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


def _fluid_recipe(catalog, recipe: str) -> bool:
    return any(r.get("type") == "fluid" for r in (catalog.recipes.get(recipe) or {}).get("ingredients", ()))


BELT_KEY = "__belt"


def row_key(parameters: dict) -> str:
    """State/block key of a request: the item for a stock row, ``<item>__belt`` for a belt row.

    An item may have both (engine units: the engine row's stock chest feeds construction and
    blue science, a belt row feeds electric engines); ``machine_stock`` reads the stock one."""
    return parameters["item"] + (BELT_KEY if output_mode(parameters) == "belt" else "")


def output_mode(parameters: dict) -> str:
    return parameters.get("output", "stock_chest")


def state_output(state: dict) -> str:
    return "belt" if is_belt_output(state.get("line") or {}) else "stock_chest"


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
        pipes = ("pipe", "pipe-to-ground") if recipe and _fluid_recipe(self.catalog, recipe) else ()
        missing = [
            name for name in (recipe, "transport-belt", "small-electric-pole", *pipes) if name and name not in enabled
        ]
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
        owner = BLOCK_PREFIX + row_key(parameters)
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
            output=output_mode(parameters),
        )
        ranked, found, demand = self._candidates(obs, item, arguments, needs["enabled"])
        try:
            line = ranked[0]["line"] if ranked else design_line(self.catalog, **arguments)
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
        fluid = fluid_inputs(line)
        if fluid and not parameters.get("fluid_source"):
            # A fluid row reads one published outlet (production_fluids); the request names it.
            return {"ok": False, "reason": "production_row_fluid_source_required", "item": fluid[0]["item"]}
        for port in line["inputs"]:
            if port.get("kind") == "fluid":
                continue  # routed from the registry outlet by production_site (production_fluids)
            found = resolve(board, item, port["item"], owner=owner)
            if not found["ok"]:
                tap = tap_parameters(board, item, port["item"], input_demand(self.catalog, line, port["item"]))
                if tap is None:
                    return {"ok": False, **found}
                return {"ok": False, "reason": "production_row_needs_bus_tap", "tap": tap, "item": port["item"]}
        return {
            "ok": True,
            "line": line,
            "arguments": arguments,
            "rate": rate,
            "candidates": ranked,
            "demand": demand,
            "layout_library": found.get("library"),
            "layout_skipped": found.get("skipped"),
        }

    def _candidates(self, obs, item: str, arguments: dict, enabled: list[str]):
        """Audited candidate lines ranked by their site-free score (``layout_selection.score``)."""
        from . import layout_selection as selection
        from .layout_variants import candidates

        found = candidates(self.catalog, dict(arguments))
        long_ok = "long-handed-inserter" in enabled
        usable = [
            c
            for c in found["candidates"]
            if long_ok or not any(n.startswith("long-handed") for row in rows(c["line"]) for n in row["required_items"])
        ]
        demand = selection.demand_for(
            arguments["rate"], upcoming_units=self._upcoming_units(obs, item), tick=obs.get("tick")
        )
        usable.sort(key=lambda c: (selection.score(c["descriptor"], demand)[0], c["descriptor"]["id"]))
        return usable, found, demand

    def prepare(self, obs, parameters: dict) -> dict:
        validate_parameters(parameters)
        board = self._board(obs)
        if board is None:
            return {"ok": False, "reason": "production_row_board_identity_changed"}
        designed = self.design(obs, board, parameters)
        if not designed["ok"]:
            return designed
        from . import layout_selection as selection

        owner = BLOCK_PREFIX + row_key(parameters)
        design = dict(
            designed["line"],
            arguments=designed["arguments"],
            candidates=designed.get("candidates"),
            demand=designed.get("demand"),
            layout_settings=selection.settings(self.root),
            layout_seed=selection.seed_for(obs["world_id"], owner),
            layout_library=designed.get("layout_library"),
            layout_skipped=designed.get("layout_skipped"),
            fluid_source=parameters.get("fluid_source"),
        )
        placed = place_line(
            self.game,
            self.builder,
            obs,
            self.catalog,
            self.site,
            board,
            design,
            owner=owner,
            returns_to_bus=output_mode(parameters) == "belt",
        )
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
        name = BLOCK_PREFIX + state.get("key", state["item"])
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

    def _upcoming_units(self, obs, item: str):
        """Units of ``item`` the next roadmap steps place (``supply_demand.item_demand``), or None when unknown."""
        from .supply_cell import DEMAND_HORIZON
        from .supply_demand import item_demand

        root = self.path(item).parent
        try:
            ledger = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
            jobs = [job for job in ledger.get("jobs", {}).values() if job.get("world_id") == obs.get("world_id")]
            row = item_demand(self.catalog, jobs, obs, horizon=DEMAND_HORIZON, root=root).get(item)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        return row.get("count") if isinstance(row, dict) else None

    def _bar(self, obs, state: dict) -> dict | None:
        from .blocks.stock_tail import BAR_RECHECK_TICKS, adaptive_stacks

        if is_belt_output(state["line"]):
            return None
        # The bar follows the item's upcoming demand (belts are used hundreds at
        # a time); it is looked at again every BAR_RECHECK_TICKS.
        checked = state.get("bar_tick")
        tick = obs.get("tick", 0)
        if state.get("bar_set") and isinstance(checked, (int, float)) and 0 <= tick - checked < BAR_RECHECK_TICKS:
            return None
        chest = state["line"]["tail"]["chest"]
        payload = json.dumps(json.dumps({"name": chest["name"], "position": chest["position"]}))
        native = self.game.query(BAR_LUA.replace("PAYLOAD", payload))
        if not isinstance(native, dict) or not native.get("ok"):
            return {"status": "blocked", **(native if isinstance(native, dict) else {"reason": "bar_query_failed"})}
        stack = (self.catalog.items.get(state["item"]) or {}).get("stack_size")
        slots = max(
            state["line"]["bar_slots"],
            adaptive_stacks(self._upcoming_units(obs, state["item"]), stack, native.get("size")),
        )
        slots = min(slots, native.get("size") or slots)
        if native.get("slots") != slots:
            return {"type": "bar", "name": chest["name"], "position": chest["position"], "slots": slots}
        state["bar_set"] = True
        state["bar_tick"] = tick
        self.save(state.get("key", state["item"]), state)
        return None

    def _extend(self, obs, state: dict, rate: float) -> dict | None:
        """Fill reserved slots for a higher rate; None when nothing changes."""
        line = state["line"]
        per = line["main"]["rated_per_minute"] / line["main"]["count"]
        arm = line["tail"].get("arm")
        target = min(rate, inserter_rate(self.catalog, arm["name"])) if arm else rate
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
        self.save(state.get("key", state["item"]), state)
        return None

    def step(self, obs, parameters: dict) -> dict:
        validate_parameters(parameters)
        item = parameters["item"]
        key = row_key(parameters)
        state = self.state(key)
        if not state:
            prepared = self.prepare(obs, parameters)
            if prepared.get("reason") == "production_row_needs_bus_tap":
                return self._install_tap(obs, prepared["tap"])
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            state = dict(prepared, key=key)
            self.save(key, state)
        if state["world_id"] != obs["world_id"] or state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "production_row_world_or_catalog_changed"}
        if output_mode(parameters) != state_output(state):
            # One row per item: a stock row is not rebuilt as a belt row in place.
            return {
                "status": "blocked",
                "reason": "production_row_output_mode_differs",
                "built": state_output(state),
                "requested": output_mode(parameters),
            }
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
        routed = self._route_taps(obs, key, state)
        if routed is not None:
            return routed
        clear = state["plan"].get("clear_obstacles", [])
        cut = planned_tap.cutover(self.root, self.builder, obs, taps, clear_obstacles=clear)
        if cut is not None:
            return cut
        from . import production_fluids

        # A fluid row: declare its source edge before building, bind it once the first pipe stands.
        handoff = production_fluids.handoff_step(self.game, self.catalog, obs, item, state.get("fluid_handoff"))
        if handoff is not None:
            return handoff
        result = self.builder.ensure_plan(obs, state["plan"])
        if result.get("status") != "succeeded":
            return result
        bar = self._bar(obs, state)
        if bar is not None:
            return bar
        evidence = self.proof(obs, parameters)
        if evidence is None:
            where = "on the output belt" if is_belt_output(state["line"]) else "in the stock chest"
            return {"status": "waiting", "reason": f"waiting for machine-made {item} {where}"}
        state["phase"] = "verified"
        if isinstance(state.get("layout"), dict) and state["layout"].get("verified_tick") is None:
            state["layout"]["verified_tick"] = obs.get("tick")
        self.save(key, state)
        reason = (
            f"belt-fed assemblers put {item} on a published output belt"
            if is_belt_output(state["line"])
            else f"belt-fed assemblers stock {item} in a capped chest"
        )
        return {"status": "succeeded", "reason": reason, "evidence": evidence}

    def _route_taps(self, obs, key: str, state: dict) -> dict | None:
        """Cut over the row's splitter taps on other blocks' routes (``production_sources.route_tap_sites``).

        The tapped belt leaves every owner's saved plan and board copy first
        (``mine_trunk.release_trunk_belts``, plus the other production rows' states), so no proof or
        repair rebuilds it; then ``mine_trunk.cutover`` stocks, mines that one belt by unit, builds and
        configures the splitter. One action or status, or None once every tap stands."""
        from . import mine_trunk

        for tap in state.get("route_taps") or []:
            if not tap.get("released"):
                mine_trunk.release_trunk_belts(self.root, obs["world_id"], tap)
                self._release_row_belts(obs, key, tap)
                tap["released"] = True
                self.save(key, state)
            built = bool(tap.get("built"))
            action = mine_trunk.cutover(self.builder, obs, tap)
            if tap.get("built") and not built:
                self.save(key, state)
            if action is not None:
                return action
        return None

    def _release_row_belts(self, obs, key: str, tap: dict) -> list[str]:
        """Drop ``tap``'s replaced belts from the other production rows' saved plans (idempotent)."""
        wanted = {(p["x"], p["y"]) for p in tap["remove"]}
        changed = []
        for path in sorted(self.root.glob(f"{STATE_PREFIX}*.json")):
            other = path.name[len(STATE_PREFIX) : -len(".json")]
            if other == key:
                continue
            row = self.state(other)
            plan = row.get("plan") if isinstance(row, dict) else None
            if row.get("world_id") != obs["world_id"] or not isinstance(plan, dict):
                continue
            kept = [e for e in plan.get("entities", []) if not (e["name"] == "transport-belt" and xy(e) in wanted)]
            if len(kept) != len(plan.get("entities", [])):
                row["plan"] = dict(plan, entities=kept)
                row["plan"].setdefault("released_to_route_taps", []).append(tap["name"])
                self.save(other, row)
                changed.append(other)
        return changed

    def proof(self, obs, parameters: dict) -> dict | None:
        self.last_proof_failure = None
        state = self.state(row_key(parameters))
        if not state or state["world_id"] != obs["world_id"] or not underground_proof(obs, state["plan"]):
            return None
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        released = planned_tap.released_tiles(self.root, obs["world_id"])
        for wanted in state["plan"]["entities"]:
            live = actual.get((wanted["name"], *xy(wanted)))
            if live is None and planned_tap.released_belt_stands(actual, released, wanted):
                continue  # a later bus tap's splitter replaced this lane-extension belt
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
        from . import production_fluids

        if not production_fluids.handoff_bound(self.root, obs, parameters["item"], state.get("fluid_handoff")):
            self.last_proof_failure = {"reason": "fluid_handoff_not_bound"}
            return None
        machines = [actual[(e["name"], *xy(e))] for row in rows(line) for e in row["entities"] if e.get("recipe")]
        if any(not m.get("electric_network_connected") or m.get("energy", 0) <= 0 for m in machines):
            self.last_proof_failure = {"reason": "machine_unpowered"}
            return None
        if any(m.get("products_finished", 0) < 1 for m in machines):
            self.last_proof_failure = {"reason": "machine_has_not_produced"}
            return None
        if is_belt_output(line):
            return {
                "tick": obs["tick"],
                "machine_units": [m.get("unit_number") for m in machines],
                "machine_products_finished": [m.get("products_finished") for m in machines],
                "output_port": dict(line["output"]["position"]),
                "rated_per_minute": line["rated_per_minute"],
                "scope": "every machine finished a product onto the output belt; nameplate rate is not a measured rate",
            }
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
