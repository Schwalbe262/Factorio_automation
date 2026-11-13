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
from .power_grid import generating
from .power_grid import plan_connection as power_connection
from .smelting import merge_entities
from .paired_mining import prepare_new_bank, lane_evidence
from .steam_layout import rank_variants, reserved_area, water_connected
from .steam_siting import board_areas, candidate_layouts, known_rows
from .transport import plan_connection, xy, underground_proof

TAP_ROUTE_ATTEMPTS = 4  # native routes from trunk tap rows to one stub variant
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
    def __init__(self, game, builder, catalog, outposts, site=None, stage=1):
        self.game, self.builder, self.catalog, self.outposts = game, builder, catalog, outposts
        self.site = site
        self.stage = stage  # power_growth: stage 1 is the roadmap's pair, stage k one more pair each
        name = "steam-expansion.json" if stage == 1 else f"steam-expansion-{stage}.json"
        self.path = Path(game.cfg.runtime_dir) / name
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @property
    def block(self) -> str:
        stage = getattr(self, "stage", 1)
        return "steam_expansion" if stage == 1 else f"steam_expansion_{stage}"

    def _stage_states(self):
        yield self.state
        for path in sorted(self.path.parent.glob("steam-expansion-*.json")):
            if path != self.path:
                try:
                    yield json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue

    def owns_burner(self, e, obs):
        """The new boilers (every stage) and their burner inserters belong to steam growth, not to starter fuel upkeep."""
        if e["name"] not in ("boiler", "burner-inserter"):
            return False
        return any(
            state.get("world_id") == obs["world_id"]
            and any(
                w["name"] == e["name"] and w["position"] == e["position"]
                for w in state.get("core", {}).get("entities", [])
            )
            for state in self._stage_states()
        )

    def prepare(self, obs):
        """Plan the pair, its paired coal bank, coal belt, water link and poles; no action."""
        engines = [e for e in obs["entities"] if e["name"] == "steam-engine"]
        if not engines or not any(generating(e) for e in engines):
            # A wait, not a planning failure (seed 1188507449: the boiler had run out of coal and
            # upkeep refuelled it in the same step); ``step`` reports it as waiting.
            return {"ok": False, "reason": "running_source_plant_required", "wait": True}
        board = json.loads((self.path.parent / "pcb-board.json").read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "steam_board_world_changed"}
        failures, rejected = [], {}
        for layout in candidate_layouts(self.game, self.builder, obs, self.site, board, rejected):
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
        if failures:
            return {"ok": False, "reason": "steam_expansion_layout_unroutable", "failures": failures[:6]}
        from . import plan_needs
        from .steam_siting import MAX_WATER_RUN_LIMIT

        reason = "steam_expansion_no_buildable_bank_slot"
        return {
            "ok": False,
            "reason": reason,
            "rejected_slots": dict(sorted(rejected.items())[:12]),
            "plan_needs": plan_needs.make(
                "site",
                reason,
                at=next((e["position"] for e in engines), None),
                rejected_by=rejected,
                tried=sum(rejected.values()) or None,
                change="a free steam-bank site near the plant with a water run of at most "
                f"{MAX_WATER_RUN_LIMIT} tiles from the plant's water network (or a second offshore pump)",
            ),
        }

    def _prepare_layout(self, obs, layout, board):
        """One layout: coal from the coal patch trunk (a priority tap), else a dedicated coal bank."""
        tapped = self._trunk_layout(obs, layout, board)
        if tapped.get("ok"):
            return tapped
        banked = self._bank_layout(obs, layout, board)
        if banked.get("ok") or tapped.get("reason") == "steam_coal_no_patch_trunk":
            return banked
        return dict(banked, trunk_failure={k: tapped.get(k) for k in ("reason", "rows_rejected", "failures")})

    def _trunk_layout(self, obs, layout, board):
        """Coal from a ``serve="first"`` tap on the coal patch's trunk (mine_trunk rule: one block per
        patch, consumers on taps; eval-1575138435: every dedicated coal bank site failed
        no_safe_bounded_mining_exit while the coal trunk ran 60/min past the base)."""
        from . import mine_trunk
        from .board import blocks_without_bus, surface_conflict
        from .core.deterministic_builder import boiler_coal_per_minute

        root = self.path.parent
        mining_path = root / "mining-outposts.json"
        mining = json.loads(mining_path.read_text(encoding="utf-8")) if mining_path.exists() else {}
        if mining.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "steam_coal_no_patch_trunk"}
        bank = mine_trunk.patch_bank(mining.get("banks"), "coal", mine_trunk.patch_seed(obs, "coal"))
        if bank is None:
            return {"ok": False, "reason": "steam_coal_no_patch_trunk"}
        key = mine_trunk.trunk_key(bank)
        name = f"steam:coal:{getattr(self, 'stage', 1)}"
        blocks = blocks_without_bus(board)
        failure = {"ok": False, "reason": "steam_coal_trunk_tap_unroutable", "failures": []}
        for found in rank_variants(layout["variants"], bank["plan"]["ports"][0]["position"]):
            core = _plan(
                found["entities"],
                [found["coal_port"]],
                reserved_area=reserved_area(found["entities"]),
                template=TEMPLATE,
                layout=layout_record(layout, found),
            )
            chosen, attempts = {}, [0]

            def accept(tap, core=core):
                if chosen or attempts[0] >= TAP_ROUTE_ATTEMPTS:
                    return False
                if surface_conflict(self.builder, obs, blocks, tap["entities"]) is not None:
                    return False
                check = preflight(
                    self.game, self.builder, obs, tap["entities"], ignored_units=tuple(tap["remove_units"])
                )
                if not check.get("ok"):
                    return False
                attempts[0] += 1
                route = plan_connection(
                    self.game, self.builder, obs, tap["port"], core["ports"][0], core["entities"] + tap["entities"]
                )
                if route.get("ok"):
                    chosen.update(tap=tap, route=route)
                    return True
                failure["failures"] = (failure["failures"] + [route.get("reason")])[-4:]
                return False

            planned = mine_trunk.plan_tap(
                bank,
                obs,
                owner="steam_expansion",
                name=name,
                demand=boiler_coal_per_minute(self.catalog),
                serve="first",
                stations=mine_trunk.stations(root, obs["world_id"], key, exclude=name),
                accept=accept,
            )
            if not chosen:
                failure["rows_rejected"] = planned.get("rows_rejected")
                continue
            result = self._finish_tap(obs, layout, found, core, chosen["tap"], chosen["route"], board)
            if result.get("ok"):
                return result
            failure = dict(failure, reason=result.get("reason", failure["reason"]))
        return failure

    def _finish_tap(self, obs, layout, found, core, tap, route, board):
        """Poles, water audit and preflight for a trunk-fed variant (the tap itself is preflighted)."""
        es = merge_entities(core["entities"], route["entities"])
        pole = next(p for p in found["bank"]["ports"] if p["item"] == "electricity")
        power = power_connection(
            self.game,
            self.builder,
            obs,
            pole["position"],
            [e for e in es if e["name"] != "small-electric-pole"] + list(tap["entities"]),
        )
        if not power.get("ok"):
            return {**power, "reason": power.get("reason", "steam_pair_power_unavailable")}
        es = _merge_new(es, power["entities"])
        if not water_connected(known_rows(self.builder, board) + es, layout["old_boiler"], found["boiler"]):
            return {"ok": False, "reason": "steam_new_boiler_water_not_on_old_network"}
        check = preflight(self.game, self.builder, obs, es)
        if not check.get("ok"):
            return check
        plan = _plan(
            es,
            core["ports"],
            underground_pairs=route["underground_pairs"],
            clear_obstacles=check["clear_obstacles"],
            clearance_world_id=obs["world_id"],
            layout_policy="pcb-additive-steam-v2",
        )
        return {
            "ok": True,
            "core": core,
            "coal_source": "trunk_tap",
            "tap": tap,
            "plan": plan,
            "power_wire_hops": power.get("wire_hops"),
        }

    def _bank_layout(self, obs, layout, board):
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
            if prepared.get("wait"):
                return {
                    "status": "waiting",
                    "reason": "waiting for the running steam plant to generate again (boiler coal and water)",
                    "evidence": {"wait_for": "running_source_plant_required"},
                }
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
        if self.block not in board["blocks"]:
            core = self.state["core"]
            if core.get("template") == LEGACY_TEMPLATE and "startup_science" in board["blocks"]:
                # The old fixed layout borrowed the startup cell's eastern edge.
                area = board["blocks"]["startup_science"]["reserved_area"]
                area[1][0] = min(area[1][0], core["reserved_area"][0][0])
            board["blocks"][self.block] = dict(self.state["plan"], reserved_area=self.state["core"]["reserved_area"])
            _atomic_json(path, board)
        if self.state.get("coal_source") == "trunk_tap":
            pending = self._cut_over_tap(obs)
            if pending is not None:
                return pending
            result = self.builder.ensure_plan(obs, self.state["plan"])
            if result.get("status") != "succeeded":
                return result
            return self._sample_to_proof(obs)
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
        return self._sample_to_proof(obs)

    def _cut_over_tap(self, obs):
        """Record the coal trunk tap (its demand grows the coal bank), release and replace the trunk belt."""
        from . import mine_trunk

        root = self.path.parent
        tap = self.state["tap"]
        mining_path = root / "mining-outposts.json"
        mining = json.loads(mining_path.read_text(encoding="utf-8")) if mining_path.exists() else {}
        bank = mine_trunk.find_bank(mining.get("banks"), tap["trunk"])
        if bank is None or not bank.get("ready"):
            return {"status": "blocked", "reason": "mine_trunk_patch_bank_missing", "trunk": tap["trunk"]}
        area = mine_trunk.tap_area([e for e in tap["entities"] if e["name"] != "splitter"] or tap["entities"])
        block = (self.block + "_coal_tap", dict(mine_trunk.tap_plan(tap), reserved_area=area, owner=self.block))
        result = mine_trunk.record_tap(root, obs["world_id"], tap, block=block)
        if result:
            return result
        if mine_trunk.release_trunk_belts(root, obs["world_id"], tap) and self.outposts is not None:
            self.outposts.state = json.loads(mining_path.read_text(encoding="utf-8"))
        action = mine_trunk.cutover(self.builder, obs, tap)
        if tap.get("built"):
            mine_trunk.record_tap(root, obs["world_id"], tap)
        _atomic_json(self.path, self.state)
        return action

    def _sample_to_proof(self, obs):
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
        if self.state.get("coal_source") == "trunk_tap":
            return {
                "status": "succeeded",
                "reason": "paid additive steam generation has coal from a priority coal trunk tap and shared water",
                "evidence": proof,
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
        if self.state.get("coal_source") == "trunk_tap":
            from .mine_trunk import tap_standing

            if not tap_standing(obs, self.state["tap"]):
                return None
        else:
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
            "dedicated_coal_nominal_per_minute": (self.state.get("bank") or {}).get("nominal_rate_per_minute"),
            "coal_source": self.state.get("coal_source", "dedicated_bank"),
            "layout": self.state.get("core", {}).get("layout") or {"kind": "legacy_fixed_layout", "degraded": False},
            "scope": "native generation from both new engines over thirty-second sampled window; nominal added capacity is not a full-load stress test",
        }
