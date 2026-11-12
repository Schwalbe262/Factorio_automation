"""Paid single-source gas manifold with separate plastic and sulfur handoffs."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .chemical_production import _handoff, plan_typed_fluid_connection, source_owned_apron
from .fluid_handoffs import bound_handoffs
from .industrial import PlanningBudget
from .petroleum import BasicPetroleum, basic_petroleum_rates, pipeline_extent
from .planning import overlap, preflight, survey_slots
from .smelting import merge_entities
from .transport import VECTORS, xy


CONSUMER_ID = "chemistry-gas"
ALIASES = ("plastic-gas", "sulfur-gas")


def _key(e):
    return e["name"] + ":" + str(e["position"]["x"]) + ":" + str(e["position"]["y"])


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def gas_manifold_core(x, y, side=-1):
    """Two normal output pipes and fixed three-cell outward exit contracts."""
    if side not in (-1, 1) or any(type(v) not in (int, float) or not math.isfinite(v) or v % 1 != 0.5 for v in (x, y)):
        raise ValueError("half-tile manifold anchor and north/south branch required")
    pipes = [entity("pipe", x + i, y) for i in range(8)] + [entity("pipe", x + 2, y + side * i) for i in range(1, 4)]
    inlet = {
        "kind": "fluid",
        "item": "petroleum-gas",
        "name": "pipe",
        "direction": "input",
        "facing": 4,
        "position": {"x": x, "y": y},
        "fluidbox_index": 1,
    }
    ports = [
        dict(inlet, direction="output", position={"x": x + 7, "y": y}),
        dict(inlet, direction="output", facing=0 if side < 0 else 8, position={"x": x + 2, "y": y + side * 3}),
    ]
    access = {
        name: [
            {
                "x": p["position"]["x"] + VECTORS[p["facing"]][0] * i,
                "y": p["position"]["y"] + VECTORS[p["facing"]][1] * i,
            }
            for i in range(1, 4)
        ]
        for name, p in zip(ALIASES, ports)
    }
    # Reserve two empty continuation cells; the third is the first cell outside.
    points = [xy(e) for e in pipes] + [(p["x"], p["y"]) for path in access.values() for p in path[:2]]
    area = [
        [min(a for a, b in points) - 0.5, min(b for a, b in points) - 0.5],
        [max(a for a, b in points) + 0.5, max(b for a, b in points) + 0.5],
    ]
    return _plan(
        pipes,
        ports,
        inlet=inlet,
        fluid_groups={"petroleum-gas": pipes},
        output_access=[entity("pipe", p["x"], p["y"]) for path in access.values() for p in path],
        alias_access=access,
        reserved_area=area,
        anchor={"x": x, "y": y},
        layout_policy="shared-gas-two-outlet-v1",
    )


def _edges(nodes, pairs):
    by = {xy(e): e for e in nodes}
    edges = []
    seen = set()

    def add(a, b):
        identity = tuple(sorted((_key(a), _key(b))))
        if identity not in seen:
            seen.add(identity)
            edges.append({"a": a, "b": b})

    for p, e in by.items():
        for d, (dx, dy) in VECTORS.items():
            other = by.get((p[0] + dx, p[1] + dy))
            if (
                other
                and (e["name"] != "pipe-to-ground" or e["direction"] == d)
                and (other["name"] != "pipe-to-ground" or other["direction"] == (d + 8) % 16)
            ):
                add(e, other)
    for pair in pairs:
        add(by[tuple(pair["first"])], by[tuple(pair["last"])])
    return edges


class GasDistribution:
    def __init__(self, game, builder, catalog, perimeter, *, petroleum=None, chemical=None):
        self.game, self.builder, self.catalog, self.perimeter = game, builder, catalog, perimeter
        self.petroleum = petroleum if petroleum is not None else BasicPetroleum(game, builder, catalog, perimeter)
        self.chemical = chemical
        self.path = Path(game.cfg.runtime_dir) / "gas-distribution.json"
        self.sources_path = self.path.parent / "fluid-sources.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.last_proof_failure = None

    def _identity(self, obs, value):
        return (
            obs.get("ok", True)
            and isinstance(obs.get("world_id"), str)
            and bool(obs["world_id"])
            and value.get("world_id") == obs["world_id"]
            and value.get("catalog_fingerprint") == self.catalog.fingerprint
        )

    def _registry(self, obs):
        registry = json.loads(self.sources_path.read_text(encoding="utf-8")) if self.sources_path.exists() else {}
        if not self._identity(obs, registry):
            return {"ok": False, "reason": "gas_distribution_registry_identity_changed"}
        if not isinstance(registry.get("sources"), dict) or not isinstance(registry.get("handoffs", {}), dict):
            return {"ok": False, "reason": "gas_distribution_registry_shape_invalid"}
        source = registry.get("sources", {}).get("petroleum-gas", {})
        if (
            source.get("kind") != "fluid"
            or source.get("item") != "petroleum-gas"
            or source.get("name") != "pipe"
            or source.get("direction") != "output"
            or source.get("facing") != 4
            or source.get("fluidbox_index") != 1
            or source.get("world_id") != obs["world_id"]
            or type(source.get("unit_number")) is not int
            or source["unit_number"] <= 0
        ):
            return {"ok": False, "reason": "exact_eastbound_original_petroleum_output_required"}
        path = self.path.parent / "chemical-production.json"
        chemical = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else getattr(self.chemical, "state", {})
        )
        if chemical and not self._identity(obs, chemical):
            return {"ok": False, "reason": "gas_chemical_ownership_identity_changed"}
        for product, entry in chemical.get("products", {}).items():
            if entry.get("sources", {}).get("petroleum-gas", {}).get("unit_number") == source["unit_number"]:
                return {"ok": False, "reason": "original_gas_already_owned_by_chemical_plan", "product": product}
        for owner, rows in registry.get("handoffs", {}).items():
            if not isinstance(rows, dict) or any(not isinstance(r, dict) for r in rows.values()):
                return {"ok": False, "reason": "gas_distribution_registry_shape_invalid"}
            for row in rows.values():
                if row.get("source", {}).get("unit_number") == source["unit_number"]:
                    if owner != CONSUMER_ID:
                        return {"ok": False, "reason": "original_gas_edge_already_owned", "consumer": owner}
                    if not self.state:
                        return {"ok": False, "reason": "gas_handoff_exists_without_manifold_checkpoint"}
        if not self.state and any(name in registry.get("sources", {}) for name in ALIASES):
            return {"ok": False, "reason": "gas_alias_exists_without_manifold_checkpoint"}
        if self.state and source != self.state["original_source"]:
            return {"ok": False, "reason": "original_petroleum_output_changed"}
        return {"ok": True, "registry": registry, "source": source}

    def prepare(self, obs):
        if self.state:
            return (
                deepcopy(self.state)
                if self._identity(obs, self.state)
                else {"ok": False, "reason": "gas_distribution_world_or_catalog_changed"}
            )
        ready = self._registry(obs)
        if not ready.get("ok"):
            return ready
        if not self._identity(obs, self.petroleum.state) or self.petroleum.state.get("phase") != "verified":
            return {"ok": False, "reason": "normally_commissioned_basic_petroleum_required"}
        upstream = self.petroleum.proof(obs)
        if not upstream:
            return {
                "ok": False,
                "reason": "fresh_native_petroleum_proof_required",
                "evidence": self.petroleum.last_proof_failure,
            }
        source = ready["source"]
        board = json.loads((self.path.parent / "pcb-board.json").read_text(encoding="utf-8"))
        if not self._identity(obs, board):
            return {"ok": False, "reason": "gas_distribution_board_identity_changed"}
        blocks = [b for b in board["blocks"].values() if b.get("reserved_area")]
        apron = source_owned_apron(self.builder, source, blocks)
        if not apron.get("ok"):
            return apron
        first_clear = apron["path"][-1] if apron["path"] else source["position"]
        owner = blocks[apron["owner_index"] - 1] if apron["owner_index"] else self.petroleum.state["plan"]
        steel_path = self.path.parent / "steel-production.json"
        steel = json.loads(steel_path.read_text(encoding="utf-8")) if steel_path.exists() else {}
        steel_outputs = [p for p in steel.get("core", {}).get("ports", []) if p.get("direction") == "output"]
        south_y = steel_outputs[0]["position"]["y"] + 2 if steel_outputs else owner["reserved_area"][1][1] + 3.5
        north_y = owner["reserved_area"][0][1] - 3.5
        sx, sy = xy(source)
        candidates = [
            (first_clear["x"] + dx, y, side)
            for y, dx, side in (
                (sy, 0, -1),
                (sy, 4, -1),
                (north_y, 2, -1),
                (north_y, 6, -1),
                (south_y, 2, -1),
                (south_y, 6, -1),
                (sy, 0, 1),
                (south_y, 6, 1),
            )
        ]
        engine_path = self.path.parent / "engine-production.json"
        engine = json.loads(engine_path.read_text(encoding="utf-8")) if engine_path.exists() else {}
        if engine and not self._identity(obs, engine):
            return {"ok": False, "reason": "gas_future_engine_plan_identity_changed"}
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        future = [e for e in engine.get("plan", {}).get("entities", []) if (e["name"], *xy(e)) not in actual]
        engine_area = engine.get("core", {}).get("reserved_area")
        if engine_area:
            blocks.append({"reserved_area": engine_area})
            future += [
                entity("pcb-reserved", engine_area[0][0] + 0.5 + i, engine_area[0][1] + 0.5 + j)
                for i in range(int(engine_area[1][0] - engine_area[0][0]))
                for j in range(int(engine_area[1][1] - engine_area[0][1]))
            ]
        budget = PlanningBudget(self.game)
        failures = []
        snapshot = self._source_snapshot(budget.game, source)
        if not snapshot.get("ok"):
            return snapshot
        for x, y, side in candidates:
            if budget.expired():
                return budget.failure(candidate_failures=failures[-6:])
            core = gas_manifold_core(x, y, side)
            if any(overlap(core["reserved_area"], b["reserved_area"]) for b in blocks):
                continue
            if any(
                overlap([[p["x"] - 0.49, p["y"] - 0.49], [p["x"] + 0.49, p["y"] + 0.49]], b["reserved_area"])
                for path in core["alias_access"].values()
                for p in path
                for b in blocks
            ):
                continue
            land = survey_slots(budget.game, [[core["reserved_area"]]])
            if not land.get("ok") or 1 not in land.get("legal", []):
                continue
            route = plan_typed_fluid_connection(budget.game, self.builder, obs, source, core["inlet"], core, future)
            if not route.get("ok"):
                failures.append({"anchor": core["anchor"], "stage": "inlet_route", "reason": route.get("reason")})
                continue
            by = {xy(e): e for e in route["entities"]}
            landing = by.get(xy(core["inlet"]))
            own = [
                e
                for e in core["entities"]
                if not (landing and landing["name"] != "pipe" and xy(e) == xy(core["inlet"]))
            ]
            es = merge_entities(route["entities"], own)
            nodes = merge_entities([source], es)
            extent = pipeline_extent(snapshot["extent"], es, snapshot["limit"])
            if not extent.get("ok"):
                failures.append({"anchor": core["anchor"], "stage": "extent", "reason": extent.get("reason")})
                continue
            isolation = self._isolation(budget.game, source, es, route["maximum_underground_span"])
            if not isolation.get("ok"):
                failures.append({"anchor": core["anchor"], "stage": "isolation", "reason": isolation.get("reason")})
                continue
            check = preflight(budget.game, self.builder, obs, es + core["output_access"])
            if not check.get("ok"):
                failures.append({"anchor": core["anchor"], "stage": "whole_preflight", "reason": check.get("reason")})
                continue
            scope = "gas-distribution"
            defense = self.perimeter.prepare_coverage(obs, scope, [core["reserved_area"]])
            if budget.expired():
                return budget.failure(candidate_failures=failures[-6:])
            if not defense.get("ok"):
                failures.append({"anchor": core["anchor"], "stage": "defense", "reason": defense.get("reason")})
                continue
            crossed = self._crossed_assets(obs, route)
            if not crossed.get("ok"):
                failures.append(crossed)
                continue
            capacity = self._capacity(upstream)
            if capacity is None:
                return {"ok": False, "reason": "gas_upstream_verified_refinery_count_unavailable"}
            plan = _plan(
                es,
                core["ports"],
                reserved_area=core["reserved_area"],
                defense_scope_id=scope,
                clear_obstacles=check["clear_obstacles"],
                clearance_world_id=obs["world_id"],
                layout_policy=core["layout_policy"],
            )
            return {
                "ok": True,
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "phase": "build",
                "core": core,
                "plan": plan,
                "original_source": deepcopy(source),
                "first_neighbor": route["first_neighbor"],
                "edges": _edges(nodes, route["underground_pairs"]),
                "nodes": nodes,
                "bindings": {},
                "crossed_assets": crossed["assets"],
                "defense_scope_id": scope,
                "defense_preview": defense,
                "route_metrics": route["route_metrics"],
                "pipeline_extent": extent,
                "shared_nominal_gas_per_minute": capacity,
                "prepared_tick": obs.get("tick"),
            }
        return {
            "ok": False,
            "reason": "no_isolated_two_outlet_gas_manifold",
            "evidence": {"candidate_failures": failures[-8:]},
        }

    def _capacity(self, upstream):
        rows = upstream.get("native", {}).get("refineries", [])
        count = len(rows)
        units = [row.get("unit_number") for row in rows]
        if any(type(unit) is not int or unit < 1 for unit in units) or len(set(units)) != count:
            return None
        if count < 1 or count != len(self.petroleum.state["core"]["machines"]):
            return None
        return basic_petroleum_rates(self.catalog)["petroleum_per_minute"] * count

    @staticmethod
    def _source_snapshot(game, source):
        return game.query(
            """local q=helpers.json_to_table("""
            + _payload(source)
            + """);local e=target(q.position,"pipe")
if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return{ok=false,reason="original_gas_source_identity_changed"}end
local fluids=e.get_fluid_contents();for name in pairs(fluids)do if name~="petroleum-gas"then return{ok=false,reason="original_gas_source_contaminated"}end end
if(fluids["petroleum-gas"]or 0)<=0 or not e.has_fluid_segment(1)then return{ok=false,reason="normally_produced_gas_required"}end
return{ok=true,extent=e.get_fluid_segment_extent_bounding_box(1),limit=prototypes.utility_constants.default_pipeline_extent}"""
        )

    def _crossed_assets(self, obs, route):
        buried = set()
        for pair in route["underground_pairs"]:
            a = pair["first"]
            dx, dy = VECTORS[pair["direction"]]
            buried.update((a[0] + dx * i, a[1] + dy * i) for i in range(1, pair["span"]))
        allowed = {
            "transport-belt",
            "underground-belt",
            "small-electric-pole",
            "inserter",
            "fast-inserter",
            "long-handed-inserter",
        }
        assets = []
        for e in obs["entities"]:
            if e["name"] == "character" or not (self.builder._occupied_by_plan([e]) & buried):
                continue
            if e["name"] not in allowed or type(e.get("unit_number")) is not int:
                return {"ok": False, "reason": "gas_manifold_cannot_bury_under_foreign_producer"}
            assets.append({k: deepcopy(e[k]) for k in ("name", "position", "unit_number", "direction")})
        return {"ok": True, "assets": assets}

    @staticmethod
    def _isolation(game, source, es, span):
        return game.query(
            """local args=helpers.json_to_table("""
            + _payload({"source": source, "entities": es, "span": span})
            + """)
for _,q in ipairs(args.entities)do local p=q.position
 if target(p,"pipe")or target(p,"pipe-to-ground")then return{ok=false,reason="gas_manifold_requires_empty_paid_footprint"}end
 for _,e in pairs(s.find_entities_filtered{area={{p.x-1.49,p.y-1.49},{p.x+1.49,p.y+1.49}}})do
  if e.unit_number~=args.source.unit_number then for _ in pairs(e.prototype.fluidbox_prototypes or{})do return{ok=false,reason="gas_manifold_near_foreign_fluid_network"}end end
 end
 if q.name=="pipe-to-ground"then for _,e in pairs(s.find_entities_filtered{area={{p.x-args.span,p.y-args.span},{p.x+args.span,p.y+args.span}},name="pipe-to-ground"})do
  if(p.x==e.position.x or p.y==e.position.y)and math.abs(p.x-e.position.x)+math.abs(p.y-e.position.y)<=args.span then return{ok=false,reason="gas_manifold_existing_buried_peer_conflict"}end end end
end;return{ok=true}"""
        )

    def _save(self):
        _atomic_json(self.path, self.state)

    def _bind(self, obs):
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        changed = False
        for spec in self.state["plan"]["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            old = self.state["bindings"].get(_key(spec))
            if old and (not live or live.get("unit_number") != old):
                return {"status": "blocked", "reason": "gas_manifold_asset_missing_or_replaced", "entity": spec}
            if live:
                if type(live.get("unit_number")) is not int or (
                    spec["name"] == "pipe-to-ground" and live.get("direction") != spec["direction"]
                ):
                    return {"status": "blocked", "reason": "gas_manifold_asset_identity_or_direction_changed"}
                if old is None:
                    self.state["bindings"][_key(spec)] = live["unit_number"]
                    changed = True
        if changed:
            self._save()
        return None

    def _protect(self, obs):
        return (
            self.game.query(
                """local specs=helpers.json_to_table("""
                + _payload(self.state["crossed_assets"])
                + """)
for _,q in ipairs(specs)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.direction~=q.direction or e.position.x~=q.position.x or e.position.y~=q.position.y then return{ok=false,reason="gas_manifold_crossed_asset_changed",position=q.position}end
end;return{ok=true}"""
            )
            if self.state["crossed_assets"]
            else {"ok": True}
        )

    def _reserve(self, obs):
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        if not self._identity(obs, board):
            return {"status": "blocked", "reason": "gas_manifold_board_identity_changed"}
        old = board["blocks"].get("gas_distribution")
        plan = self.state["plan"]
        if old and old != plan:
            return {"status": "blocked", "reason": "gas_manifold_reservation_changed"}
        if any(
            k != "gas_distribution" and overlap(plan["reserved_area"], b["reserved_area"])
            for k, b in board["blocks"].items()
            if b.get("reserved_area")
        ):
            return {"status": "blocked", "reason": "gas_manifold_reservation_conflict"}
        if not old:
            board["blocks"]["gas_distribution"] = plan
            _atomic_json(path, board)
        return None

    def step(self, obs):
        if not self.state:
            ready = self.prepare(obs)
            if not ready.get("ok"):
                evidence = ready.get("evidence") or {
                    k: deepcopy(v) for k, v in ready.items() if k not in ("ok", "status")
                }
                return {"status": "blocked", **ready, "evidence": evidence}
            self.state = ready
            self._save()
        if not self._identity(obs, self.state):
            return {"status": "blocked", "reason": "gas_distribution_world_or_catalog_changed"}
        ready = self._registry(obs)
        if not ready.get("ok"):
            return {"status": "blocked", **ready}
        snapshot = self._source_snapshot(self.game, self.state["original_source"])
        if not snapshot.get("ok"):
            return {"status": "blocked", "reason": "gas_manifold_original_source_guard_failed", "evidence": snapshot}
        result = self._bind(obs)
        if result:
            return result
        result = self._reserve(obs)
        if result:
            return result
        result = self.perimeter.ensure_coverage(
            obs, self.state["defense_scope_id"], [self.state["core"]["reserved_area"]]
        )
        if result is not None:
            return result
        protection = self._protect(obs)
        if not protection.get("ok"):
            return {"status": "blocked", "reason": "gas_manifold_crossed_asset_guard_failed", "evidence": protection}
        first = {k: v for k, v in self.state["first_neighbor"].items() if k != "unit_number"}
        declared = _handoff(
            self.game, self.catalog, obs, CONSUMER_ID, "petroleum-gas", self.state["original_source"], first
        )
        if not declared.get("ok"):
            return {"status": "blocked", "reason": "gas_manifold_handoff_not_authorized", "evidence": declared}
        unit = self.state["bindings"].get(_key(first))
        if unit:
            bound = _handoff(
                self.game,
                self.catalog,
                obs,
                CONSUMER_ID,
                "petroleum-gas",
                self.state["original_source"],
                dict(first, unit_number=unit),
                bind=True,
            )
            if not bound.get("ok"):
                return {"status": "blocked", "reason": "gas_manifold_handoff_not_bound", "evidence": bound}
        if not self.state.get("construction_authorized"):
            check = preflight(
                self.game, self.builder, obs, self.state["plan"]["entities"] + self.state["core"]["output_access"]
            )
            if not check.get("ok"):
                return {"status": "blocked", "reason": "gas_manifold_site_changed_after_defense", "evidence": check}
            self.state["construction_authorized"] = True
            self._save()
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        evidence = self.proof(obs)
        if not evidence:
            return {
                "status": "waiting",
                "reason": "waiting for both native shared-gas outlets and bound reciprocal producer handoff",
                "evidence": self.last_proof_failure,
            }
        registry = json.loads(self.sources_path.read_text(encoding="utf-8"))
        if (
            not self._identity(obs, registry)
            or registry["sources"].get("petroleum-gas") != self.state["original_source"]
        ):
            return {"status": "blocked", "reason": "gas_distribution_registry_identity_changed"}
        for name, source in evidence["sources"].items():
            old = registry["sources"].get(name)
            if old and old != source:
                return {"status": "blocked", "reason": "published_gas_alias_identity_changed", "alias": name}
            registry["sources"][name] = source
        _atomic_json(self.sources_path, registry)
        self.state.update(
            phase="verified", proof=evidence, shared_nominal_gas_per_minute=evidence["shared_nominal_gas_per_minute"]
        )
        self._save()
        return {
            "status": "succeeded",
            "reason": "one normally supplied gas manifold publishes two fixed independent consumer edges",
            "evidence": evidence,
        }

    def proof(self, obs):
        self.last_proof_failure = None
        if not self.state or not self._identity(obs, self.state):
            return None
        ready = self._registry(obs)
        if not ready.get("ok"):
            self.last_proof_failure = ready
            return None
        first = dict(
            self.state["first_neighbor"], unit_number=self.state["bindings"].get(_key(self.state["first_neighbor"]))
        )
        record = ready["registry"].get("handoffs", {}).get(CONSUMER_ID, {}).get("petroleum-gas", {})
        if (
            record.get("phase") != "bound"
            or record.get("source") != self.state["original_source"]
            or record.get("first_neighbor") != first
            or not self._identity(obs, record)
        ):
            self.last_proof_failure = {"reason": "gas_manifold_original_handoff_not_bound"}
            return None
        upstream = self.petroleum.proof(obs)
        if not upstream:
            self.last_proof_failure = {"reason": "gas_manifold_upstream_production_proof_missing"}
            return None
        capacity = self._capacity(upstream)
        if capacity is None:
            self.last_proof_failure = {"reason": "gas_upstream_verified_refinery_count_unavailable"}
            return None
        defense = self.perimeter.coverage_proof(obs, self.state["defense_scope_id"])
        if not defense:
            self.last_proof_failure = {"reason": "gas_manifold_defense_coverage_missing"}
            return None
        protection = self._protect(obs)
        if not protection.get("ok"):
            self.last_proof_failure = protection
            return None
        bindings = {
            **self.state["bindings"],
            _key(self.state["original_source"]): self.state["original_source"]["unit_number"],
        }
        if any(_key(e) not in bindings for e in self.state["nodes"]):
            return None
        aliases = {
            name: dict(
                spec,
                world_id=obs["world_id"],
                unit_number=bindings[_key(spec)],
                defense_scope_id=self.state["defense_scope_id"],
            )
            for name, spec in zip(ALIASES, self.state["core"]["ports"])
        }
        downstream = []
        try:
            for source in aliases.values():
                for row in bound_handoffs(ready["registry"], obs, self.catalog, source):
                    downstream.append({"source": source, "first_neighbor": row["first_neighbor"]})
        except (ValueError, KeyError, TypeError) as exc:
            self.last_proof_failure = {"reason": "gas_alias_handoff_registry_changed", "detail": str(exc)}
            return None
        result = self.game.query(
            GAS_PROOF_LUA.replace(
                "PAYLOAD",
                _payload(
                    {
                        "source": self.state["original_source"],
                        "nodes": [dict(e, unit_number=bindings[_key(e)]) for e in self.state["nodes"]],
                        "edges": self.state["edges"],
                        "bindings": bindings,
                        "aliases": aliases,
                        "downstream": downstream,
                    }
                ),
            )
        )
        if not result.get("ok"):
            self.last_proof_failure = result
            return None
        return {
            "tick": result["tick"],
            "sources": aliases,
            "native": result,
            "upstream": upstream,
            "shared_nominal_gas_per_minute": capacity,
            "defense_coverage": defense,
            "capacity_scope": "one verified upstream refinery supply shared by both aliases; no capacity multiplication or measured rate claim",
        }


GAS_PROOF_LUA = r"""
local args=helpers.json_to_table(PAYLOAD);local live={};local expected={}
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
local function allow(a,b)expected[a.unit_number]=expected[a.unit_number]or{};expected[a.unit_number][b.unit_number]=true end
local function edge(a,b)
 local forward=false;local reverse=false;for _,p in pairs(a.get_fluid_box_neighbours(1)or{})do if p.entity==b and p.index==1 then forward=true end end
 for _,p in pairs(b.get_fluid_box_neighbours(1)or{})do if p.entity==a and p.index==1 then reverse=true end end
 return forward and reverse end
for _,q in ipairs(args.nodes)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y
 or(q.name=="pipe-to-ground"and e.direction~=q.direction)then return{ok=false,reason="gas_manifold_native_asset_identity_changed"}end
 for name in pairs(e.get_fluid_contents())do if name~="petroleum-gas"then return{ok=false,reason="gas_manifold_native_contamination"}end end
 if not e.has_fluid_segment(1)then return{ok=false,reason="gas_manifold_native_pipeline_missing"}end
 local b=e.get_fluid_segment_extent_bounding_box(1);local limit=prototypes.utility_constants.default_pipeline_extent
 if b.right_bottom.x-b.left_top.x>limit or b.right_bottom.y-b.left_top.y>limit then return{ok=false,reason="gas_manifold_native_extent_exceeded"}end
 live[key(q)]=e
end
for _,q in ipairs(args.edges)do local a=live[key(q.a)];local b=live[key(q.b)]
 if not edge(a,b)then return{ok=false,reason="gas_manifold_native_reciprocal_edge_missing"}end;allow(a,b);allow(b,a)end
for _,q in ipairs(args.downstream)do local a=live[key(q.source)];local spec=q.first_neighbor;local b=target(spec.position,spec.name)
 if not b or b.force~=f or b.surface~=s or b.unit_number~=spec.unit_number or b.position.x~=spec.position.x or b.position.y~=spec.position.y
 or(spec.name=="pipe-to-ground"and b.direction~=spec.direction)then return{ok=false,reason="gas_alias_consumer_identity_changed"}end
 for name in pairs(b.get_fluid_contents())do if name~="petroleum-gas"then return{ok=false,reason="gas_alias_consumer_contaminated"}end end
 if not edge(a,b)then return{ok=false,reason="gas_alias_consumer_edge_missing"}end;allow(a,b)
end
for _,q in ipairs(args.nodes)do local e=live[key(q)]
 if e.unit_number~=args.source.unit_number then for _,p in pairs(e.get_fluid_box_neighbours(1)or{})do
  if p.index~=1 or not(expected[e.unit_number]or{})[p.entity.unit_number]then return{ok=false,reason="gas_manifold_undeclared_peer"}end
 end end
end
local amounts={};for name,q in pairs(args.aliases)do local e=live[key(q)];local gas=e.get_fluid_contents()["petroleum-gas"]or 0
 if gas<=0 then return{ok=false,reason="gas_manifold_alias_empty",alias=name}end;amounts[name]=gas end
return{ok=true,tick=game.tick,alias_amounts=amounts,reciprocal_edges=#args.edges}
"""
