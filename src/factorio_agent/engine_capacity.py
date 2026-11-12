"""Paid engine-row additions with immutable existing ports and source pins."""

from copy import deepcopy
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .bus_budget import declared_bus_demand
from .bus_tap import signature
from .engine_production import EngineProduction, engine_core, engine_rates, audit_engine_core, SCOPE_ID
from .mining_layout_policy import diagnose_bank_capacity
from .planning import preflight, overlap
from .radar_network import RADAR_POWER_LUA, planned_power_watts
from .smelting import merge_entities
from .steel_capacity import verify_steel_capacity, _hash, _payload, _bound
from .transport import xy, surface_pipe_proof, unbuilt_external_keepouts


MODE = "additive_engine_capacity_v1"
ALLOWED = {"assembling-machine-1", "inserter", "long-handed-inserter", "small-electric-pole", "transport-belt"}
PIN_FIELDS = (
    "iron_source",
    "steel_source",
    "steel_plan_revision",
    "steel_furnace_units",
    "shared_steel_nominal_per_minute",
)


def _key(q):
    return q["name"] + ":" + str(q["position"]["x"]) + ":" + str(q["position"]["y"])


def validate_parameters(parameters):
    if (
        not isinstance(parameters, dict)
        or set(parameters) != {"rate_per_minute"}
        or type(parameters["rate_per_minute"]) not in (int, float)
        or not math.isfinite(parameters["rate_per_minute"])
        or parameters["rate_per_minute"] <= 0
    ):
        raise ValueError("engine_capacity requires positive finite absolute rate_per_minute")


def additive_engine_layout(catalog, old, count):
    anchor = old["core"]["anchor"]
    core = engine_core(catalog, anchor["x"], anchor["y"], count)
    if old["core"] != engine_core(catalog, anchor["x"], anchor["y"], len(old["core"]["engine_machines"])):
        raise ValueError("existing engine core is not the exact installed canonical layout")
    audit = audit_engine_core(core, catalog)
    if not audit["ok"]:
        raise ValueError("expanded engine core audit failed:" + str(audit["errors"]))
    if (
        core["ports"] != old["core"]["ports"]
        or core["reserved_area"] != old["core"]["reserved_area"]
        or core["underground_pairs"] != old["core"]["underground_pairs"]
        or any(q not in core["entities"] for q in old["core"]["entities"])
    ):
        raise ValueError("engine capacity would change an installed core asset or fixed interface")
    prior = {signature(q) for q in old["plan"]["entities"]}
    added = [q for q in core["entities"] if signature(q) not in prior]
    if any(q["name"] not in ALLOWED for q in added):
        raise ValueError("unsupported additive engine entity")
    new_machines = [q for q in added if q["name"] == "assembling-machine-1"]
    input_positions = {(xy(q)[0], xy(q)[1] - 2) for q in new_machines} | {
        (xy(q)[0] - 1, xy(q)[1] - 2) for q in new_machines
    }
    input_arms = [q for q in added if q["name"] in ("inserter", "long-handed-inserter") and xy(q) in input_positions]
    if len(input_arms) != 2 * len(new_machines):
        raise ValueError("two cold-isolated normal input arms required per new engine machine")
    extra = {
        k: deepcopy(v)
        for k, v in old["plan"].items()
        if k not in ("ok", "reason", "entities", "ports", "required_items")
    }
    plan = _plan(merge_entities(old["plan"]["entities"], added), deepcopy(old["plan"]["ports"]), **extra)
    return dict(
        core=core,
        plan=plan,
        audit=audit,
        rated=core["rated"],
        added=added,
        new_machines=new_machines,
        new_input_arms=input_arms,
        cold_plan=_plan([q for q in added if q not in input_arms], []),
    )


def verify_engine_capacity(
    game, obs, catalog, engine, *, pinned_plan_revision, pinned_source, pinned_machine_units=None
):
    """Future Blue consumer hook; validate ancestry without replacing its pins."""
    try:
        state = engine.state
        if (
            state.get("world_id") != obs.get("world_id")
            or state.get("catalog_fingerprint") != catalog.fingerprint
            or state.get("phase") != "verified"
        ):
            raise ValueError("commissioned current-world engine producer required")
        if hasattr(engine, "path"):
            root = Path(engine.path).parent
            board = json.loads((root / "pcb-board.json").read_text())
            if (
                json.loads(Path(engine.path).read_text()) != state
                or board.get("world_id") != obs["world_id"]
                or board.get("catalog_fingerprint") != catalog.fingerprint
                or board.get("blocks", {}).get("engine_production") != state["plan"]
            ):
                raise ValueError("engine canonical publication changed or incomplete")
        actual = {(q["name"], *xy(q)): q for q in obs["entities"]}
        current = _hash(state["plan"])
        revision = pinned_plan_revision
        chain = []
        original = None if pinned_machine_units is None else set(pinned_machine_units)
        if original is not None and (
            not original
            or len(original) != len(pinned_machine_units)
            or any(type(u) is not int or u < 1 for u in original)
        ):
            raise ValueError("positive unique original machine pins required")
        while revision != current:
            matches = [q for q in state.get("capacity_revisions", []) if q.get("old_plan_revision") == revision]
            if len(matches) != 1 or len(chain) >= 8:
                raise ValueError("missing or ambiguous engine capacity ancestry")
            r = matches[0]
            oldkeys = {signature(q) for q in r["old_plan"]["entities"]}
            newkeys = {signature(q) for q in r["new_plan"]["entities"]}
            added = [q for q in r["new_plan"]["entities"] if signature(q) not in oldkeys]
            retained = {(q["name"], *xy(q), q["direction"]) for q in r["retained_units"]}
            produced = r.get("production_witness", {}).get("machines", [])
            if (
                r.get("mode") != MODE
                or r.get("world_id") != obs["world_id"]
                or r.get("catalog_fingerprint") != catalog.fingerprint
                or _hash({k: v for k, v in r.items() if k != "receipt_id"}) != r.get("receipt_id")
                or _hash(r["old_plan"]) != revision
                or _hash(r["new_plan"]) != r["new_plan_revision"]
                or not oldkeys <= newkeys
                or any(q not in r["new_plan"]["entities"] for q in r["old_plan"]["entities"])
                or r["old_plan"]["ports"] != r["new_plan"]["ports"]
                or r["old_plan"].get("reserved_area") != r["new_plan"].get("reserved_area")
                or r["old_plan"].get("underground_pairs") != r["new_plan"].get("underground_pairs")
                or any(q["name"] not in ALLOWED for q in added)
                or r["source"] != pinned_source
                or {(q["name"], *xy(q), q.get("direction", 0)) for q in r["old_plan"]["entities"]} != retained
                or any(
                    r["old_source_pins"][k] != r["new_source_pins"][k] or r["new_source_pins"][k] != state[k]
                    for k in PIN_FIELDS
                )
                or not r.get("production_witness", {}).get("ok")
                or any(q.get("products_finished", 0) <= 0 for q in produced)
                or len(produced) not in (1, 2, 3)
                or len(produced) != len([q for q in added if q["name"] == "assembling-machine-1"])
                or {q["unit_number"] for q in produced} != set(r["added_machine_units"])
                or any(
                    len(set(r[k])) != len(r[k])
                    for k in ("old_machine_units", "current_machine_units", "added_machine_units")
                )
                or set(r["old_machine_units"])
                != {q["unit_number"] for q in r["retained_units"] if q["name"] == "assembling-machine-1"}
                or set(r["current_machine_units"]) - set(r["old_machine_units"]) != set(r["added_machine_units"])
            ):
                raise ValueError("engine receipt is not a paid interface-preserving addition")
            if original is None:
                original = set(r["old_machine_units"])
            if not original <= set(r["old_machine_units"]) <= set(r["current_machine_units"]):
                raise ValueError("original engine machines were not retained")
            for q in r["retained_units"]:
                live = actual.get((q["name"], *xy(q)))
                if not live or live.get("unit_number") != q["unit_number"] or live.get("direction") != q["direction"]:
                    raise ValueError("original engine asset changed")
            revision = r["new_plan_revision"]
            chain.append(r["receipt_id"])
        proof = engine.proof(obs)
        if not proof or proof.get("output") != pinned_source:
            raise ValueError("current native engine health/output missing")
        units = [q["unit_number"] for q in proof["native"]["machines"]]
        if len(units) != len(set(units)) or (chain and set(units) != set(r["current_machine_units"])):
            raise ValueError("current native machine set differs from capacity receipt")
        if original is not None and not original <= set(units):
            raise ValueError("original engine machine pins changed")
        return dict(
            ok=True,
            source=proof["output"],
            proof=proof,
            rated=proof["rated"],
            current_plan_revision=current,
            original_machine_units=sorted(original or units),
            current_machine_units=units,
            chain=chain,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "reason": "engine_additive_capacity_not_verified", "detail": str(exc)}


class EngineCapacity:
    def __init__(self, game, builder, catalog, perimeter, *, engine):
        self.game, self.builder, self.catalog, self.perimeter, self.engine = game, builder, catalog, perimeter, engine
        self.path = Path(game.cfg.runtime_dir) / "engine-capacity.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.last_failure = None

    def _save(self):
        _atomic_json(self.path, self.state)

    def _docs(self):
        return {
            k: json.loads((self.path.parent / n).read_text())
            for k, n in {"engine": "engine-production.json", "board": "pcb-board.json"}.items()
        }

    def _steel(self, obs, old):
        return verify_steel_capacity(
            self.game,
            obs,
            self.catalog,
            self.engine.steel,
            pinned_plan_revision=old["steel_plan_revision"],
            pinned_source=old["steel_source"],
            pinned_furnace_units=old["steel_furnace_units"],
        )

    def _budget(self, obs, old, new):
        try:
            return self._budget_context(obs, old, new)
        except (OSError, KeyError, TypeError, ValueError, StopIteration) as exc:
            return {"ok": False, "reason": "engine_capacity_upstream_context_invalid", "detail": str(exc)}

    def _budget_context(self, obs, old, new):
        steel = self._steel(obs, old)
        if not steel.get("ok"):
            return {"ok": False, "reason": "engine_capacity_verified_steel_ancestry_required", "evidence": steel}
        needed = new["rated"]["steel_per_minute"]
        available = steel["rated"]["steel_per_minute"]
        if available < needed:
            return {
                "ok": False,
                "reason": "expand_automatic_steel_before_engine_capacity",
                "available": available,
                "required": needed,
            }
        docs = self._docs()
        board = docs["board"]
        bus = board["shared_main_bus"]
        source = old["iron_source"]
        tap = next((p for p in bus["ports"] if p.get("name") == source["name"]), None)
        if (
            not tap
            or tap.get("position") != source["position"]
            or tap.get("item") != "iron-plate"
            or tap.get("priority") != "surplus"
        ):
            return {"ok": False, "reason": "engine_capacity_existing_surplus_iron_tap_changed"}
        # The component AMs can burst at180; the original physical tap declares
        # the engine's steady24. Account for growth outside that pinned port.
        allocated = tap.get("demand_per_minute", 0) + sum(
            q["delta_per_minute"]
            for q in board.get("consumer_capacity_allocations", [])
            if q.get("consumer") == "engine_production" and q.get("item") == "iron-plate"
        )
        needed_iron = new["rated"]["iron_per_minute"]
        delta = max(0, needed_iron - allocated)
        if not math.isfinite(allocated) or allocated < old["rated"]["iron_per_minute"]:
            return {"ok": False, "reason": "engine_capacity_existing_iron_demand_accounting_missing"}
        upstream = {}
        for item in ("iron-plate", "copper-plate"):
            supply = bus["capacity"]["materials"][item]["supply_ceiling_per_minute"]
            demand = declared_bus_demand(board, item)
            if type(supply) not in (int, float) or not math.isfinite(supply) or supply <= 0:
                raise ValueError("finite positive bus supply required")
            extra = delta if item == "iron-plate" else 0
            upstream[item] = dict(
                source_nominal=supply,
                declared_peak_before=demand,
                declared_peak_after=demand + extra,
                design_deficit=max(0, demand + extra - supply),
                new_allocation=extra,
                guaranteed_surplus=0,
            )
        if upstream["iron-plate"]["source_nominal"] < needed_iron or needed_iron > 450:
            return {
                "ok": False,
                "reason": "engine_capacity_iron_source_or_single_lane_insufficient",
                "required": needed_iron,
            }
        root = self.path.parent
        supplies = json.loads((root / "material-supply.json").read_text())
        coal = next(q for q in self.engine.steel.state["sources"] if q["item"] == "coal")
        banks = [
            q for q in supplies["supplies"].values() if q.get("material_supply_id") == coal.get("material_supply_id")
        ]
        brick = json.loads((root / "brick-production.json").read_text())
        registry = json.loads((root / "material-sources.json").read_text())
        if (
            any(
                q.get("world_id") != obs["world_id"] or q.get("catalog_fingerprint") != self.catalog.fingerprint
                for q in (supplies, brick, registry)
            )
            or len(banks) != 1
            or banks[0].get("phase") != "verified"
            or brick.get("phase") != "verified"
            or registry.get("sources", {}).get("steel-coal") != coal
            or not any(
                q.get("item") == "coal" and q.get("material_supply_id") == coal["material_supply_id"]
                for q in brick.get("sources", [])
            )
        ):
            return {"ok": False, "reason": "engine_capacity_shared_coal_frame_changed"}
        capacity = diagnose_bank_capacity(self.game, obs, self.catalog, banks[0]["bank"])
        coal_needed = (
            self.engine.steel.state["core"]["rated"]["coal_per_minute_at_max_catalog_usage"]
            + brick["core"]["rated"]["coal_per_minute_at_max_catalog_usage"]
        )
        if (
            not capacity.get("ok")
            or not capacity.get("all_installed_drop_lanes_verified")
            or capacity.get("active_collector_ceiling_per_minute", 0) < coal_needed
        ):
            return {"ok": False, "reason": "engine_capacity_shared_industrial_coal_insufficient", "evidence": capacity}
        return dict(
            ok=True,
            steel_required=needed,
            shared_steel_nameplate=available,
            steel_potential_remaining_before_other_consumers=available - needed,
            steel_ancestry=steel["chain"],
            iron_steady_per_minute=needed_iron,
            iron_declared_before=allocated,
            iron_declared_after=allocated + delta,
            iron_additive_delta=delta,
            component_iron_burst_nominal=new["rated"]["component_nominal_iron_per_minute"],
            shared_bus=upstream,
            coal_shared_potential=capacity["active_collector_ceiling_per_minute"],
            coal_required_with_brick=coal_needed,
            measured_per_minute=None,
            scope="one shared steel source and additive demand on the existing surplus iron tap; finite buffers, no guaranteed sustained rate",
        )

    def _power(self, obs, old, added, board):
        pole = next(q for q in old["core"]["entities"] if q["name"] == "small-electric-pole")
        live = next(q for q in obs["entities"] if q["name"] == pole["name"] and xy(q) == xy(pole))
        source = dict(_bound(live), electric_network_id=live["electric_network_id"], world_id=obs["world_id"])
        watts = sum(
            self.catalog.entities[q["name"]].get("energy_usage", 0) * 60
            for q in added
            if q["name"] in ("assembling-machine-1", "inserter", "long-handed-inserter")
        )
        return self.game.query(
            RADAR_POWER_LUA.replace(
                "PAYLOAD",
                _payload(
                    dict(
                        world_id=obs["world_id"],
                        source=source,
                        preserved_poles=[source],
                        scheduled_watts=planned_power_watts(self.catalog, obs, board),
                        additional_watts=watts,
                    )
                ),
            )
        )

    def prepare(self, obs, parameters):
        validate_parameters(parameters)
        try:
            docs = self._docs()
            old = docs["engine"]
            if (
                any(
                    q.get("world_id") != obs["world_id"] or q.get("catalog_fingerprint") != self.catalog.fingerprint
                    for q in docs.values()
                )
                or old != self.engine.state
                or old.get("phase") != "verified"
                or docs["board"]["blocks"].get("engine_production") != old["plan"]
            ):
                return {"ok": False, "reason": "engine_capacity_matching_commissioned_producer_required"}
            if not obs.get("technologies", {}).get("engine") or not all(
                obs.get("enabled_recipes", {}).get(k)
                for k in (
                    "engine-unit",
                    "assembling-machine-1",
                    "inserter",
                    "long-handed-inserter",
                    "small-electric-pole",
                    "transport-belt",
                )
            ):
                return {"ok": False, "reason": "engine_capacity_normal_recipes_required"}
            count = next(
                (
                    i
                    for i in range(len(old["core"]["engine_machines"]), 5)
                    if engine_rates(self.catalog, i)["engines_per_minute"] >= parameters["rate_per_minute"]
                ),
                None,
            )
            if count is None:
                return {
                    "ok": False,
                    "reason": "engine_capacity_exceeds_four_reserved_slots",
                    "maximum": engine_rates(self.catalog, 4)["engines_per_minute"],
                }
            if count == len(old["core"]["engine_machines"]):
                return {"ok": False, "reason": "engine_capacity_target_already_installed", "rated": old["rated"]}
            old_proof = self.engine.proof(obs)
            if not old_proof:
                return {
                    "ok": False,
                    "reason": "engine_capacity_old_native_production_health_required",
                    "evidence": self.engine.last_proof_failure,
                }
            layout = additive_engine_layout(self.catalog, old, count)
            actual = {(q["name"], *xy(q)): q for q in obs["entities"]}
            retained = []
            for spec in old["plan"]["entities"]:
                live = actual.get((spec["name"], *xy(spec)))
                if (
                    not live
                    or live.get("unit_number") != old["bindings"].get(_key(spec))
                    or live.get("direction") != spec["direction"]
                ):
                    return {
                        "ok": False,
                        "reason": "engine_capacity_original_unit_changed",
                        "position": spec["position"],
                    }
                retained.append(_bound(live))
            if any((q["name"], *xy(q)) in actual for q in layout["added"]):
                return {"ok": False, "reason": "engine_capacity_unregistered_new_asset_already_present"}
            foreign = {k: v for k, v in docs["board"]["blocks"].items() if k != "engine_production"}
            if any(
                overlap(layout["core"]["reserved_area"], q["reserved_area"])
                for q in foreign.values()
                if q.get("reserved_area")
            ) or self.builder._occupied_by_plan(layout["added"]) & unbuilt_external_keepouts(
                self.builder, obs, foreign
            ):
                return {"ok": False, "reason": "engine_capacity_foreign_future_layout_conflict"}
            budget = self._budget(obs, old, layout)
            if not budget.get("ok"):
                return budget
            power = self._power(obs, old, layout["added"], docs["board"])
            if not power.get("ok"):
                return power
            pipe = surface_pipe_proof(self.game, obs, layout["plan"])
            if not pipe.get("ok"):
                return pipe
            whole = preflight(self.game, self.builder, obs, layout["plan"]["entities"])
            if not whole.get("ok"):
                return whole
            layout["cold_plan"].update(clear_obstacles=whole["clear_obstacles"], clearance_world_id=obs["world_id"])
            return dict(
                layout,
                ok=True,
                world_id=obs["world_id"],
                catalog_fingerprint=self.catalog.fingerprint,
                phase="cold",
                parameters=deepcopy(parameters),
                old_state=old,
                retained_units=retained,
                bindings=deepcopy(old["bindings"]),
                baseline={},
                source=old_proof["output"],
                budget=budget,
                power_preview=power,
                old_production_witness=old_proof["native"]["machines"],
                scope_id=SCOPE_ID,
                prepared_tick=obs["tick"],
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "reason": "engine_capacity_prepare_context_invalid", "detail": str(exc)}

    def _check(self, obs):
        s = self.state
        actual = {(q["name"], *xy(q)): q for q in obs["entities"]}
        if s["world_id"] != obs["world_id"] or s["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "engine_capacity_frame_changed"}
        for q in s["retained_units"]:
            live = actual.get((q["name"], *xy(q)))
            if not live or live.get("unit_number") != q["unit_number"] or live.get("direction") != q["direction"]:
                return {
                    "status": "blocked",
                    "reason": "engine_capacity_retained_unit_changed",
                    "position": q["position"],
                }
        for q in s["added"]:
            live = actual.get((q["name"], *xy(q)))
            unit = s["bindings"].get(_key(q))
            if unit and (not live or live.get("unit_number") != unit):
                return {"status": "blocked", "reason": "engine_capacity_new_unit_changed"}
            if live:
                if type(live.get("unit_number")) is not int or live.get("direction") != q["direction"]:
                    return {"status": "blocked", "reason": "engine_capacity_new_configuration_changed"}
                s["bindings"][_key(q)] = live["unit_number"]
        return None

    def _new(self, obs):
        rows = [dict(q, unit_number=self.state["bindings"].get(_key(q))) for q in self.state["new_machines"]]
        result = self.game.query(
            "local x=helpers.json_to_table("
            + _payload(dict(world_id=obs["world_id"], machines=rows))
            + ");"
            + NEW_ENGINE_LUA
        )
        if result.get("ok") and (
            len(result.get("machines", [])) != len(rows)
            or {q.get("unit_number") for q in result["machines"]} != {q["unit_number"] for q in rows}
            or any(
                type(q.get("products_finished")) is not int
                or q["products_finished"] < 0
                or type(q.get("empty_process")) is not bool
                for q in result["machines"]
            )
        ):
            return {"ok": False, "reason": "engine_capacity_new_native_receipt_incomplete"}
        return result

    def _preview(self):
        p = EngineProduction(
            self.game, self.builder, self.catalog, self.perimeter, tap=self.engine.tap, steel=self.engine.steel
        )
        p.state = dict(
            deepcopy(self.state["old_state"]),
            core=self.state["core"],
            plan=self.state["plan"],
            audit=self.state["audit"],
            rated=self.state["rated"],
            bindings=deepcopy(self.state["bindings"]),
        )
        return p

    def _publish(self, obs, proof):
        s = self.state
        if not s.get("publication_intent"):
            docs = self._docs()
            old = s["old_state"]
            if docs["engine"] != old or docs["board"]["blocks"].get("engine_production") != old["plan"]:
                return {"status": "blocked", "reason": "engine_capacity_publish_parent_changed"}
            new = dict(
                deepcopy(old),
                core=s["core"],
                plan=s["plan"],
                audit=s["audit"],
                rated=s["rated"],
                bindings=deepcopy(s["bindings"]),
                phase="verified",
            )
            original_units = [old["bindings"][_key(q)] for q in old["core"]["machines"]]
            units = [s["bindings"][_key(q)] for q in s["core"]["machines"]]
            r = dict(
                mode=MODE,
                world_id=s["world_id"],
                catalog_fingerprint=s["catalog_fingerprint"],
                old_plan_revision=_hash(old["plan"]),
                new_plan_revision=_hash(s["plan"]),
                old_plan=deepcopy(old["plan"]),
                new_plan=deepcopy(s["plan"]),
                retained_units=s["retained_units"],
                source=s["source"],
                old_machine_units=original_units,
                current_machine_units=units,
                added_machine_units=[q["unit_number"] for q in proof["production_witness"]["machines"]],
                old_source_pins={k: deepcopy(old[k]) for k in PIN_FIELDS},
                new_source_pins={k: deepcopy(new[k]) for k in PIN_FIELDS},
                old_production_witness=deepcopy(s.get("old_production_witness", [])),
                production_witness=proof["production_witness"],
                rated=s["rated"],
                upstream_budget=s["budget"],
                commissioning_tick=obs["tick"],
            )
            r["receipt_id"] = _hash(r)
            new["capacity_revisions"] = deepcopy(old.get("capacity_revisions", [])) + [r]
            docs["engine"] = new
            docs["board"]["blocks"]["engine_production"] = s["plan"]
            if s["budget"]["iron_additive_delta"]:
                docs["board"].setdefault("consumer_capacity_allocations", []).append(
                    dict(
                        receipt_id=r["receipt_id"],
                        consumer="engine_production",
                        item="iron-plate",
                        old_per_minute=s["budget"]["iron_declared_before"],
                        new_per_minute=s["budget"]["iron_declared_after"],
                        delta_per_minute=s["budget"]["iron_additive_delta"],
                    )
                )
            docs["board"].setdefault("producer_capacity_allocations", []).append(
                dict(
                    receipt_id=r["receipt_id"],
                    consumer="engine_production",
                    item="steel-plate",
                    old_per_minute=old["rated"]["steel_per_minute"],
                    new_per_minute=s["rated"]["steel_per_minute"],
                    delta_per_minute=s["rated"]["steel_per_minute"] - old["rated"]["steel_per_minute"],
                )
            )
            s["publication_intent"] = {
                name: dict(
                    old_hash=_hash(json.loads((self.path.parent / name).read_text())),
                    new_hash=_hash(docs[key]),
                    value=docs[key],
                )
                for key, name in {"engine": "engine-production.json", "board": "pcb-board.json"}.items()
            }
            s["receipt"] = r
            self._save()
        for name, row in s["publication_intent"].items():
            if _hash(json.loads((self.path.parent / name).read_text())) not in (row["old_hash"], row["new_hash"]):
                return {
                    "status": "blocked",
                    "reason": "engine_capacity_publication_compare_and_swap_failed",
                    "document": name,
                }
        for name, row in s["publication_intent"].items():
            if _hash(json.loads((self.path.parent / name).read_text())) == row["old_hash"]:
                _atomic_json(self.path.parent / name, row["value"])
        self.engine.state = s["publication_intent"]["engine-production.json"]["value"]
        s["published"] = True
        self._save()
        return None

    def step(self, obs, parameters):
        validate_parameters(parameters)
        if self.state and self.state.get("parameters") != parameters:
            if self.state.get("phase") != "verified":
                return {"status": "blocked", "reason": "another_engine_capacity_change_unfinished"}
            if parameters["rate_per_minute"] <= self.state["rated"]["engines_per_minute"]:
                p = self.proof(obs)
                return (
                    {"status": "succeeded", "evidence": p}
                    if p
                    else {"status": "blocked", "reason": "engine_capacity_verified_health_missing"}
                )
            self.state = {}
        if not self.state:
            ready = self.prepare(obs, parameters)
            if not ready.get("ok"):
                return dict(ready, status="blocked")
            self.state = ready
            self._save()
        s = self.state
        issue = self._check(obs)
        if issue:
            return issue
        self._save()
        if s.get("publication_intent") and not s.get("published"):
            result = self._publish(obs, {})
            if result:
                return result
            s["phase"] = "verified"
            self._save()
        if s["phase"] == "verified":
            p = self.proof(obs)
            return (
                {"status": "succeeded", "evidence": p}
                if p
                else {
                    "status": "blocked",
                    "reason": "engine_capacity_verified_health_missing",
                    "evidence": self.last_failure,
                }
            )
        budget = self._budget(obs, s["old_state"], s)
        if not budget.get("ok"):
            return dict(budget, status="blocked")
        s["budget"] = budget
        self._save()
        defense = self.perimeter.ensure_coverage(obs, s["scope_id"], [s["core"]["reserved_area"]])
        if defense is not None:
            return defense
        if not s.get("post_defense_checked"):
            check = preflight(self.game, self.builder, obs, s["plan"]["entities"])
            if not check.get("ok"):
                return dict(check, status="blocked")
            s["post_defense_checked"] = True
            self._save()
        pipes = surface_pipe_proof(self.game, obs, s["plan"])
        if not pipes.get("ok"):
            return dict(pipes, status="blocked")
        if s["phase"] == "cold":
            result = self.builder.ensure_plan(obs, s["cold_plan"])
            if result.get("status") != "succeeded":
                return result
            empty = self._new(obs)
            if not empty.get("ok") or any(
                q["products_finished"] != 0 or not q["empty_process"] for q in empty["machines"]
            ):
                return {
                    "status": "blocked",
                    "reason": "engine_capacity_empty_zero_process_baseline_required",
                    "evidence": empty,
                }
            s["baseline"] = {str(q["unit_number"]): 0 for q in empty["machines"]}
            s["phase"] = "feed"
            self._save()
        result = self.builder.ensure_plan(obs, _plan(s["added"], []))
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs)
        if not proof:
            return {
                "status": "waiting",
                "reason": "waiting_for_both_new_engines_and_complete_normal_feed_health",
                "evidence": self.last_failure,
            }
        result = self._publish(obs, proof)
        if result:
            return result
        s["phase"] = "verified"
        self._save()
        return {
            "status": "succeeded",
            "reason": "paid additive engine capacity commissioned with original pins retained",
            "evidence": proof,
        }

    def proof(self, obs):
        self.last_failure = None
        if not self.state:
            return None
        issue = self._check(obs)
        if issue:
            self.last_failure = issue
            return None
        s = self.state
        if any(_key(q) not in s["bindings"] for q in s["added"]):
            return None
        budget = self._budget(obs, s["old_state"], s)
        if not budget.get("ok"):
            self.last_failure = budget
            return None
        native = self._new(obs)
        if (
            not native.get("ok")
            or not s.get("baseline")
            or any(
                q["products_finished"] <= s["baseline"].get(str(q["unit_number"]), math.inf) for q in native["machines"]
            )
        ):
            self.last_failure = {"reason": "engine_capacity_every_new_machine_must_produce", "native": native}
            return None
        preview = self._preview()
        production = preview.proof(obs)
        if not production or production.get("output") != s["source"]:
            self.last_failure = {
                "reason": "engine_capacity_complete_native_production_missing",
                "evidence": preview.last_proof_failure,
            }
            return None
        return dict(
            tick=obs["tick"],
            production=production,
            production_witness=native,
            new_machine_units=[q["unit_number"] for q in native["machines"]],
            output=s["source"],
            rated=s["rated"],
            upstream_budget=budget,
            scope="actual new recipe machines and unchanged normal sources; catalog capacity is not a sustained-rate measurement",
        )


NEW_ENGINE_LUA = r"""
if not d or d.world_id~=x.world_id then return {ok=false,reason="engine_capacity_world_changed"}end
local rows={}
for _,q in ipairs(x.machines)do local e=target(q.position,"assembling-machine-1");local r=e and e.get_recipe()
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y
  or e.direction~=q.direction or e.quality.name~="normal"or not r or r.name~="engine-unit"
  or not e.is_connected_to_electric_network()or e.energy<=0 then return {ok=false,reason="engine_capacity_new_recipe_unit_or_power_changed"}end
 local input=e.get_inventory(defines.inventory.crafter_input);local output=e.get_output_inventory();local empty=e.crafting_progress==0
 if not input or not output then return {ok=false,reason="engine_capacity_native_inventory_missing"}end
 for index,inv in ipairs({input,output})do for i=1,#inv do local st=inv[i];if st.valid_for_read then
  empty=false;local allowed=index==1 and(st.name=="steel-plate"or st.name=="iron-gear-wheel"or st.name=="pipe")or(index==2 and st.name=="engine-unit")
  if not allowed or st.quality.name~="normal"or st.health~=1 or st.spoil_tick~=0 or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data then return {ok=false,reason="engine_capacity_new_machine_contaminated"}end
 end end end
 rows[#rows+1]={unit_number=e.unit_number,products_finished=e.products_finished,empty_process=empty}
end
return {ok=true,tick=game.tick,machines=rows}
"""
