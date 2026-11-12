"""An additive normally fueled boiler/engine pair; preserve the running plant.

Rules:

* The new pair is the next ``steam_bank`` bank of the first plant, derived
  from the builder's saved ``power_plan`` (``steam_layout``/``steam_siting``):
  same rotation and pitch, on whichever end of the row is buildable; only
  when neither end is buildable a free site near the plant is used and the
  layout is reported as degraded. No map coordinates are fixed here.
* Water is shared with the old boiler through pipes audited to join its
  water network; coal comes from a dedicated paired coal bank whose belt
  enters the coal stub variant facing it; the new poles join the running
  network through the shared pole planner.
* A saved state is never re-planned: states from the old fixed layout keep
  their core, plan and board block unchanged.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .planning import preflight
from .power_grid import plan_connection as power_connection
from .smelting import merge_entities
from .paired_mining import prepare_new_bank, lane_evidence
from .steam_layout import rank_variants, reserved_area, water_connected
from .steam_siting import board_areas, candidate_layouts, known_rows
from .transport import plan_connection, xy, underground_proof

LEGACY_TEMPLATE = "additional_shared_water_steam_pair"  # fixed development-map layout (old states only)
TEMPLATE = "steam_bank_next_pair"
BANK_PLAN_KEYS = (
    "ok",
    "reason",
    "entities",
    "ports",
    "required_items",
    "clear_obstacles",
    "clearance_world_id",
    "underground_pairs",
)


def layout_record(layout: dict, found: dict) -> dict:
    """What the evidence reports about where the pair went and why."""
    record = {
        "kind": layout["kind"],
        "rotation": layout["rotation"],
        "coal_entry": found["entry"],
        "old_boiler": deepcopy(layout["old_boiler"]["position"]),
        "new_boiler": deepcopy(found["boiler"]["position"]),
    }
    for key in ("side", "index", "site", "water_run_tiles"):
        if key in layout:
            record[key] = deepcopy(layout[key])
    record["degraded"] = layout["kind"] != "adjacent_bank"
    return record


def _merge_new(entities: list[dict], extra: list[dict]) -> list[dict]:
    """Add rows on tiles not already planned (pole planners restate the goal pole)."""
    known = {(e["name"], *xy(e)) for e in entities}
    return entities + [e for e in extra if (e["name"], *xy(e)) not in known]


class SteamExpansion:
    def __init__(self, game, builder, catalog, outposts, site=None):
        self.game, self.builder, self.catalog, self.outposts = game, builder, catalog, outposts
        self.site = site
        self.path = Path(game.cfg.runtime_dir) / "steam-expansion.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def owns_burner(self, e, obs):
        """The new boiler and its burner inserter belong to this job, not to starter fuel upkeep."""
        return (
            self.state.get("world_id") == obs["world_id"]
            and e["name"] in ("boiler", "burner-inserter")
            and any(
                w["name"] == e["name"] and w["position"] == e["position"]
                for w in self.state.get("core", {}).get("entities", [])
            )
        )

    def prepare(self, obs):
        """Plan the pair, its paired coal bank, coal belt, water link and poles; no action."""
        engines = [e for e in obs["entities"] if e["name"] == "steam-engine"]
        if not engines or not any(e.get("energy", 0) > 0 for e in engines):
            return {"ok": False, "reason": "running_source_plant_required"}
        board = json.loads((self.path.parent / "pcb-board.json").read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "steam_board_world_changed"}
        failures = []
        for layout in candidate_layouts(self.game, self.builder, obs, self.site, board):
            result = self._prepare_layout(obs, layout, board)
            if result.get("ok"):
                return dict(
                    result,
                    world_id=obs["world_id"],
                    catalog_fingerprint=self.catalog.fingerprint,
                    samples=[],
                    added_nominal_kw=2 * self.catalog.entities["steam-engine"]["energy_production"] * 60 / 1000,
                    preserved_engine_units=[e["unit_number"] for e in engines],
                )
            failures.append({"kind": layout["kind"], "side": layout.get("side"), **result})
        reason = "steam_expansion_layout_unroutable" if failures else "steam_expansion_no_buildable_bank_slot"
        return {"ok": False, "reason": reason, "failures": failures[:6]}

    def _prepare_layout(self, obs, layout, board):
        """One layout: coal bank, then stub variants in coal-facing order until one routes."""
        union = [e for found in layout["variants"] for e in found["entities"]]
        reservations = board_areas(board) + [reserved_area(union)]
        bank = prepare_new_bank(self.game, self.builder, obs, "coal", 2, reservations)
        if not bank.get("ok"):
            return bank
        source = bank["plan"]["ports"][0]
        failure = {"ok": False, "reason": "steam_no_coal_stub_variant_available"}
        for found in rank_variants(layout["variants"], source["position"]):
            core = _plan(
                found["entities"],
                [found["coal_port"]],
                reserved_area=reserved_area(found["entities"]),
                template=TEMPLATE,
                layout=layout_record(layout, found),
            )
            result = self._finish(obs, layout, found, core, bank, board)
            if result.get("ok"):
                return result
            failure = result
        return failure

    def _finish(self, obs, layout, found, core, bank, board):
        """Coal belt, pole bridge, water audit and whole-block preflight for one variant."""
        es = merge_entities(core["entities"], bank["plan"]["entities"])
        route = plan_connection(self.game, self.builder, obs, bank["plan"]["ports"][0], core["ports"][0], es)
        if not route.get("ok"):
            return dict(route, source=bank["plan"]["ports"][0], destination=core["ports"][0])
        es = merge_entities(es, route["entities"])
        pole = next(p for p in found["bank"]["ports"] if p["item"] == "electricity")
        power = power_connection(
            self.game, self.builder, obs, pole["position"], [e for e in es if e["name"] != "small-electric-pole"]
        )
        if not power.get("ok"):
            return {**power, "reason": power.get("reason", "steam_pair_power_unavailable")}
        es = _merge_new(es, power["entities"])
        if not water_connected(known_rows(self.builder, board) + es, layout["old_boiler"], found["boiler"]):
            return {"ok": False, "reason": "steam_new_boiler_water_not_on_old_network"}
        check = preflight(self.game, self.builder, obs, es)
        if not check.get("ok"):
            return check
        pairs = route["underground_pairs"]
        plan = _plan(
            es,
            core["ports"],
            resource_cell=True,
            underground_pairs=pairs,
            clear_obstacles=check["clear_obstacles"],
            clearance_world_id=obs["world_id"],
            layout_policy="pcb-additive-steam-v2",
        )
        bank = dict(bank)
        bank["plan"] = _plan(
            merge_entities(bank["plan"]["entities"], route["entities"]),
            bank["plan"]["ports"],
            **{k: v for k, v in bank["plan"].items() if k not in BANK_PLAN_KEYS},
            underground_pairs=pairs,
            clear_obstacles=check["clear_obstacles"],
            clearance_world_id=obs["world_id"],
        )
        return {"ok": True, "core": core, "bank": bank, "plan": plan, "power_wire_hops": power.get("wire_hops")}

    def step(self, obs):
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            _atomic_json(self.path, self.state)
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "steam_expansion_world_or_catalog_changed"}
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "steam_expansion_board_world_changed"}
        if "steam_expansion" not in board["blocks"]:
            core = self.state["core"]
            if core.get("template") == LEGACY_TEMPLATE and "startup_science" in board["blocks"]:
                # The old fixed layout borrowed the startup cell's eastern edge.
                area = board["blocks"]["startup_science"]["reserved_area"]
                area[1][0] = min(area[1][0], core["reserved_area"][0][0])
            board["blocks"]["steam_expansion"] = dict(
                self.state["plan"], reserved_area=self.state["core"]["reserved_area"]
            )
            _atomic_json(path, board)
        bank = self.state["bank"]
        if bank.get("layout_contract"):
            # Publish the mining-only growth area separately from its clear exit.
            saved = (
                json.loads(self.outposts.path.read_text(encoding="utf-8"))
                if self.outposts.path.exists()
                else deepcopy(self.outposts.state)
            )
            if (
                saved.get("world_id") != obs["world_id"]
                or saved.get("catalog_fingerprint", self.catalog.fingerprint) != self.catalog.fingerprint
            ):
                return {"status": "blocked", "reason": "steam_coal_registry_identity_changed"}
            matches = [b for b in saved.get("banks", []) if b["plan"]["ports"] == bank["plan"]["ports"]]
            if len(matches) > 1 or (matches and matches[0]["plan"] != bank["plan"]):
                return {"status": "blocked", "reason": "steam_fixed_coal_bank_plan_changed"}
            if not matches:
                saved.setdefault("banks", []).append(dict(deepcopy(bank), ready=False, power_only=True))
                _atomic_json(self.outposts.path, saved)
            self.outposts.state = saved
            reservation = self.outposts.publish_reservations(obs)
            if reservation:
                return reservation
        from .mining_layout_policy import validate_retained_units

        retained = validate_retained_units(obs, bank)
        if not retained.get("ok"):
            return dict(retained, status="blocked")
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        sample = self.sample(obs)
        if not sample or not sample.get("ok"):
            self.state["samples"] = []
            _atomic_json(self.path, self.state)
            temporary = {"boiler_waiting_for_automatic_coal_or_water", "engine_waiting_for_steam_or_load"}
            if sample and sample.get("reason") and sample["reason"] not in temporary:
                return {"status": "blocked", "reason": "steam_native_probe_failed", "detail": sample}
            return {
                "status": "waiting",
                "reason": "waiting for automatic coal, water and native generation from both new engines",
            }
        history = self.state["samples"]
        if history and not 0 < sample["tick"] - history[-1]["tick"] <= 600:
            history.clear()
        if history and [e["unit_number"] for e in sample["engines"]] != [
            e["unit_number"] for e in history[-1]["engines"]
        ]:
            history.clear()
        history.append(sample)
        while len(history) > 2 and history[1]["tick"] <= sample["tick"] - 1800:
            history.pop(0)
        _atomic_json(self.path, self.state)
        proof = self.proof(obs)
        if not proof:
            return {
                "status": "waiting",
                "reason": "sampling native generation from the new engine pair for thirty game seconds",
            }
        bank = self.state["bank"]
        bank["ready"] = True
        bank["power_only"] = True
        matches = [b for b in self.outposts.state["banks"] if b["plan"]["ports"] == bank["plan"]["ports"]]
        if matches and bank.get("layout_contract"):
            matches[0].update(deepcopy(bank))
            _atomic_json(self.outposts.path, self.outposts.state)
        elif not matches:
            self.outposts.state["banks"].append(bank)
            _atomic_json(self.outposts.path, self.outposts.state)
        return {
            "status": "succeeded",
            "reason": "paid additive steam generation has automatic dedicated coal and shared normal water",
            "evidence": proof,
        }

    def sample(self, obs):
        if not underground_proof(obs, self.state["plan"]):
            return None
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        for wanted in self.state["plan"]["entities"]:
            e = actual.get((wanted["name"], *xy(wanted)))
            if not e:
                return None
            if wanted["name"] == "steam-engine":
                if e["direction"] % 8 != wanted.get("direction", 0) % 8:
                    return None
            elif wanted["name"] not in ("pipe", "small-electric-pole") and e["direction"] != wanted.get("direction", 0):
                return None
        bank = lane_evidence(self.game, obs, self.state["bank"])
        if not bank.get("ok"):
            return None
        payload = json.dumps(json.dumps(self.state["core"], separators=(",", ":")))
        return self.game.query(
            """
local core=helpers.json_to_table("""
            + payload
            + """);local result={ok=true,tick=game.tick,engines={}}
local function fluid(e,name) local contents=e.get_fluid_contents();return contents[name] or 0 end
for _,spec in ipairs(core.entities) do
 local e=target(spec.position,spec.name)
 if not e then return {ok=false} end
 if e.name=="boiler" then
  local fuel=e.get_fuel_inventory();result.boiler_coal=fuel and fuel.get_item_count("coal") or 0
  result.water=fluid(e,"water");if result.boiler_coal<=0 or result.water<=0 then return {ok=false,reason="boiler_waiting_for_automatic_coal_or_water"} end
 elseif e.name=="steam-engine" then
  local generated=e.energy_generated_last_tick;local steam=fluid(e,"steam")
  if generated<=0 or steam<=0 or not e.is_connected_to_electric_network() then return {ok=false,reason="engine_waiting_for_steam_or_load"} end
  result.engines[#result.engines+1]={unit_number=e.unit_number,kw=generated*60/1000,steam=steam,network=e.electric_network_id}
 end
end
return result
"""
        )

    def proof(self, obs):
        if not self.state or self.state["world_id"] != obs["world_id"]:
            return None
        history = self.state["samples"]
        if len(history) < 2 or history[-1]["tick"] - history[0]["tick"] < 1800:
            return None
        if not all(
            any(e.get("unit_number") == unit and e["name"] == "steam-engine" for e in obs["entities"])
            for unit in self.state["preserved_engine_units"]
        ):
            return None
        fresh = self.sample(obs)
        if (
            not fresh
            or not fresh.get("ok")
            or [e["unit_number"] for e in fresh["engines"]] != [e["unit_number"] for e in history[-1]["engines"]]
        ):
            return None
        if not 0 <= fresh["tick"] - history[-1]["tick"] <= 600:
            return None
        return {
            "tick": fresh["tick"],
            "from_tick": history[0]["tick"],
            "to_tick": history[-1]["tick"],
            "added_nominal_kw": self.state["added_nominal_kw"],
            "latest_native_sample": fresh,
            "dedicated_coal_nominal_per_minute": self.state["bank"]["nominal_rate_per_minute"],
            "layout": self.state.get("core", {}).get("layout") or {"kind": "legacy_fixed_layout", "degraded": False},
            "scope": "native generation from both new engines over thirty-second sampled window; nominal added capacity is not a full-load stress test",
        }
