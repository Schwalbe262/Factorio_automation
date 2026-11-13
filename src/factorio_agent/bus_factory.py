"""Read-only whole migration preparation and ordinary resumable bus cutover.

Only ``step`` persists state or asks the executor for an action. ``prepare``
never changes the game, saved production plans, or runtime checkpoints.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .main_bus import BusMaterial, ConsumerTap, plan_main_bus
from .manufacturing import connect_power_groups, lab_green_entry, raw_demand
from .planning import preflight
from .smelting import merge_entities, bank_capacities
from .transport import VECTORS, plan_connection, underground_proof, xy


def identity(e):
    return e["name"], *xy(e), e.get("direction", 0), e.get("belt_to_ground_type")


def trace_corridor(plan, source, destination):
    """Follow one exact saved belt path, including explicit underground pairs.

    A splitter, a missing pair, or a loop fails closed. Caller chooses which
    endpoint tiles to retain. No ownership is inferred from proximity.
    """
    by_position = {
        xy(e): e for e in plan["entities"] if e["name"] in ("transport-belt", "underground-belt", "splitter")
    }
    pairs = {tuple(p["input"]): p for p in plan.get("underground_pairs", [])}
    current, goal = xy(source), xy(destination)
    result, visited = [], set()
    while current not in visited and len(result) <= len(by_position):
        visited.add(current)
        e = by_position.get(current)
        if e is None:
            return {"ok": False, "reason": "saved_corridor_has_gap", "position": list(current)}
        if e["name"] == "splitter":
            return {
                "ok": False,
                "reason": "saved_corridor_requires_explicit_splitter_branch",
                "position": list(current),
            }
        result.append(e)
        if current == goal:
            return {"ok": True, "entities": result}
        if e.get("belt_to_ground_type") == "input":
            pair = pairs.get(current)
            if not pair or pair["direction"] != e["direction"]:
                return {"ok": False, "reason": "saved_corridor_underground_pair_missing", "position": list(current)}
            endpoint = by_position.get(tuple(pair["output"]))
            if (
                not endpoint
                or endpoint["name"] != "underground-belt"
                or endpoint.get("belt_to_ground_type") != "output"
                or endpoint["direction"] != e["direction"]
            ):
                return {"ok": False, "reason": "saved_corridor_underground_pair_changed", "position": list(current)}
            current = tuple(pair["output"])
        else:
            dx, dy = VECTORS[e["direction"]]
            current = current[0] + dx, current[1] + dy
    return {"ok": False, "reason": "saved_corridor_loop"}


def match_recoveries(obs, wanted):
    """Bind saved owned specifications to current exact native unit identities."""
    actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
    bound = []
    for spec in merge_entities(wanted):
        live = actual.get((spec["name"], *xy(spec)))
        # Destruction or earlier normal recovery leaves nothing to remove.
        # A different entity at this tile is never granted ownership by this
        # omission; whole native preflight still treats it as protected.
        if live is None:
            continue
        if identity(live) != identity(spec) or type(live.get("unit_number")) is not int:
            return {
                "ok": False,
                "reason": "owned_migration_entity_changed",
                "entity": spec["name"],
                "position": spec["position"],
            }
        bound.append(
            {k: live[k] for k in ("name", "position", "direction", "unit_number")}
            | ({"belt_to_ground_type": live["belt_to_ground_type"]} if "belt_to_ground_type" in live else {})
        )
    return {"ok": True, "entities": bound}


def _replan(plan, entities, *, pairs=None, **extra):
    metadata = {
        k: v
        for k, v in plan.items()
        if k not in ("ok", "reason", "entities", "ports", "required_items", "underground_pairs")
    }
    metadata.update(extra)
    return _plan(
        entities,
        plan.get("ports", []),
        underground_pairs=plan.get("underground_pairs", []) if pairs is None else pairs,
        **metadata,
    )


class BusFactory:
    def __init__(self, game, builder, catalog, smelting, science, green):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.smelting, self.science, self.green = smelting, science, green
        self.path = Path(game.cfg.runtime_dir) / "bus-factory.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _load(self, component):
        path = getattr(component, "path", None)
        return (
            json.loads(Path(path).read_text(encoding="utf-8"))
            if path and Path(path).exists()
            else deepcopy(component.state)
        )

    def prepare(self, obs, parameters=None):
        if parameters not in (None, {}):
            return {"ok": False, "reason": "main_bus_factory_takes_no_parameters"}
        old_smelting, old_red, old_green = (self._load(c) for c in (self.smelting, self.science, self.green))
        if any(
            s.get("world_id") != obs["world_id"] or s.get("catalog_fingerprint") != self.catalog.fingerprint
            for s in (old_smelting, old_red, old_green)
        ):
            return {"ok": False, "reason": "migration_source_world_or_catalog_changed"}
        if old_green.get("compact_main_bus"):
            return {"ok": False, "reason": "compact_main_bus_already_published_use_existing_checkpoint"}
        if not all(obs.get("enabled_recipes", {}).get(r) for r in ("logistic-science-pack", "fast-inserter")):
            return {"ok": False, "reason": "native_green_and_fast_inserter_recipes_required"}
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"ok": False, "reason": "migration_board_identity_changed"}
        sources = {p["item"]: p for p in old_smelting["plan"]["ports"] if p["item"] in ("iron-plate", "copper-plate")}
        if (
            set(sources) != {"iron-plate", "copper-plate"}
            or xy(sources["iron-plate"]) != (-9.5, -29.5)
            or xy(sources["copper-plate"]) != (-9.5, -17.5)
        ):
            return {"ok": False, "reason": "fixed_smelting_plate_interface_changed"}
        old_raw, paths = [], []
        red_inputs = {p["item"]: p for p in old_red["science"]["ports"] if p["direction"] == "input"}
        green_inputs = {p["item"]: p for p in old_green["core"]["ports"] if p["direction"] == "input"}
        for item, source in sources.items():
            trace = trace_corridor(old_red["plan"], source, red_inputs[item])
            if not trace.get("ok"):
                return {**trace, "stage": "trace_old_red", "item": item}
            old_raw.extend(trace["entities"][1:-1])
            paths.append({"consumer": "red", "item": item, "entities": trace["entities"]})
            branch = dict(source, position={"x": source["position"]["x"], "y": source["position"]["y"] - 1}, facing=0)
            trace = trace_corridor(old_green["plan"], branch, green_inputs[item])
            if not trace.get("ok"):
                return {**trace, "stage": "trace_old_green", "item": item}
            old_raw.extend(trace["entities"][:-1])
            paths.append({"consumer": "green", "item": item, "entities": trace["entities"]})
        entry = lab_green_entry(old_red["labs"])
        old_output = next(p for p in old_green["core"]["ports"] if p["direction"] == "output")
        trace = trace_corridor(old_green["plan"], old_output, entry["port"])
        if not trace.get("ok"):
            return {**trace, "stage": "trace_old_green_output"}
        old_raw.extend(trace["entities"][1:-1])
        splitters = []
        for item, source in sources.items():
            x, y = xy(source)
            wanted = next(
                (
                    e
                    for e in old_smelting["plan"]["entities"]
                    if e["name"] == "splitter" and xy(e) == (x - 1, y - 0.5) and e["direction"] == 4
                ),
                None,
            )
            if not wanted:
                return {"ok": False, "reason": "owned_old_plate_source_splitter_missing", "item": item}
            splitters.append(wanted)
        retired_specs = merge_entities(old_raw, splitters)
        recovery = match_recoveries(obs, retired_specs)
        if not recovery.get("ok"):
            return recovery
        ignored = {e["unit_number"] for e in recovery["entities"]}
        # Missing obsolete hardware must still disappear from authoritative
        # plans, otherwise a later maintenance job would rebuild the old run.
        remove_keys = {identity(e) for e in retired_specs}
        virtual = {**obs, "entities": [e for e in obs["entities"] if e.get("unit_number") not in ignored]}
        adapter = SimpleNamespace(
            _occupied_by_plan=self.builder._occupied_by_plan,
            can_place=lambda es: preflight(self.game, self.builder, obs, es, ignored_units=ignored),
        )
        try:
            rates = bank_capacities(self.catalog, old_smelting["banks"])
        except ValueError as error:
            return {"ok": False, "reason": "invalid_smelting_bank_capacity", "detail": str(error)}
        if not {"iron-ore", "copper-ore"} <= set(rates):
            return {"ok": False, "reason": "migration_smelting_banks_incomplete"}
        materials = (
            BusMaterial("iron-plate", -11.5, (rates["iron-ore"],), 2),
            BusMaterial("copper-plate", -3.5, (rates["copper-ore"],), 2),
        )
        bus = plan_main_bus(
            materials,
            (
                ConsumerTap("green-copper", "copper-plate", -79.5, "critical", demand_per_minute=52.5),
                ConsumerTap("green-iron", "iron-plate", -76.5, "critical", demand_per_minute=192.5),
                ConsumerTap("red-iron", "iron-plate", -87.5, "critical", demand_per_minute=72),
                ConsumerTap("red-copper", "copper-plate", -102.5, "critical", demand_per_minute=36),
            ),
            north_y=-118.5,
            south_y=-12.5,
            port_x=12.5,
            foreign_entities=virtual["entities"],
        )
        if not bus.get("ok"):
            return {**bus, "stage": "main_bus"}
        from .compact_green import compact_green_core, preflight_compact_green, audit_lab_injection

        core = compact_green_core(43.5, -78.5)
        compact_check = preflight_compact_green(self.game, self.builder, obs, core, self.catalog)
        if not compact_check.get("ok"):
            return {**compact_check, "stage": "compact_production_audit_and_reservation"}
        lab_audit = audit_lab_injection(old_red["labs"], entry)
        if not lab_audit.get("ok"):
            return {"ok": False, "reason": "compact_lab_injection_audit_failed", "audit": lab_audit}
        restored = [
            entity("transport-belt", source["position"]["x"] - 1, source["position"]["y"], 4)
            for source in sources.values()
        ]
        source_entities = merge_entities(
            [e for e in old_smelting["plan"]["entities"] if identity(e) not in remove_keys], restored
        )
        source_plan = _replan(old_smelting["plan"], source_entities)
        es = merge_entities(restored, bus["entities"], core["entities"], entry["entities"])
        check = preflight(self.game, self.builder, obs, es, ignored_units=ignored)
        if not check.get("ok"):
            return {**check, "stage": "main_bus_and_compact_core"}
        obstacles = {(e["name"], *xy(e)): e for e in check["clear_obstacles"]}
        bus_ports = {p["name"]: p for p in bus["ports"]}
        core_inputs = {p["item"]: p for p in core["ports"] if p["direction"] == "input"}
        pairs = [
            ("source-copper", sources["copper-plate"], bus_ports["copper-plate:0:input"]),
            ("source-iron", sources["iron-plate"], bus_ports["iron-plate:0:input"]),
        ]
        pairs += [
            ("green-" + item, bus_ports["green-" + item.split("-")[0]], target) for item, target in core_inputs.items()
        ]
        pairs += [
            ("red-" + item, bus_ports["red-" + item.split("-")[0]], target) for item, target in red_inputs.items()
        ]
        pairs.append(("green-output", next(p for p in core["ports"] if p["direction"] == "output"), entry["port"]))
        routes = []
        for name, source, destination in pairs:
            route = plan_connection(self.game, adapter, virtual, source, destination, es, ignored_units=ignored)
            if not route.get("ok"):
                return {
                    **route,
                    "stage": "adapter",
                    "connection": name,
                    "source": source["position"],
                    "destination": destination["position"],
                }
            routes.append(dict(route, connection_name=name))
            es = merge_entities(es, route["entities"])
            obstacles.update({(e["name"], *xy(e)): e for e in route["clear_obstacles"]})
        groups = list(core.get("cells", {}).values()) or [core]
        power = connect_power_groups(self.game, self.builder, virtual, groups, es)
        if not power.get("ok"):
            return {**power, "stage": "compact_power"}
        es = merge_entities(es, power["entities"])
        check = preflight(self.game, self.builder, obs, es, ignored_units=ignored)
        if not check.get("ok"):
            return {**check, "stage": "whole_migration"}
        obstacles.update({(e["name"], *xy(e)): e for e in check["clear_obstacles"]})
        ug_pairs = bus["underground_pairs"] + core.get("underground_pairs", []) + [entry["pair"]]
        ug_pairs += [pair for route in routes for pair in route["underground_pairs"]]
        plan = _plan(
            es,
            core["ports"],
            reserved_area=core["reserved_area"],
            underground_pairs=ug_pairs,
            clear_obstacles=list(obstacles.values()),
            clearance_world_id=obs["world_id"],
            layout_policy="pcb-shared-main-bus-compact-green-v1",
        )
        # Red retains every original producer/lab/science-output asset. Only its
        # traced raw corridors are replaced by the shared source/bus adapters.
        red_kept = [e for e in old_red["plan"]["entities"] if identity(e) not in remove_keys]
        red_extra = merge_entities(
            bus["entities"], *(r["entities"] for r in routes if r["connection_name"].startswith(("source-", "red-")))
        )
        red_entities = merge_entities(red_kept, red_extra)
        red_positions = {(e["name"], *xy(e)) for e in red_entities}
        red_pairs = [
            p
            for p in old_red["plan"].get("underground_pairs", [])
            if ("underground-belt", *p["input"]) in red_positions
            and ("underground-belt", *p["output"]) in red_positions
        ]
        red_pairs += bus["underground_pairs"] + [
            p for r in routes if r["connection_name"].startswith(("source-", "red-")) for p in r["underground_pairs"]
        ]
        red_plan = _replan(old_red["plan"], red_entities, pairs=red_pairs)
        protected = {identity(e) for e in merge_entities(source_plan["entities"], red_entities, es, entry["entities"])}
        cleanup_specs = [
            e
            for e in old_green["plan"]["entities"]
            if e["name"] != "small-electric-pole" and identity(e) not in protected and identity(e) not in remove_keys
        ]
        cleanup = match_recoveries(obs, cleanup_specs)
        if not cleanup.get("ok"):
            return {**cleanup, "stage": "old_green_retirement"}
        cleanup["entities"].sort(
            key=lambda e: (0 if e["name"] in ("assembling-machine-1", "inserter", "fast-inserter") else 1, xy(e))
        )
        return {
            "ok": True,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "phase": "recover",
            "plan": plan,
            "bus": bus,
            "core": core,
            "source_plan": source_plan,
            "red_plan": red_plan,
            "remove": recovery["entities"],
            "cleanup": cleanup["entities"],
            "old_green_reserved_area": old_green["core"]["reserved_area"],
            "lab_join": old_green["lab_join"],
            "requested_rate": 30,
            "nominal_machine_rate": 35,
            "rated_demand": raw_demand(self.catalog, "logistic-science-pack", 35),
            "compact_audit": compact_check["audit"],
            "lab_injection_audit": lab_audit,
            "route_metrics": [{"connection": r["connection_name"], **r["route_metrics"]} for r in routes],
            "clearance_count": len(obstacles),
            "prepared_tick": obs["tick"],
        }

    def _save(self):
        _atomic_json(self.path, self.state)

    def _recover(self, obs, key):
        """Prune completed identities before asking to mine any replacement tile."""
        pending = self.state.get(key, [])
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        remaining, actions = [], []
        for wanted in pending:
            live = actual.get((wanted["name"], *xy(wanted)))
            if live is None:
                continue
            if live["unit_number"] != wanted["unit_number"] or identity(live) != identity(wanted):
                return {
                    "status": "blocked",
                    "reason": "bus_recovery_identity_changed",
                    "entity": wanted["name"],
                    "position": wanted["position"],
                }
            remaining.append(wanted)
            if len(actions) < 8:
                actions.append(
                    {
                        "type": "mine",
                        "name": wanted["name"],
                        "position": wanted["position"],
                        "count": 1,
                        "expected_entity_unit": wanted["unit_number"],
                        "expected_entity_world_id": obs["world_id"],
                    }
                )
        if remaining != pending:
            self.state[key] = remaining
            self._save()
        if actions:
            return {
                "type": "mine_many",
                "actions": actions,
                "reason": "normally recover exact owned obsolete corridors"
                if key == "remove"
                else "normally retire the superseded western green production block",
            }
        return None

    def _publish(self):
        s = self.state
        # Other completed jobs may have expanded producer banks after this
        # coordinator object was constructed; retain their latest metadata.
        self.smelting.state = self._load(self.smelting)
        self.science.state = self._load(self.science)
        self.smelting.state["plan"] = s["source_plan"]
        _atomic_json(self.smelting.path, self.smelting.state)
        self.science.state["plan"] = s["red_plan"]
        _atomic_json(self.science.path, self.science.state)
        self.green.state = {
            k: deepcopy(s[k])
            for k in (
                "world_id",
                "catalog_fingerprint",
                "plan",
                "core",
                "source_plan",
                "lab_join",
                "requested_rate",
                "nominal_machine_rate",
                "rated_demand",
                "route_metrics",
            )
        }
        self.green.state.update(ok=True, phase="build", published=True, remove=[], compact_main_bus=True)
        _atomic_json(self.green.path, self.green.state)
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        board["blocks"]["smelting_district"] = s["source_plan"]
        board["blocks"]["logistic_science"] = s["plan"]
        board["blocks"]["retiring_logistic_science"] = {"reserved_area": s["old_green_reserved_area"], "entities": []}
        board["shared_main_bus"] = s["bus"]
        _atomic_json(path, board)
        s["published"] = True
        self._save()

    def step(self, obs, parameters=None):
        if not self.state:
            prepared = self.prepare(obs, parameters)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "bus_factory_world_or_catalog_changed"}
        if self.state["phase"] == "recover":
            result = self._recover(obs, "remove")
            if result:
                return result
            self.state["phase"] = "build"
            self._save()
        if not self.state.get("published"):
            self._publish()
        if self.state["phase"] in ("build", "commission", "verified"):
            result = self.builder.ensure_plan(obs, self.state["plan"])
            if result.get("status") != "succeeded":
                return result
            proof = self.proof(obs)
            if not proof:
                self.state["phase"] = "commission"
                self._save()
                return {
                    "status": "waiting",
                    "reason": "waiting for every compact machine, typed bus priorities and all eight lab stocks",
                }
            self.state["commissioned"] = proof
            self.state["phase"] = "retire"
            self._save()
        if self.state["phase"] == "retire":
            replacement = self.proof(obs)
            if not replacement:
                return {
                    "status": "waiting",
                    "reason": "replacement_main_bus_or_compact_production_unhealthy_hold_old_factory_retirement",
                }
            self.state["commissioned"] = replacement
            result = self._recover(obs, "cleanup")
            if result:
                return result
            board_path = self.path.parent / "pcb-board.json"
            board = json.loads(board_path.read_text(encoding="utf-8"))
            board["blocks"].pop("retiring_logistic_science", None)
            _atomic_json(board_path, board)
            self.state["phase"] = "verified"
            self.green.state["phase"] = "verified"
            _atomic_json(self.green.path, self.green.state)
            self._save()
        return {
            "status": "succeeded",
            "reason": "shared main bus and compact green commissioned; obsolete western production normally recovered",
            "evidence": self.state.get("commissioned"),
        }

    def proof(self, obs):
        s = self.state
        if not s or s["world_id"] != obs["world_id"] or not underground_proof(obs, s["plan"]):
            return None
        source = self.smelting.proof(obs)
        if not source:
            return None
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        machines = []
        for spec in s["plan"]["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            if not live or live.get("direction", 0) != spec.get("direction", 0):
                return None
            if spec.get("recipe"):
                if live.get("recipe") != spec["recipe"] or live.get("products_finished", 0) < 1:
                    return None
                machines.append(live)
            if spec["name"] in ("assembling-machine-1", "inserter", "fast-inserter"):
                if not live.get("electric_network_connected") or live.get("energy", 0) <= 0:
                    return None
            if spec["name"] == "splitter" and any(
                live.get(field) != spec[field]
                for field in ("splitter_input_priority", "splitter_output_priority")
                if field in spec
            ):
                return None
        if len(machines) != 12:
            return None
        lines = []
        for trunk in s["bus"]["trunks"]:
            belts = [
                actual[(e["name"], *xy(e))]
                for e in s["bus"]["entities"]
                if e["name"] in ("transport-belt", "underground-belt")
                and xy(e)[0] == trunk["x"]
                and e["direction"] == 0
            ]
            contents = {}
            for belt in belts:
                for item, amount in belt.get("belt_inventory", {}).items():
                    contents[item] = contents.get(item, 0) + amount
            if set(contents) - {trunk["item"]} or contents.get(trunk["item"], 0) <= 0:
                return None
            lines.append({"item": trunk["item"], "line": trunk["line"], "observed_contents": contents})
        from .science_growth import built_labs

        labs = [actual.get(("lab", *xy(e))) for e in built_labs(self.science.state)]
        if not labs or any(
            not e
            or not e.get("electric_network_connected")
            or e.get("energy", 0) <= 0
            or e.get("inventory", {}).get("logistic-science-pack", 0) <= 0
            for e in labs
        ):
            return None
        green = self.green.proof(obs)
        red = self.science.proof(obs)
        if not green or not red:
            return None
        return {
            "tick": obs["tick"],
            "compact_machine_units": [e["unit_number"] for e in machines],
            "typed_main_bus_lines": lines,
            "native_splitter_priorities": [row["splitter"] for row in s["bus"]["stations"]],
            "source_smelting": source,
            "red": red,
            "green": green,
            "scope": "normal production, typed shared buses and all-lab commissioning; no sustained-rate certification",
        }
