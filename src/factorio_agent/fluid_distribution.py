"""Normally paid same-fluid manifolds on an unclaimed fixed producer edge."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_game import DeterministicGame
from factorio_agent.core.deterministic_state import _atomic_json
from .chemical_production import _handoff, plan_typed_fluid_connection, source_owned_apron
from .fluid_handoffs import bound_handoffs
from .gas_distribution import GasDistribution, GAS_PROOF_LUA, gas_manifold_core, _edges
from .industrial import PlanningBudget
from .petroleum import pipeline_extent
from .planning import overlap, preflight, survey_slots
from .smelting import merge_entities
from .transport import VECTORS, xy


FLUIDS = {"water", "crude-oil", "petroleum-gas", "heavy-oil", "light-oil", "sulfuric-acid", "lubricant"}
NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")


def _key(e):
    return e["name"] + ":" + str(e["position"]["x"]) + ":" + str(e["position"]["y"])


def _payload(v):
    return json.dumps(json.dumps(v, separators=(",", ":")))


def _hash(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def manifold_core(x, y, fluid, aliases, side=-1):
    if fluid not in FLUIDS or len(aliases) != 2 or len(set(aliases)) != 2:
        raise ValueError("two unique same-fluid output names required")
    core = gas_manifold_core(x, y, side)
    core["inlet"]["item"] = fluid
    for port in core["ports"]:
        port["item"] = fluid
    core["alias_access"] = {name: path for name, path in zip(aliases, core["alias_access"].values())}
    core["fluid_groups"] = {fluid: deepcopy(core["entities"])}
    core["layout_policy"] = "same-fluid-two-outlet-v1"
    return core


class FluidDistribution(GasDistribution):
    """Use existing paid routing/edge proofs without changing old producer claims."""

    def __init__(self, game, builder, catalog, perimeter, *, source_key, aliases):
        if (
            not isinstance(source_key, str)
            or not NAME.fullmatch(source_key)
            or not isinstance(aliases, (tuple, list))
            or len(aliases) != 2
            or len(set(aliases)) != 2
            or source_key in aliases
            or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in aliases)
        ):
            raise ValueError("bounded source key and two distinct output aliases required")
        self.game, self.builder, self.catalog, self.perimeter = game, builder, catalog, perimeter
        self.source_key = source_key
        self.aliases = tuple(aliases)
        self.profile = {"source_key": source_key, "aliases": list(aliases)}
        token = _hash(self.profile)[:20]
        self.block_id = "fluid_distribution_" + token
        self.consumer_id = "fluid-header:" + token
        self.path = Path(game.cfg.runtime_dir) / ("fluid-distribution-" + token + ".json")
        self.sources_path = self.path.parent / "fluid-sources.json"
        self.last_proof_failure = None
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if self.state and self.state.get("profile") != self.profile:
            raise ValueError("fluid distribution profile changed")

    def _registry(self, obs):
        registry = json.loads(self.sources_path.read_text(encoding="utf-8")) if self.sources_path.exists() else {}
        if not self._identity(obs, registry) or not isinstance(registry.get("sources"), dict):
            return dict(ok=False, reason="fluid_distribution_registry_identity_changed")
        source = registry["sources"].get(self.source_key, {})
        if (
            source.get("kind") != "fluid"
            or source.get("item") not in FLUIDS
            or source.get("name") != "pipe"
            or source.get("direction") != "output"
            or source.get("facing") not in VECTORS
            or source.get("fluidbox_index") != 1
            or source.get("world_id") != obs.get("world_id")
            or type(source.get("unit_number")) is not int
            or source["unit_number"] < 1
        ):
            return dict(ok=False, reason="published_native_fluid_source_required", source_key=self.source_key)
        handoffs = registry.get("handoffs", {})
        if not isinstance(handoffs, dict):
            return dict(ok=False, reason="fluid_distribution_handoff_registry_invalid")
        for owner, rows in handoffs.items():
            if not isinstance(rows, dict) or any(not isinstance(r, dict) for r in rows.values()):
                return dict(ok=False, reason="fluid_distribution_handoff_registry_invalid")
            for row in rows.values():
                if row.get("source", {}).get("unit_number") == source["unit_number"]:
                    if owner != self.consumer_id:
                        return dict(
                            ok=False,
                            reason="fluid_source_edge_already_owned",
                            consumer_id=owner,
                            source_key=self.source_key,
                        )
                    if not self.state:
                        return dict(ok=False, reason="fluid_handoff_exists_without_distribution_checkpoint")
        if self.state and source != self.state["original_source"]:
            return dict(ok=False, reason="fluid_distribution_original_source_changed")
        if not self.state and any(alias in registry["sources"] for alias in self.aliases):
            return dict(ok=False, reason="fluid_distribution_alias_exists_without_checkpoint")
        return dict(ok=True, registry=registry, source=source)

    @staticmethod
    def _source_snapshot(game, source):
        return game.query(
            "local q=helpers.json_to_table("
            + _payload(source)
            + """);
local e=target(q.position,q.name)
if not d or d.world_id~=q.world_id or not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number
 or e.position.x~=q.position.x or e.position.y~=q.position.y then return{ok=false,reason="fluid_distribution_source_identity_changed"}end
for name in pairs(e.get_fluid_contents())do if name~=q.item then return{ok=false,reason="fluid_distribution_source_contaminated"}end end
if(e.get_fluid_contents()[q.item]or 0)<=0 or not e.has_fluid_segment(1)then return{ok=false,reason="actual_native_fluid_source_required"}end
return{ok=true,extent=e.get_fluid_segment_extent_bounding_box(1),limit=prototypes.utility_constants.default_pipeline_extent}"""
        )

    def prepare(self, obs):
        if self.state:
            return (
                deepcopy(self.state)
                if self._identity(obs, self.state)
                else dict(ok=False, reason="fluid_distribution_world_or_catalog_changed")
            )
        if not all(obs.get("enabled_recipes", {}).get(n) for n in ("pipe", "pipe-to-ground")):
            return dict(ok=False, reason="native_pipe_and_underground_pipe_recipes_required")
        ready = self._registry(obs)
        if not ready.get("ok"):
            return ready
        source = ready["source"]
        board = json.loads((self.path.parent / "pcb-board.json").read_text(encoding="utf-8"))
        if not self._identity(obs, board):
            return dict(ok=False, reason="fluid_distribution_board_identity_changed")
        blocks = [b for b in board["blocks"].values() if b.get("reserved_area")]
        if not any(
            any(
                all(port.get(k) == source.get(k) for k in ("name", "position", "item", "direction", "facing"))
                for port in block.get("ports", [])
            )
            for block in blocks
        ):
            return dict(ok=False, reason="fluid_distribution_registered_producer_port_required")
        apron = source_owned_apron(self.builder, source, blocks)
        if not apron.get("ok"):
            return apron
        start = apron["path"][-1] if apron["path"] else source["position"]
        sx, sy = start["x"], start["y"]
        budget = PlanningBudget(self.game)
        failures = []
        snapshot = self._source_snapshot(budget.game, source)
        if not snapshot.get("ok"):
            return snapshot
        for dx, dy, side in (
            (8, 0, -1),
            (12, -12, -1),
            (12, 12, 1),
            (24, -24, -1),
            (24, 24, 1),
            (40, -40, -1),
            (40, 40, 1),
        ):
            if budget.expired():
                return budget.failure(candidate_failures=failures[-5:])
            core = manifold_core(sx + dx, sy + dy, source["item"], self.aliases, side)
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
            route = plan_typed_fluid_connection(budget.game, self.builder, obs, source, core["inlet"], core, [])
            if not route.get("ok"):
                failures.append(route.get("reason"))
                continue
            landing = {xy(e): e for e in route["entities"]}.get(xy(core["inlet"]))
            own = [
                e
                for e in core["entities"]
                if not (landing and landing["name"] != "pipe" and xy(e) == xy(core["inlet"]))
            ]
            es = merge_entities(route["entities"], own)
            nodes = merge_entities([source], es)
            extent = pipeline_extent(snapshot["extent"], es, snapshot["limit"])
            if not extent.get("ok"):
                failures.append(extent.get("reason"))
                continue
            isolation = self._isolation(budget.game, source, es, route["maximum_underground_span"])
            if not isolation.get("ok"):
                failures.append(isolation.get("reason"))
                continue
            check = preflight(budget.game, self.builder, obs, es + core["output_access"])
            if not check.get("ok"):
                failures.append(check.get("reason"))
                continue
            scope = self.consumer_id
            defense = self.perimeter.prepare_coverage(obs, scope, [core["reserved_area"]])
            if not defense.get("ok"):
                failures.append(defense.get("reason"))
                continue
            crossed = self._crossed_assets(obs, route)
            if not crossed.get("ok"):
                failures.append(crossed.get("reason"))
                continue
            plan = _plan(
                es,
                core["ports"],
                reserved_area=core["reserved_area"],
                defense_scope_id=scope,
                clear_obstacles=check["clear_obstacles"],
                clearance_world_id=obs["world_id"],
                layout_policy=core["layout_policy"],
            )
            return dict(
                ok=True,
                world_id=obs["world_id"],
                catalog_fingerprint=self.catalog.fingerprint,
                profile=deepcopy(self.profile),
                phase="build",
                core=core,
                plan=plan,
                original_source=deepcopy(source),
                first_neighbor=route["first_neighbor"],
                edges=_edges(nodes, route["underground_pairs"]),
                nodes=nodes,
                bindings={},
                crossed_assets=crossed["assets"],
                maximum_underground_span=route["maximum_underground_span"],
                defense_scope_id=scope,
                prepared_tick=obs["tick"],
            )
        return dict(ok=False, reason="no_isolated_same_fluid_manifold", candidate_failures=failures[-5:])

    def _reserve(self, obs):
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        if not self._identity(obs, board):
            return dict(status="blocked", reason="fluid_distribution_board_identity_changed")
        old = board["blocks"].get(self.block_id)
        plan = self.state["plan"]
        if old and old != plan:
            return dict(status="blocked", reason="fluid_distribution_reservation_changed")
        if any(
            k != self.block_id and overlap(plan["reserved_area"], b["reserved_area"])
            for k, b in board["blocks"].items()
            if b.get("reserved_area")
        ):
            return dict(status="blocked", reason="fluid_distribution_reservation_conflict")
        if not old:
            board["blocks"][self.block_id] = plan
            _atomic_json(path, board)
        return None

    def _bind(self, obs):
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        for spec in self.state["plan"]["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            unit = self.state["bindings"].get(_key(spec))
            if unit and (
                not live
                or live.get("unit_number") != unit
                or (spec["name"] == "pipe-to-ground" and live.get("direction") != spec["direction"])
            ):
                return dict(status="blocked", reason="fluid_distribution_owned_asset_changed")
            if live and not unit:
                return dict(status="blocked", reason="fluid_distribution_unbound_asset_present")
        return None

    def _reload(self):
        """The checkpoint on disk is the truth: ``execute_build_fluid_distribution`` records the ACK
        (binding, cleared ``pending_build``) through its own instance, so a long-lived controller
        must re-read it, or it keeps the stale pending build and later overwrites the binding."""
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if self.state and self.state.get("profile") != self.profile:
            raise ValueError("fluid distribution profile changed")

    def step(self, obs):
        self._reload()
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return dict(prepared, status="blocked")
            self.state = prepared
            self._save()
        if not self._identity(obs, self.state):
            return dict(status="blocked", reason="fluid_distribution_world_or_catalog_changed")
        if self.state.get("pending_build"):
            return dict(status="blocked", reason="fluid_distribution_build_outcome_requires_reconciliation")
        ready = self._registry(obs)
        if not ready.get("ok"):
            return dict(ready, status="blocked")
        for call in (self._bind, self._reserve):
            result = call(obs)
            if result:
                return result
        result = self.perimeter.ensure_coverage(
            obs, self.state["defense_scope_id"], [self.state["core"]["reserved_area"]]
        )
        if result is not None:
            return result
        first = {k: v for k, v in self.state["first_neighbor"].items() if k != "unit_number"}
        fluid = self.state["original_source"]["item"]
        declared = _handoff(self.game, self.catalog, obs, self.consumer_id, fluid, self.state["original_source"], first)
        if not declared.get("ok"):
            return dict(status="blocked", reason="fluid_distribution_handoff_not_authorized", evidence=declared)
        unit = self.state["bindings"].get(_key(first))
        if unit:
            bound = _handoff(
                self.game,
                self.catalog,
                obs,
                self.consumer_id,
                fluid,
                self.state["original_source"],
                dict(first, unit_number=unit),
                bind=True,
            )
            if not bound.get("ok"):
                return dict(status="blocked", reason="fluid_distribution_handoff_not_bound", evidence=bound)
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("type") == "build_many":
            result = deepcopy(result["actions"][0])
        if result.get("type") == "build":
            if result.get("name") not in ("pipe", "pipe-to-ground"):
                return dict(status="blocked", reason="fluid_distribution_unexpected_build")
            action = dict(
                type="build_fluid_distribution",
                profile=deepcopy(self.profile),
                ordinary_build=deepcopy(result),
                expected_world_id=obs["world_id"],
                expected_catalog_fingerprint=self.catalog.fingerprint,
            )
            self.state["pending_build"] = deepcopy(action)
            self.state["build_phase"] = "ready"
            self._save()
            return action
        if result.get("status") != "succeeded":
            return result
        evidence = self.proof(obs)
        if not evidence:
            return dict(
                status="waiting",
                reason="waiting_for_actual_connected_fluid_distribution_outlets",
                evidence=self.last_proof_failure,
            )
        ready = self._registry(obs)
        if not ready.get("ok"):
            return dict(ready, status="blocked")
        registry = ready["registry"]
        for name, source in evidence["sources"].items():
            if registry["sources"].get(name) not in (None, source):
                return dict(status="blocked", reason="published_fluid_alias_identity_changed", alias=name)
        registry["sources"].update(evidence["sources"])
        _atomic_json(self.sources_path, registry)
        self.state.update(phase="verified", proof=evidence)
        self._save()
        return dict(
            status="succeeded",
            reason="normally paid shared fluid header has two native filled outlets",
            evidence=evidence,
        )

    def proof(self, obs):
        self.last_proof_failure = None
        if not self.state or not self._identity(obs, self.state):
            return None
        ready = self._registry(obs)
        if not ready.get("ok"):
            self.last_proof_failure = ready
            return None
        source = self.state["original_source"]
        fluid = source["item"]
        first = dict(
            self.state["first_neighbor"], unit_number=self.state["bindings"].get(_key(self.state["first_neighbor"]))
        )
        record = ready["registry"].get("handoffs", {}).get(self.consumer_id, {}).get(fluid, {})
        if (
            record.get("phase") != "bound"
            or record.get("source") != source
            or record.get("first_neighbor") != first
            or not self._identity(obs, record)
        ):
            self.last_proof_failure = {"reason": "fluid_distribution_upstream_handoff_not_bound"}
            return None
        defense = self.perimeter.coverage_proof(obs, self.state["defense_scope_id"])
        if not defense:
            return None
        protected = self._protect(obs)
        if not protected.get("ok"):
            self.last_proof_failure = protected
            return None
        bindings = {**self.state["bindings"], _key(source): source["unit_number"]}
        if any(_key(e) not in bindings for e in self.state["nodes"]):
            return None
        aliases = {
            name: dict(
                spec,
                world_id=obs["world_id"],
                unit_number=bindings[_key(spec)],
                defense_scope_id=self.state["defense_scope_id"],
            )
            for name, spec in zip(self.aliases, self.state["core"]["ports"])
        }
        downstream = []
        try:
            for output in aliases.values():
                downstream += [
                    dict(source=output, first_neighbor=r["first_neighbor"])
                    for r in bound_handoffs(ready["registry"], obs, self.catalog, output)
                ]
        except (ValueError, KeyError, TypeError) as exc:
            self.last_proof_failure = {"reason": str(exc)}
            return None
        args = dict(
            source=source,
            fluid=fluid,
            nodes=[dict(e, unit_number=bindings[_key(e)]) for e in self.state["nodes"]],
            edges=self.state["edges"],
            aliases=aliases,
            downstream=downstream,
            world_id=obs["world_id"],
        )
        native = self.game.query(FLUID_PROOF_LUA.replace("PAYLOAD", _payload(args)))
        if not native.get("ok"):
            self.last_proof_failure = native
            return None
        return dict(
            tick=native["tick"],
            sources=aliases,
            native=native,
            defense_coverage=defense,
            scope="one real input shared by two paid outlets; no multiplied supply or sustained throughput claim",
        )


FLUID_PROOF_LUA = GAS_PROOF_LUA.replace('"petroleum-gas"', "args.fluid ")


BUILD_GUARD_LUA = r"""
local args=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=args.world_id then return {ok=false,reason="fluid_distribution_world_changed",pre_effect=true}end
local allowed={};local source=target(args.source.position,args.source.name)
if not source or source.force~=f or source.surface~=s or source.unit_number~=args.source.unit_number then return {ok=false,reason="fluid_distribution_source_changed",pre_effect=true}end
allowed[source.unit_number]=true
if not source.has_fluid_segment(1)then return {ok=false,reason="fluid_distribution_source_pipeline_missing",pre_effect=true}end
local extent=source.get_fluid_segment_extent_bounding_box(1);local minx,miny=extent.left_top.x,extent.left_top.y
local maxx,maxy=extent.right_bottom.x,extent.right_bottom.y
for _,q in ipairs(args.nodes)do minx=math.min(minx,q.position.x-.5);miny=math.min(miny,q.position.y-.5);maxx=math.max(maxx,q.position.x+.5);maxy=math.max(maxy,q.position.y+.5)end
local limit=prototypes.utility_constants.default_pipeline_extent
if maxx-minx>limit or maxy-miny>limit then return {ok=false,reason="fluid_distribution_native_extent_changed",pre_effect=true}end
for _,q in ipairs(args.assets)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.direction~=q.direction then return {ok=false,reason="fluid_distribution_crossed_asset_changed",pre_effect=true}end
end
for _,q in ipairs(args.nodes)do local e=target(q.position,q.name)
 if q.unit_number then
  if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or(e.name=="pipe-to-ground"and e.direction~=q.direction)then return {ok=false,reason="fluid_distribution_owned_asset_changed",pre_effect=true}end
  for name in pairs(e.get_fluid_contents())do if name~=args.fluid then return {ok=false,reason="fluid_distribution_contaminated",pre_effect=true}end end
  allowed[e.unit_number]=true
 elseif e then return {ok=false,reason="fluid_distribution_unbound_asset_present",pre_effect=true}end
end
for _,q in ipairs(args.nodes)do local p=q.position
 if q.unit_number~=args.source.unit_number then
  if s.count_entities_filtered{area={{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}},type="resource"}>0 then return {ok=false,reason="fluid_distribution_resource_changed",pre_effect=true}end
  for _,e in pairs(s.find_entities_filtered{area={{p.x-1.49,p.y-1.49},{p.x+1.49,p.y+1.49}}})do
   if not allowed[e.unit_number]then for _ in pairs(e.prototype.fluidbox_prototypes or{})do return {ok=false,reason="fluid_distribution_incidental_foreign_fluid",pre_effect=true}end end
  end
  if q.name=="pipe-to-ground"then for _,e in pairs(s.find_entities_filtered{position=p,radius=args.maximum,name="pipe-to-ground"})do
   if(e.position.x==p.x or e.position.y==p.y)and not allowed[e.unit_number]then return {ok=false,reason="fluid_distribution_foreign_buried_peer",pre_effect=true}end
  end end
 end
end
"""


def execute_build_fluid_distribution(game, action):
    """Prepend guards to the ordinary native build query, retaining its cost rules."""
    allowed = {"type", "profile", "ordinary_build", "expected_world_id", "expected_catalog_fingerprint"}
    if not isinstance(action, dict) or set(action) != allowed or action.get("type") != "build_fluid_distribution":
        raise ValueError("invalid fluid distribution build")
    profile = action["profile"]
    ordinary = action["ordinary_build"]
    if (
        not isinstance(profile, dict)
        or set(profile) != {"source_key", "aliases"}
        or not isinstance(ordinary, dict)
        or ordinary.get("type") != "build"
        or ordinary.get("name") not in ("pipe", "pipe-to-ground")
    ):
        raise ValueError("ordinary pipe build required")
    from factorio_agent.core.world_catalog import WorldCatalog

    root = Path(game.cfg.runtime_dir)
    catalog = WorldCatalog.from_dict(json.loads((root / "catalog.json").read_text(encoding="utf-8")))
    controller = FluidDistribution(game, None, catalog, None, **profile)
    state = controller.state
    if (
        state.get("pending_build") != action
        or state.get("build_phase") != "ready"
        or state.get("world_id") != action["expected_world_id"]
        or state.get("catalog_fingerprint") != catalog.fingerprint
        or catalog.fingerprint != action["expected_catalog_fingerprint"]
        or getattr(game, "expected_world", None) != state.get("world_id")
    ):
        raise ValueError("fluid distribution build authority changed")
    if not any(
        all(ordinary.get(k, 0) == spec.get(k, 0) for k in ("name", "position", "direction"))
        for spec in state["plan"]["entities"]
    ):
        raise ValueError("pipe build is outside the fixed paid header")
    board = json.loads((root / "pcb-board.json").read_text(encoding="utf-8"))
    if (
        not controller._identity({"world_id": state["world_id"]}, board)
        or board["blocks"].get(controller.block_id) != state["plan"]
    ):
        raise ValueError("fluid distribution PCB authority changed")
    ready = controller._registry({"world_id": state["world_id"]})
    if not ready.get("ok"):
        raise ValueError(ready.get("reason"))
    source = state["original_source"]
    nodes = [
        dict(q, **({"unit_number": state["bindings"][_key(q)]} if _key(q) in state["bindings"] else {}))
        for q in state["plan"]["entities"]
    ]
    nodes.append(source)
    guard = BUILD_GUARD_LUA.replace(
        "PAYLOAD",
        _payload(
            dict(
                world_id=state["world_id"],
                source=source,
                fluid=source["item"],
                nodes=nodes,
                maximum=state["maximum_underground_span"],
                assets=state["crossed_assets"],
            )
        ),
    )

    class GuardedQuery:
        def __getattr__(self, name):
            return getattr(game, name)

        def query(self, body):
            return game.query(guard + body)

        def _record_action(self, _, result):
            return result

    state["build_phase"] = "executing"
    _atomic_json(controller.path, state)
    try:
        result = DeterministicGame.act(GuardedQuery(), ordinary)
        if result.get("ok") is True:
            unit = result.get("unit_number")
            if type(unit) is not int or unit < 1:
                raise ValueError("ordinary pipe build native unit unknown")
            state["bindings"][_key(ordinary)] = unit
        state.pop("pending_build", None)
        state["build_phase"] = "ready"
        _atomic_json(controller.path, state)
        return result
    except Exception:
        state["build_phase"] = "outcome_unknown"
        _atomic_json(controller.path, state)
        raise
