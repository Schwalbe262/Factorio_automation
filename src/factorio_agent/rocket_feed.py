"""``rocket_parts`` (feed the silo until a rocket stands ready) and ``rocket_launch`` (launch it).

``rocket_parts`` (``RocketParts``), on the silo ``rocket_silo`` placed and proved:

* the silo crafts parts from its input inventory (``defines.inventory.crafter_input``,
  the 2.x crafting-machine input) and counts them itself (``rocket_parts``);
  this module only reads that counter and the input inventory, never writes them;
* what to deliver comes from the live observation only: parts still to craft
  times the part recipe, minus what the input already holds. No delivery is
  remembered in a state file, so an unknown outcome is reconciled by observing
  again (``jobs.control(..., "reconcile")``) and never replayed;
* stage (i), hand-fed: ``feed_rocket_silo`` (``FEED_SILO_LUA``) moves carried
  ingredients into the input when its slot is at most half full, in stacks
  (one action carries all three items); the carried stock comes from published
  rows (``rocket_stages.StageSupply``, named waits per item);
* stage (ii), belt-fed: when all three ingredients have a free belt output
  port (``production_sources.resolve``: board ports and bus taps; a stock chest
  is no belt source), the reserved feed side of the silo block (three
  inserters, a pole, three belt ports) is routed (``transport.plan_connection``),
  preflighted and published with its ``connections``. Chosen by wait-vs-spend
  (``feed_choice``): build seconds of the feed against the hand trips the rest
  of the rocket needs; the hand feed stays the fallback when the feed cannot be
  planned or its build blocks;
* proof: the silo's own status is ``rocket_ready`` (``defines.rocket_silo_status``).

``rocket_launch`` (``RocketLaunch``) wraps ``core.deterministic_launch``:
``LaunchStage`` decides on the observation (``launch`` receipt of
``OBSERVE_LAUNCH_LUA``), the payload (``LAUNCH_PAYLOAD``) is collected or hand
crafted through the stage supply, and the ``launch`` act (``LAUNCH_LUA``) moves
the carried pack into the rocket, creates the platform request the way the
player's "new platform" button does (free; the hub exists only after the paid
pack is delivered, see ``docs/v2/ROCKET_LAUNCH.md``) and orders the launch with
``LuaEntity.launch_rocket``. Proof: the launch receipt reports ``succeeded``
(ordered by this run, platform hub built from the delivered pack, the force's
``rockets_launched`` above the baseline).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json

from .planning import preflight
from .production_sources import resolve
from .rocket_stages import BLOCK, RocketSilo, StageSupply, live
from .smelting import merge_entities
from .transport import plan_connection

PARTS_STATE = "rocket-parts.json"
LAUNCH_STATE = "rocket-launch.json"
MAX_FEED = 200  # per item and action; the input slot takes at most a stack anyway
HAND_TRIP_SECONDS = 20.0  # one collect walk or one insert walk of the actor
BUILD_SECONDS_PER_ENTITY = 2.0  # paid placement of one feed entity (craft + walk + build)
FEED_RECHECK_TICKS = 3600
LAUNCH_RETRY_TICKS = 600
BELT_SOURCES = ("board_port", "bus_tap")

FEED_SILO_LUA = r"""
local x=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=x.world_id then return failure("rocket_feed_world_changed") end
if not a or not a.valid or a.force~=f then return failure("agent_dead_or_force_changed") end
local inv=a.get_main_inventory()
if not inv or not inv.valid then return failure("character_main_inventory_missing") end
local e=target(x.position,x.name)
if not e or not e.valid or e.type~="rocket-silo" or e.force~=f or e.unit_number~=x.unit_number then return failure("rocket_silo_changed") end
if character_backend and not a.can_reach_entity(e) then return failure("out_of_reach") end
local recipe=e.get_recipe()
if not recipe then return failure("rocket_silo_recipe_missing") end
local wanted={}
for _,ingredient in pairs(recipe.ingredients) do if ingredient.type=="item" then wanted[ingredient.name]=true end end
for _,spec in ipairs(x.items) do
 if not wanted[spec.item] then return failure("rocket_feed_item_not_an_ingredient:"..spec.item) end
end
local dest=e.get_inventory(defines.inventory.crafter_input)
if not dest or not dest.valid then return failure("rocket_silo_input_missing") end
local parts=e.rocket_parts
local rows={}
local total=0
for _,spec in ipairs(x.items) do
 local n=math.min(spec.count,inv.get_item_count{name=spec.item,quality="normal"})
 local moved=0
 if n>0 then
  local inserted=dest.insert{name=spec.item,quality="normal",count=n}
  if inserted>0 then
   local removed=inv.remove{name=spec.item,quality="normal",count=inserted}
   if removed<inserted then dest.remove{name=spec.item,quality="normal",count=inserted-removed} end
   moved=removed
  end
 end
 rows[#rows+1]={item=spec.item,requested=spec.count,moved=moved}
 total=total+moved
end
return success{status=total>0 and "succeeded" or "waiting",moved=total,items=rows,rocket_parts=parts,unit_number=e.unit_number}
"""


def _position(value) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"x", "y"}
        and all(type(value[k]) in (int, float) and math.isfinite(value[k]) for k in ("x", "y"))
    )


def validate_feed(action) -> None:
    """Reject a malformed ``feed_rocket_silo`` envelope before any native I/O."""
    items = action.get("items") if isinstance(action, dict) else None
    if (
        not isinstance(action, dict)
        or set(action) - {"type", "name", "position", "unit_number", "world_id", "items", "reason"}
        or action.get("type") != "feed_rocket_silo"
        or not isinstance(action.get("name"), str)
        or not _position(action.get("position"))
        or type(action.get("unit_number")) is not int
        or action["unit_number"] < 1
        or not isinstance(action.get("world_id"), str)
        or not action["world_id"]
        or not isinstance(items, list)
        or not 1 <= len(items) <= 3
        or ("reason" in action and not isinstance(action["reason"], str))
    ):
        raise ValueError("feed_rocket_silo requires the silo identity, world and 1..3 item rows")
    seen = set()
    for row in items:
        if (
            not isinstance(row, dict)
            or set(row) != {"item", "count"}
            or not isinstance(row["item"], str)
            or row["item"] in seen
            or type(row["count"]) is not int
            or not 1 <= row["count"] <= MAX_FEED
        ):
            raise ValueError("feed_rocket_silo item rows need distinct items and counts 1..200")
        seen.add(row["item"])


def execute_feed_silo(game, action) -> dict:
    """One native query; an inconsistent receipt is an unknown outcome (reconcile, never retry)."""
    validate_feed(action)
    expected = getattr(game, "expected_world", None)
    if expected is not None and expected != action["world_id"]:
        raise ValueError("feed_rocket_silo must target the bound world")
    backend = getattr(game, "backend", "assisted")
    if backend not in ("assisted", "character"):
        raise ValueError("feed_rocket_silo requires an ordinary supported backend")
    body = (
        "local character_backend="
        + ("true" if backend == "character" else "false")
        + ";"
        + FEED_SILO_LUA.replace("PAYLOAD", json.dumps(json.dumps(action, separators=(",", ":"))))
    )
    receipt = game.query(body)
    if not receipt.get("ok"):
        return receipt
    rows = receipt.get("items")
    rows = [] if rows == {} else rows
    requested = {row["item"]: row["count"] for row in action["items"]}
    if (
        type(receipt.get("moved")) is not int
        or not isinstance(rows, list)
        or len(rows) != len(requested)
        or any(
            not isinstance(row, dict)
            or row.get("item") not in requested
            or type(row.get("moved")) is not int
            or not 0 <= row["moved"] <= requested[row["item"]]
            for row in rows
        )
        or sum(row["moved"] for row in rows) != receipt["moved"]
        or receipt.get("status") != ("succeeded" if receipt["moved"] else "waiting")
    ):
        raise RuntimeError("feed_rocket_silo receipt violates conservation; reconcile before retry")
    return dict(receipt, items=rows)


def feed_choice(remaining_parts: int, per_part: dict, stacks: dict, feed_entities: int | None) -> dict:
    """Wait-vs-spend: ``belt`` when building the feed costs less actor time than hand-feeding the rest."""
    trips = sum(math.ceil(remaining_parts * amount / max(1, stacks.get(item, 50))) for item, amount in per_part.items())
    hand = 2 * trips * HAND_TRIP_SECONDS  # one collect and one insert walk per stack
    belt = None if feed_entities is None else feed_entities * BUILD_SECONDS_PER_ENTITY
    mode = "belt" if belt is not None and belt < hand else "hand"
    return {"mode": mode, "hand_seconds": hand, "belt_seconds": belt, "hand_trips": trips}


class RocketParts:
    """``rocket_parts`` (module doc). State: ``rocket-parts.json`` (feed mode only, never deliveries)."""

    def __init__(self, game, builder, bootstrap, catalog, silo: RocketSilo):
        self.game, self.builder, self.bootstrap, self.catalog, self.silo_ctl = game, builder, bootstrap, catalog, silo
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / PARTS_STATE
        self.supply = StageSupply(bootstrap, catalog, self.root)
        self.last_proof_failure = None

    def state(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def save(self, state: dict) -> None:
        _atomic_json(self.path, state)

    def _stack(self, item: str) -> int:
        size = (self.catalog.items.get(item) or {}).get("stack_size")
        return int(size) if isinstance(size, (int, float)) and size >= 1 else 50

    # ----- stage (ii): the belt feed --------------------------------------------------------
    def prepare_feed(self, obs, parameters=None) -> dict:
        """Route the reserved feed side from free belt ports (pure: reads and routes only)."""
        silo, state = self.silo_ctl.silo(obs)
        if not state:
            return {"ok": False, "reason": "rocket_silo_state_missing"}
        board = self.supply.board(obs)
        if board is None:
            return {"ok": False, "reason": "rocket_parts_board_identity_changed"}
        feed = state["layout"]["feed"]
        product = state["spec"]["part_recipe"]
        entities = list(feed["entities"])
        reserved = list(state["plan"]["entities"])
        connections, pairs, clear = [], [], {}
        for port in feed["ports"]:
            found = resolve(board, product, port["item"], owner=BLOCK)
            if not found.get("ok") or found.get("kind") not in BELT_SOURCES:
                return {"ok": False, "reason": "rocket_parts_feed_source_missing", "item": port["item"]}
            route = plan_connection(self.game, self.builder, obs, found["source"], port, reserved + entities)
            if not route.get("ok"):
                return {
                    "ok": False,
                    "reason": route.get("reason", "rocket_parts_feed_route_failed"),
                    "item": port["item"],
                    "failures": [
                        {
                            "item": port["item"],
                            "source": found["source"].get("position"),
                            "destination": port["position"],
                        }
                    ],
                }
            entities = merge_entities(entities, route["entities"])
            pairs += route.get("underground_pairs", [])
            for e in route.get("clear_obstacles") or []:
                clear[(e["name"], e["position"]["x"], e["position"]["y"])] = e
            connections.append(
                {
                    "item": port["item"],
                    "source": found["source"]["position"],
                    "destination": port["position"],
                    "owner": BLOCK,
                }
            )
        check = preflight(self.game, self.builder, obs, entities)
        if not check.get("ok"):
            return {"ok": False, "reason": check.get("reason", "rocket_parts_feed_preflight_failed")}
        for e in check.get("clear_obstacles") or []:
            clear[(e["name"], e["position"]["x"], e["position"]["y"])] = e
        plan = _plan(
            entities,
            feed["ports"],
            underground_pairs=pairs,
            clear_obstacles=list(clear.values()),
            clearance_world_id=obs["world_id"],
            layout_policy="rocket-silo-feed-v1",
        )
        return {"ok": True, "plan": plan, "entities": entities, "ports": feed["ports"], "connections": connections}

    def _mode(self, obs, remaining: int, spec: dict) -> dict:
        state = self.state()
        tick = obs.get("tick", 0)
        if state.get("world_id") == obs["world_id"] and (
            state.get("mode") == "belt"
            or 0 <= tick - state.get("checked_tick", -FEED_RECHECK_TICKS) < FEED_RECHECK_TICKS
        ):
            return state
        prepared = self.prepare_feed(obs)
        stacks = {item: self._stack(item) for item in spec["part_ingredients"]}
        count = len(prepared["entities"]) if prepared.get("ok") else None
        choice = feed_choice(remaining, spec["part_ingredients"], stacks, count)
        state = {"world_id": obs["world_id"], "checked_tick": tick, **choice}
        if choice["mode"] == "belt":
            state["feed"] = {k: prepared[k] for k in ("plan", "ports", "connections")}
            silo_state = self.silo_ctl.state()
            silo_state["feed_plan"] = {
                "entities": prepared["entities"],
                "ports": prepared["ports"],
                "connections": prepared["connections"],
            }
            self.silo_ctl.save(silo_state)  # published with the block before the first feed build
        else:
            state["feed_reason"] = prepared.get("reason") if not prepared.get("ok") else "hand feed is cheaper"
        self.save(state)
        return state

    # ----- step / proof -----------------------------------------------------------------
    def step(self, obs, parameters=None) -> dict:
        silo, silo_state = self.silo_ctl.silo(obs)
        if not silo_state:
            return {"status": "blocked", "reason": "rocket_silo_state_missing"}
        if silo is None:
            return {"status": "blocked", "reason": "rocket_silo_missing"}
        evidence = self.proof(obs, parameters)
        if evidence is not None:
            return {"status": "succeeded", "reason": "rocket ready in the silo", "evidence": evidence}
        spec = silo_state["spec"]
        status = silo.get("rocket_silo_status")
        parts = int(silo.get("rocket_parts") or 0)
        if status not in (None, "building_rocket"):
            return {
                "status": "waiting",
                "reason": "rocket_parts_rocket_being_prepared",
                "evidence": {"rocket_silo_status": status, "rocket_parts": parts},
            }
        if not silo.get("electric_network_connected") or (silo.get("energy") or 0) <= 0:
            return {"status": "waiting", "reason": "rocket_parts_silo_unpowered", "evidence": {"rocket_parts": parts}}
        remaining = max(0, spec["parts_required"] - parts)
        held_in = silo.get("inventory") or {}
        want = {
            item: max(0, math.ceil(remaining * amount) - int(held_in.get(item, 0)))
            for item, amount in sorted(spec["part_ingredients"].items())
        }
        mode = self._mode(obs, remaining, spec)
        if mode.get("mode") == "belt":
            silo_publish = self.silo_ctl._publish(obs, self.silo_ctl.state())
            if silo_publish is not None:
                return silo_publish
            built = self.builder.ensure_plan(obs, mode["feed"]["plan"])
            if built.get("type"):
                return built
            if built.get("status") == "blocked":
                fallback = dict(mode, mode="hand", feed_reason="feed_build_blocked: " + str(built.get("reason"))[:120])
                fallback["checked_tick"] = obs.get("tick", 0)
                self.save(fallback)
            elif built.get("status") != "succeeded":
                return built
        inventory = obs.get("inventory") or {}
        feed = []
        for item, need in want.items():
            stack = self._stack(item)
            inside = int(held_in.get(item, 0))
            carried = int(inventory.get(item, 0))
            if need > 0 and carried > 0 and inside <= stack // 2:
                feed.append({"item": item, "count": min(carried, need, max(1, stack - inside), MAX_FEED)})
        if feed:
            return {
                "type": "feed_rocket_silo",
                "name": silo["name"],
                "position": dict(silo["position"]),
                "unit_number": silo["unit_number"],
                "world_id": obs["world_id"],
                "items": feed,
                "reason": "hand-feed rocket part ingredients into the silo input",
            }
        if self.state().get("mode") == "belt":
            return {
                "status": "waiting",
                "reason": "rocket_parts_belt_fed",
                "evidence": {"rocket_parts": parts, "remaining": remaining, "want": want},
            }
        wait = None
        for item, need in want.items():
            if int(inventory.get(item, 0)) >= need:
                continue
            result = self.supply.next(obs, item, need, stage="rocket_parts", collect_top=True)
            if result.get("type"):
                return result
            wait = wait or result
        if wait is not None:
            return wait
        return {
            "status": "waiting",
            "reason": "rocket_parts_silo_crafting",
            "evidence": {"rocket_parts": parts, "remaining": remaining, "needs": dict(want)},
        }

    def proof(self, obs, parameters=None) -> dict | None:
        self.last_proof_failure = None
        silo, state = self.silo_ctl.silo(obs)
        if silo is None:
            self.last_proof_failure = {"reason": "rocket_silo_missing"}
            return None
        if silo.get("rocket_silo_status") != "rocket_ready":
            self.last_proof_failure = {"reason": "rocket_not_ready", "status": silo.get("rocket_silo_status")}
            return None
        return {
            "tick": obs["tick"],
            "silo_unit": silo.get("unit_number"),
            "rocket_silo_status": "rocket_ready",
            "rocket_parts": silo.get("rocket_parts"),
            "feed": self.state().get("mode", "hand"),
            "scope": "the silo's own status: a rocket stands ready for launch",
        }


class RocketLaunch:
    """``rocket_launch`` (module doc). State: ``rocket-launch.json`` (last launch receipt only)."""

    def __init__(self, game, bootstrap, catalog, silo: RocketSilo):
        self.game, self.bootstrap, self.catalog, self.silo_ctl = game, bootstrap, catalog, silo
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / LAUNCH_STATE
        self.supply = StageSupply(bootstrap, catalog, self.root)

    def state(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def record_action(self, obs, action, receipt) -> None:
        if action.get("type") == "launch":
            _atomic_json(self.path, {"world_id": obs.get("world_id"), "tick": obs.get("tick"), "receipt": receipt})

    def step(self, obs, parameters=None) -> dict:
        from .core.deterministic_launch import LAUNCH_PAYLOAD, LaunchStage

        silo, silo_state = self.silo_ctl.silo(obs)
        view = obs
        if silo is not None:
            others = [
                e for e in obs.get("entities", []) if e.get("type") != "rocket-silo" and e.get("name") != silo["name"]
            ]
            view = dict(obs, entities=others + [silo])
        decision = LaunchStage(self.catalog).next_action(view)
        status, reason = decision["status"], decision["reason"]
        if status == "succeeded":
            evidence = self.proof(obs, parameters)
            if evidence is not None:
                return {
                    "status": "succeeded",
                    "reason": "rocket launched, starter pack delivered",
                    "evidence": evidence,
                }
            return {"status": "waiting", "reason": "rocket_launch_verifying"}
        if status == "running":
            return {"status": "waiting", "reason": "rocket_launch_" + reason}
        if reason == "starter_pack_required":
            return self.supply.next(obs, LAUNCH_PAYLOAD, 1, stage="rocket_launch")
        if reason == "rocket_parts_required":
            return {
                "status": "waiting",
                "reason": "rocket_launch_waits_for_rocket_parts",
                "evidence": {"requirements": decision.get("requirements")},
            }
        if status == "blocked":
            return {"status": "blocked", "reason": reason}
        action = decision.get("action")
        if not action:
            return {"status": "waiting", "reason": "rocket_launch_" + reason}
        if action["type"] == "launch":
            last = self.state()
            receipt = last.get("receipt") or {}
            if (
                last.get("world_id") == obs.get("world_id")
                and 0 <= obs.get("tick", 0) - (last.get("tick") or 0) < LAUNCH_RETRY_TICKS
                and receipt.get("ok")
                and receipt.get("status") == "waiting"
            ):
                return {"status": "waiting", "reason": "rocket_launch_" + str(receipt.get("reason") or "not_ready")}
            return dict(action, reason="launch the ready rocket with the starter pack to a new platform request")
        return dict(action, reason=str(action.get("reason") or "collect the starter pack for the rocket"))

    def proof(self, obs, parameters=None) -> dict | None:
        launch = obs.get("launch") or {}
        if not (
            launch.get("status") == "succeeded"
            and launch.get("ordered")
            and launch.get("platform_hub_valid")
            and isinstance(launch.get("baseline"), int)
            and int(launch.get("rockets_launched") or 0) > launch["baseline"]
        ):
            return None
        return {
            "tick": obs["tick"],
            "rockets_launched": launch["rockets_launched"],
            "baseline": launch["baseline"],
            "platform_index": launch.get("platform_index"),
            "silo_unit_number": launch.get("silo_unit_number"),
            "ordered_tick": launch.get("ordered_tick"),
            "scope": "launch ordered by this run, platform hub built from the delivered starter pack",
        }


def launch_verified(observation) -> bool:
    """The jobs summary's ``rocket_launch_verified``: the live launch receipt, never a saved claim."""
    launch = (observation or {}).get("launch") if isinstance(observation, dict) else None
    return bool(
        isinstance(launch, dict)
        and launch.get("status") == "succeeded"
        and launch.get("ordered")
        and launch.get("platform_hub_valid")
        and isinstance(launch.get("baseline"), int)
        and isinstance(launch.get("rockets_launched"), int)
        and launch["rockets_launched"] > launch["baseline"]
    )


__all__ = ["RocketParts", "RocketLaunch", "execute_feed_silo", "feed_choice", "launch_verified", "live"]
