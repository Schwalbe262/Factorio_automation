"""Paid shared coal-header tap; preserve brick production and source ports."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .brick_production import BrickProduction
from .bus_factory import identity, trace_corridor
from .industrial import PlanningBudget, _plan_revision, _source_identity, industrial_site_anchors
from .manufacturing import connect_power_groups
from .planning import overlap, preflight, survey_slots
from .smelting import merge_entities, port
from .steel_production import audit_steel_core, steel_core
from .transport import plan_connection, underground_proof, xy


MAX_HEADER_CANDIDATES = 6
MAX_STEEL_ATTEMPTS = 8


def coal_header_candidates(brick):
    """Only the existing consumer route owns potential shared-header assets."""
    source = next(p for p in brick["sources"] if p["item"] == "coal")
    destination = next(p for p in brick["core"]["ports"] if p["item"] == "coal")
    traced = trace_corridor(brick["plan"], source, destination)
    if not traced.get("ok"):
        raise ValueError(traced["reason"])
    rows = traced["entities"]
    result = []
    for index in range(1, len(rows) - 1):
        before, current, after = rows[index - 1 : index + 2]
        if any(e["name"] != "transport-belt" or e["direction"] != 4 for e in (before, current, after)):
            continue
        x, y = xy(current)
        if xy(before) != (x - 1, y) or xy(after) != (x + 1, y):
            continue
        splitter = dict(
            entity("splitter", x, y - 0.5, 4),
            _width=2,
            _height=1,
            splitter_input_priority="right",
            splitter_output_priority="none",
        )
        outlet = entity("transport-belt", x + 1, y - 1, 0)
        result.append(
            {
                "old": current,
                "splitter": splitter,
                "outlet": outlet,
                "through": after,
                "empty_continuation": entity("transport-belt", x + 1, y - 2, 0),
            }
        )
    return list(reversed(result))


def replace_header(plan, old, added):
    keys = {identity(e) for e in plan["entities"]}
    new_keys = {identity(e) for e in added}
    if new_keys <= keys and identity(old) not in keys:
        return deepcopy(plan)
    if identity(old) not in keys:
        raise ValueError("shared coal old header asset missing from authority")
    entities = merge_entities([e for e in plan["entities"] if identity(e) != identity(old)], added)
    extra = {
        key: value for key, value in plan.items() if key not in ("ok", "reason", "entities", "ports", "required_items")
    }
    return _plan(entities, plan["ports"], **extra)


class SharedCoalTap:
    def __init__(self, game, builder, catalog, brick, *, industrial):
        self.game, self.builder, self.catalog, self.brick, self.industrial = game, builder, catalog, brick, industrial
        self.path = Path(game.cfg.runtime_dir) / "shared-coal-tap.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _save(self):
        _atomic_json(self.path, self.state)

    def _documents(self):
        root = self.path.parent
        return {
            name: json.loads(path.read_text(encoding="utf-8"))
            for name, path in {
                "brick": Path(self.brick.path),
                "board": root / "pcb-board.json",
                "sources": root / "material-sources.json",
                "industrial": Path(self.industrial.path),
            }.items()
        }

    @staticmethod
    def _exact(actual, spec):
        live = actual.get((spec["name"], *xy(spec)))
        return (
            live
            if live and live.get("unit_number") == spec["unit_number"] and identity(live) == identity(spec)
            else None
        )

    def _prospective_steel(self, obs, core, iron, coal, added, old_unit, budget):
        adapter = SimpleNamespace(
            _occupied_by_plan=self.builder._occupied_by_plan,
            can_place=lambda es: preflight(budget.game, self.builder, obs, es, ignored_units=(old_unit,)),
        )
        virtual = dict(obs, entities=[e for e in obs["entities"] if e.get("unit_number") != old_unit])
        entities = merge_entities(core["entities"], added)
        routes = []
        for source, destination in zip((iron, coal), core["ports"][:2]):
            route = plan_connection(
                budget.game, adapter, virtual, source, destination, entities, ignored_units=(old_unit,)
            )
            if not route.get("ok"):
                return {
                    "ok": False,
                    "reason": route.get("reason"),
                    "stage": "prospective_steel_feed",
                    "item": source["item"],
                    "source": source["position"],
                    "destination": destination["position"],
                    "visited": route.get("visited"),
                }
            entities = merge_entities(entities, route["entities"])
            routes.append(route)
        power = connect_power_groups(budget.game, adapter, virtual, [core], entities)
        if not power.get("ok"):
            return dict(power, stage="prospective_steel_power")
        entities = merge_entities(entities, power["entities"])
        return {
            "ok": True,
            "entities": entities,
            "core": core,
            "route_metrics": [route["route_metrics"] for route in routes],
            "scope": "prospective normal steel layout; new coal outlet identity must be bound after paid tap commissioning",
        }

    def prepare(self, obs):
        try:
            docs = self._documents()
            if any(
                doc.get("world_id") != obs["world_id"] or doc.get("catalog_fingerprint") != self.catalog.fingerprint
                for doc in docs.values()
            ):
                return {"ok": False, "reason": "shared_coal_authority_identity_changed"}
            if self.industrial.steel.state:
                return {"ok": False, "reason": "shared_coal_adoption_requires_unbuilt_steel"}
            brick = docs["brick"]
            if brick.get("phase") != "verified":
                return {"ok": False, "reason": "shared_coal_requires_commissioned_brick_production"}
            if _plan_revision(docs["board"]["blocks"]["brick_production"]) != _plan_revision(brick["plan"]):
                return {"ok": False, "reason": "shared_coal_brick_authorities_disagree"}
            registry = docs["sources"]["sources"]
            source = registry["brick-coal"]
            unused = registry["coal"]
            if (
                source["item"] != "coal"
                or unused["item"] != "coal"
                or source["material_supply_id"] != unused["material_supply_id"]
                or any(_source_identity(p) != _source_identity(source) for p in brick["sources"] if p["item"] == "coal")
            ):
                return {"ok": False, "reason": "shared_coal_registered_source_contract_changed"}
            if "steel-coal" in registry:
                return {"ok": False, "reason": "shared_coal_alias_already_exists_without_receipt"}
            if not underground_proof(obs, brick["plan"]):
                return {"ok": False, "reason": "shared_coal_existing_brick_underground_unverified"}
            actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
            bound = {}
            for spec in brick["plan"]["entities"]:
                live = actual.get((spec["name"], *xy(spec)))
                if not live or identity(live) != identity(spec) or type(live.get("unit_number")) is not int:
                    return {
                        "ok": False,
                        "reason": "shared_coal_existing_brick_asset_changed",
                        "position": spec["position"],
                    }
                bound[identity(spec)] = {key: live[key] for key in ("name", "position", "direction", "unit_number")}
                if "belt_to_ground_type" in live:
                    bound[identity(spec)]["belt_to_ground_type"] = live["belt_to_ground_type"]
            for source_port in (source, unused):
                live = actual.get(("transport-belt", *xy(source_port)))
                if (
                    not live
                    or live["unit_number"] != source_port["unit_number"]
                    or live["direction"] != source_port["facing"]
                ):
                    return {"ok": False, "reason": "shared_coal_source_port_identity_changed"}
            supply = self.industrial._supply(obs)
            iron = supply["iron"]
            bus, board = self.industrial._bus(obs)
            budget = PlanningBudget(self.game)
            headers = coal_header_candidates(brick)[:MAX_HEADER_CANDIDATES]
            failures = []
            attempts = 0
            for header in headers:
                if budget.expired():
                    break
                old = bound[identity(header["old"])]
                added = [header["splitter"], header["outlet"]]
                check = preflight(
                    budget.game,
                    self.builder,
                    obs,
                    added + [header["empty_continuation"]],
                    ignored_units=(old["unit_number"],),
                )
                if not check.get("ok"):
                    failures.append(
                        {
                            "stage": "header_and_empty_exit",
                            "position": header["old"]["position"],
                            "reason": check.get("reason"),
                        }
                    )
                    continue
                coal = port("coal", *xy(header["outlet"]), header["outlet"]["direction"], "output")
                cores = [
                    steel_core(self.catalog, *anchor) for anchor in industrial_site_anchors(bus, board, "steel-plate")
                ]
                cores = [
                    core
                    for core in cores
                    if not any(
                        overlap(core["reserved_area"], b["reserved_area"])
                        for b in board["blocks"].values()
                        if "reserved_area" in b
                    )
                ]
                cores.sort(key=lambda core: abs(core["ports"][1]["position"]["x"] - coal["position"]["x"]))
                legal = survey_slots(budget.game, [[core["reserved_area"]] for core in cores])
                if not legal.get("ok"):
                    failures.append({"stage": "steel_site_survey", "reason": legal.get("reason")})
                    break
                final = replace_header(brick["plan"], header["old"], added)
                for index in legal.get("legal", []):
                    if budget.expired() or attempts >= MAX_STEEL_ATTEMPTS:
                        break
                    core = cores[index - 1]
                    attempts += 1
                    audit = audit_steel_core(core, self.catalog)
                    if not audit["ok"]:
                        return {"ok": False, "reason": "shared_coal_prospective_steel_audit_failed", "audit": audit}
                    preview = self._prospective_steel(obs, core, iron, coal, added, old["unit_number"], budget)
                    if not preview.get("ok"):
                        failures.append(preview)
                        continue
                    whole = preflight(
                        budget.game,
                        self.builder,
                        obs,
                        merge_entities(final["entities"], preview["entities"]),
                        ignored_units=(old["unit_number"],),
                    )
                    if not whole.get("ok"):
                        failures.append({"stage": "whole_brick_tap_and_steel", "reason": whole.get("reason")})
                        continue
                    clear = preflight(budget.game, self.builder, obs, [header["outlet"]])
                    if not clear.get("ok"):
                        failures.append({"stage": "prebuild_outlet", "reason": clear.get("reason")})
                        continue
                    obstacles = {
                        (e["name"], *xy(e)): e for row in (check, clear) for e in row.get("clear_obstacles", [])
                    }
                    return {
                        "ok": True,
                        "world_id": obs["world_id"],
                        "catalog_fingerprint": self.catalog.fingerprint,
                        "phase": "stock",
                        "old": old,
                        "old_spec": header["old"],
                        "new_entities": added,
                        "outlet": header["outlet"],
                        "empty_continuation": header["empty_continuation"],
                        "through": bound[identity(header["through"])],
                        "splitter": header["splitter"],
                        "protected": [value for key, value in bound.items() if key != identity(header["old"])],
                        "source_ports": [deepcopy(unused), deepcopy(source)],
                        "new_units": [],
                        "old_brick_revision": _plan_revision(brick["plan"]),
                        "new_brick_revision": _plan_revision(final),
                        "brick_snapshot": brick,
                        "final_brick_plan": final,
                        "core_ports": brick["core"]["ports"],
                        "coal_bank_id": source["material_supply_id"],
                        "shared_capacity_per_minute": source["nominal_shared_rate_per_minute"],
                        "steel_preview": preview,
                        "required_stock": {"splitter": 1, "transport-belt": 1},
                        "prebuild": _plan([header["outlet"]], []),
                        "clearance": _plan(
                            [], [], clear_obstacles=list(obstacles.values()), clearance_world_id=obs["world_id"]
                        ),
                        "prepared_tick": obs["tick"],
                        "unused_coal_outlet_reason": "fixed original outlet retained; forward pole/resource obstruction, no source-port relocation or capacity increase",
                    }
                if attempts >= MAX_STEEL_ATTEMPTS:
                    break
            reason = (
                "shared_coal_planning_budget_exhausted"
                if budget.expired() or attempts >= MAX_STEEL_ATTEMPTS
                else "no_shared_coal_header_with_complete_steel_layout"
            )
            return budget.failure(
                reason,
                evidence={
                    "steel_attempts": attempts,
                    "steel_attempt_budget": MAX_STEEL_ATTEMPTS,
                    "header_candidate_budget": MAX_HEADER_CANDIDATES,
                    "failures": failures[-6:],
                },
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "reason": "shared_coal_saved_state_incompatible", "evidence": {"detail": str(exc)}}

    def _check(self, obs):
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        if any(not self._exact(actual, e) for e in self.state["protected"]):
            return {"status": "blocked", "reason": "shared_coal_preserved_brick_unit_changed"}
        for source in self.state["source_ports"]:
            live = actual.get(("transport-belt", *xy(source)))
            if not live or live["unit_number"] != source["unit_number"] or live["direction"] != source["facing"]:
                return {"status": "blocked", "reason": "shared_coal_fixed_source_port_changed"}
        known = {identity(e): e for e in self.state["new_units"]}
        for spec in self.state["new_entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            prior = known.get(identity(spec))
            if prior and not self._exact(actual, prior):
                return {"status": "blocked", "reason": "shared_coal_new_asset_unit_changed"}
            if live and identity(live) == identity(spec) and not prior:
                self.state["new_units"].append(
                    {key: live[key] for key in ("name", "position", "direction", "unit_number")}
                )
        return None

    def _stock(self, obs, counts):
        payload = json.dumps(json.dumps(list(counts), separators=(",", ":")))
        stock = self.game.query(
            "local names=helpers.json_to_table("
            + payload
            + """);local result={};local inv=a.get_main_inventory()
for _,name in ipairs(names) do result[name]=inv.get_item_count{name=name,quality="normal"} end
return {stock=result}"""
        ).get("stock", {})
        for item, count in counts.items():
            if stock.get(item, 0) < count:
                normal = dict(obs, inventory=dict(obs.get("inventory", {})))
                normal["inventory"][item] = stock.get(item, 0)
                return self.builder.bootstrap.ensure_item(normal, item, count)
        return None

    def _health(self, obs):
        if self._check(obs):
            return None
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        wanted = self.state["splitter"]
        splitter = actual.get(("splitter", *xy(wanted)))
        outlet = actual.get(("transport-belt", *xy(self.state["outlet"])))
        if (
            not splitter
            or not outlet
            or splitter.get("splitter_input_priority") != "right"
            or splitter.get("splitter_output_priority") != "none"
        ):
            return None
        rows = [dict(self.state["outlet"], unit_number=outlet["unit_number"]), self.state["through"]]
        payload = json.dumps(
            json.dumps(
                {"splitter": dict(wanted, unit_number=splitter["unit_number"]), "rows": rows}, separators=(",", ":")
            )
        )
        native = self.game.query(
            """local args=helpers.json_to_table("""
            + payload
            + """);local spec=args.splitter;local splitter=target(spec.position,"splitter")
if not splitter or splitter.unit_number~=spec.unit_number or splitter.force~=f or splitter.surface~=s or splitter.direction~=4
 or splitter.splitter_input_priority~="right" or splitter.splitter_output_priority~="none" then return {ok=false} end
local counts={}
for index,row in ipairs(args.rows) do local e=target(row.position,"transport-belt")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=row.unit_number or e.direction~=row.direction then return {ok=false} end
 local n=0;for lane=1,2 do for _,v in pairs(e.get_transport_line(lane).get_contents()) do
  if v.name~="coal" then return {ok=false,reason="shared_coal_contaminated"} end;n=n+v.count
 end end;counts[index]=n
end
return {ok=true,tick=game.tick,steel_coal=counts[1],brick_coal=counts[2]}"""
        )
        if not native.get("ok") or native.get("steel_coal", 0) <= 0 or native.get("brick_coal", 0) <= 0:
            return None
        preview = BrickProduction(self.game, self.builder, self.catalog)
        preview.state = dict(deepcopy(self.state["brick_snapshot"]), plan=deepcopy(self.state["final_brick_plan"]))
        brick = preview.proof(obs)
        if not brick:
            return None
        source = dict(
            port("coal", *xy(outlet), outlet["direction"], "output"),
            name="transport-belt",
            unit_number=outlet["unit_number"],
            world_id=obs["world_id"],
            material_supply_id=self.state["coal_bank_id"],
            nominal_shared_rate_per_minute=self.state["shared_capacity_per_minute"],
            capacity_scope="shared existing coal bank total; steel and brick outlets do not multiply production",
            shared_header={
                "splitter": dict(wanted, unit_number=splitter["unit_number"]),
                "brick_through_unit": self.state["through"]["unit_number"],
            },
        )
        return {
            "tick": obs["tick"],
            "source": source,
            "native_shared_outputs": native,
            "brick_production": brick,
            "scope": "native shared normal coal and unchanged brick production health; no fresh-counter or sustained-rate claim",
        }

    def _publish(self, obs, evidence):
        docs = self._documents()
        s = self.state
        if any(
            doc.get("world_id") != s["world_id"] or doc.get("catalog_fingerprint") != s["catalog_fingerprint"]
            for doc in docs.values()
        ):
            return {"status": "blocked", "reason": "shared_coal_publication_world_changed"}
        if self.industrial.steel.state:
            return {"status": "blocked", "reason": "shared_coal_cannot_retarget_built_steel"}
        if docs["brick"]["core"]["ports"] != s["core_ports"]:
            return {"status": "blocked", "reason": "shared_coal_brick_core_ports_changed"}
        for plan in (docs["brick"]["plan"], docs["board"]["blocks"]["brick_production"]):
            if _plan_revision(plan) not in (s["old_brick_revision"], s["new_brick_revision"]):
                return {"status": "blocked", "reason": "shared_coal_brick_plan_revision_changed"}
        for key, old in zip(("coal", "brick-coal"), s["source_ports"]):
            if _source_identity(docs["sources"]["sources"][key]) != _source_identity(old):
                return {"status": "blocked", "reason": "shared_coal_source_registry_changed"}
        published = docs["sources"]["sources"].get("steel-coal")
        if published and _source_identity(published) != _source_identity(evidence["source"]):
            return {"status": "blocked", "reason": "shared_coal_alias_identity_changed"}
        author = docs["industrial"].get("producer_authorizations", {}).get("brick")
        if author and author["plan_revision"] not in (s["old_brick_revision"], s["new_brick_revision"]):
            return {"status": "blocked", "reason": "shared_coal_brick_authorization_changed"}
        docs["brick"]["plan"] = deepcopy(s["final_brick_plan"])
        docs["board"]["blocks"]["brick_production"] = deepcopy(s["final_brick_plan"])
        docs["sources"]["sources"]["steel-coal"] = deepcopy(evidence["source"])
        root = self.path.parent
        _atomic_json(self.brick.path, docs["brick"])
        _atomic_json(root / "pcb-board.json", docs["board"])
        _atomic_json(root / "material-sources.json", docs["sources"])
        self.brick.state = docs["brick"]
        self.industrial.state = docs["industrial"]
        if author:
            self.industrial.state["producer_authorizations"]["brick"]["plan_revision"] = s["new_brick_revision"]
            self.industrial.state["producer_authorizations"]["brick"]["authorized_by"] = "verified_shared_coal_tap"
        result = self.industrial.adopt_shared_coal_source(obs, evidence["source"], evidence)
        if result is not None:
            return result
        s["published"] = True
        self._save()
        return None

    def _planned(self, obs):
        """The site-planned steel block on a planned world: it routes its own coal outlet (planned_furnaces)."""
        planned = getattr(self.industrial, "planned", None)
        return planned("steel", obs) if planned is not None else None

    def step(self, obs):
        planned = self._planned(obs)
        if planned is not None:
            return planned.coal_supply_step(obs)
        if not self.state:
            ready = self.prepare(obs)
            if not ready.get("ok"):
                return {"status": "blocked", **ready}
            self.state = ready
            self._save()
        s = self.state
        if s["world_id"] != obs["world_id"] or s["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "shared_coal_world_or_catalog_changed"}
        issue = self._check(obs)
        if issue:
            return issue
        self._save()
        if s["phase"] == "stock":
            action = self._stock(obs, s["required_stock"])
            if action:
                return action
            result = self.builder.ensure_plan(obs, s["clearance"])
            if result.get("status") != "succeeded":
                return result
            s["phase"] = "prebuild"
            self._save()
        if s["phase"] == "prebuild":
            old_actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
            if not self._exact(old_actual, s["old"]):
                return {"status": "blocked", "reason": "shared_coal_old_corner_changed_before_prebuild"}
            result = self.builder.ensure_plan(obs, s["prebuild"])
            if result.get("status") != "succeeded":
                return result
            s["phase"] = "cutover"
            self._save()
        if s["phase"] == "cutover":
            action = self._stock(obs, {"splitter": 1})
            if action:
                return action
            check = preflight(
                self.game,
                self.builder,
                obs,
                s["new_entities"] + [s["empty_continuation"]],
                ignored_units=(s["old"]["unit_number"],),
            )
            if not check.get("ok"):
                return {"status": "blocked", **check}
            old = next((e for e in obs["entities"] if e["name"] == "transport-belt" and xy(e) == xy(s["old"])), None)
            if old:
                if old["unit_number"] != s["old"]["unit_number"] or identity(old) != identity(s["old"]):
                    return {"status": "blocked", "reason": "shared_coal_recovery_unit_changed"}
                s["recovery_started"] = True
                self._save()
                return {
                    "type": "mine",
                    "name": "transport-belt",
                    "position": old["position"],
                    "count": 1,
                    "expected_entity_unit": old["unit_number"],
                    "expected_entity_world_id": obs["world_id"],
                    "reason": "recover one exact stocked shared coal header corner",
                }
            if not s.get("recovery_started"):
                return {"status": "blocked", "reason": "shared_coal_unstarted_corner_missing"}
            s["phase"] = "build"
            self._save()
        if s["phase"] == "build":
            result = self.builder.ensure_plan(obs, _plan(s["new_entities"], []))
            if result.get("status") != "succeeded":
                return result
            s["phase"] = "commission"
            self._save()
        evidence = self._health(obs)
        if evidence is None:
            return {
                "status": "waiting",
                "reason": "waiting_for_both_shared_coal_outputs_and_unchanged_brick_production_health",
            }
        if not s.get("published"):
            result = self._publish(obs, evidence)
            if result:
                return result
        s["phase"] = "verified"
        s["evidence"] = evidence
        self._save()
        return {
            "status": "succeeded",
            "reason": "one paid normal shared coal tap feeds steel and preserves brick source and production",
            "evidence": evidence,
        }

    def proof(self, obs):
        planned = self._planned(obs)
        if planned is not None:
            return planned.coal_proof(obs)
        if (
            not self.state
            or not self.state.get("published")
            or self.state["world_id"] != obs["world_id"]
            or self.state["catalog_fingerprint"] != self.catalog.fingerprint
        ):
            return None
        return self._health(obs)
