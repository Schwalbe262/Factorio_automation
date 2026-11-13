"""Independent, paid coal/stone drill pairs and identity-bound material ports."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .paired_mining import prepare_new_bank
from .planning import overlap, preflight
from .smelting import port, merge_entities
from .transport import xy
from .resource_policy import annotate_mining_exit, mining_exit_candidates, MINING_EXIT_LUA
from .power_grid import plan_connection as power_connection
from .industrial import PlanningBudget
from . import mine_trunk


def validate_material(parameters):
    if (
        not isinstance(parameters, dict)
        or set(parameters) != {"resource"}
        or parameters["resource"] not in ("coal", "stone")
    ):
        raise ValueError("dedicated_materials requires resource coal or stone")


TRUNK_MODE = "trunk_tap"  # the supply is a splitter tap on the patch bank's trunk (mine_trunk.py)


def _same_plan(a, b):
    return a["plan"] == b["plan"] and a.get("reserved_area") == b.get("reserved_area")


class DedicatedMaterials:
    def __init__(self, game, builder, catalog, outposts, *, perimeter=None, purpose=None, smelting=None):
        if purpose not in (None, "chemistry"):
            raise ValueError("material purpose must be omitted or chemistry")
        self.game, self.builder, self.catalog, self.outposts = game, builder, catalog, outposts
        # The district controller of this slice: its in-memory plan is refreshed when a
        # trunk tap republishes it (mine_trunk.release_trunk_belts), so it never saves the old one.
        self.smelting = smelting
        self.perimeter = perimeter
        self.purpose = purpose
        filename = "material-supply-chemistry.json" if purpose else "material-supply.json"
        self.path = Path(game.cfg.runtime_dir) / filename
        self.sources_path = self.path.parent / "material-sources.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _save(self):
        _atomic_json(self.path, self.state)

    def _outposts(self):
        path = Path(self.outposts.path)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else deepcopy(self.outposts.state)

    def _valid(self, state, world):
        return (
            state.get("world_id") == world
            and state.get("catalog_fingerprint", self.catalog.fingerprint) == self.catalog.fingerprint
        )

    def _supply_id(self, world, resource):
        return "dedicated-material:" + world + ":" + resource + (":chemistry" if self.purpose else "")

    def _checkpoint_valid(self, state, world):
        return (
            self._valid(state, world)
            and state.get("purpose") == self.purpose
            and (not self.purpose or state.get("catalog_fingerprint") == self.catalog.fingerprint)
            and all(self._entry_valid(entry, world, resource) for resource, entry in state.get("supplies", {}).items())
        )

    def _entry_valid(self, entry, world, resource):
        expected = self._supply_id(world, resource)
        if entry.get("mode") == TRUNK_MODE:
            return (
                entry.get("resource") == resource
                and entry.get("material_supply_id") == expected
                and entry.get("purpose") == self.purpose
                and isinstance(entry.get("tap"), dict)
                and entry.get("bank", {}).get("plan", {}).get("resource") == resource
                and (self.purpose is None or resource == "coal")
            )
        return (
            entry.get("resource") == resource
            and entry.get("material_supply_id") == expected
            and entry.get("purpose") == self.purpose
            and entry.get("bank", {}).get("material_supply_id") == expected
            and entry["bank"].get("material_supply_purpose") == self.purpose
            and entry["bank"].get("plan", {}).get("resource") == resource
            and (self.purpose is None or resource == "coal")
        )

    def _coal_hub(self, source, reservations, planned=(), mining_area=None, *, game=None, layout_contract=None):
        ordinary = source.get("mining_resource") is None
        if ordinary:
            from .mining_layout_policy import _hash, validate_policy

            if (
                not isinstance(layout_contract, dict)
                or not layout_contract.get("output_resource_free_verified")
                or layout_contract.get("fixed_output_port") != source
                or source.get("kind") != "item"
                or source.get("item") != "coal"
                or source.get("facing") != 4
                or source.get("direction") != "output"
                or layout_contract.get("contract_id")
                != _hash({k: v for k, v in layout_contract.items() if k != "contract_id"})
            ):
                return {"ok": False, "reason": "coal_ordinary_output_requires_exact_verified_layout_contract"}
            try:
                validate_policy(
                    layout_contract["policy"], layout_contract["policy"]["world_id"], self.catalog.fingerprint, "coal"
                )
            except (KeyError, TypeError, ValueError):
                return {"ok": False, "reason": "coal_ordinary_output_layout_policy_changed"}
        zones = [{"area": area, "resource": "__other_reserved_area__"} for area in reservations]
        if mining_area:
            zones.append({"area": mining_area, "resource": "coal"})
        payload = json.dumps(
            json.dumps(
                {
                    "source": source,
                    "areas": reservations,
                    "zones": zones,
                    "ordinary": ordinary,
                    "world_id": layout_contract["policy"]["world_id"] if ordinary else None,
                    "planned": list(planned),
                    "exit_candidates": mining_exit_candidates(source["position"], extra_clear_steps=4),
                },
                separators=(",", ":"),
            )
        )
        return (game or self.game).query(
            """
local args=helpers.json_to_table("""
            + payload
            + """);
"""
            + MINING_EXIT_LUA
            + """
local function hub_clear(p)
 local x=p.x;local y=p.y;local area={{x+.5,y-1.5},{x+4.5,y+2.5}};local reserved=false
 for _,r in ipairs(args.areas) do
  if area[1][1]<r[2][1] and area[2][1]>r[1][1] and area[1][2]<r[2][2] and area[2][2]>r[1][2] then reserved=true;break end
 end
 for _,spec in ipairs(args.planned)do
  local proto=prototypes.entity[spec.name];local w=spec._width or proto.tile_width;local h=spec._height or proto.tile_height
  if spec.direction==4 or spec.direction==12 then w,h=h,w end
  local q=spec.position
  if q.x-w/2<area[2][1]and q.x+w/2>area[1][1]and q.y-h/2<area[2][2]and q.y+h/2>area[1][2]then return false end
 end
 for _,offset in ipairs{{3,0},{4,0},{4,-1},{3,1},{4,1},{4,2}}do
  if not s.can_place_entity{name="transport-belt",position={x=x+offset[1],y=y+offset[2]},direction=4,force=f}then return false end
 end
 return not reserved and s.count_entities_filtered{area=area,type="resource"}==0
  and s.can_place_entity{name="splitter",position={x=x+1,y=y+.5},direction=4,force=f}
  and s.can_place_entity{name="transport-belt",position={x=x+2,y=y},direction=4,force=f}
  and s.can_place_entity{name="transport-belt",position={x=x+2,y=y+1},direction=4,force=f}
  and s.can_place_entity{name="transport-belt",position={x=x+3,y=y},direction=4,force=f}
  and s.can_place_entity{name="transport-belt",position={x=x+3,y=y+1},direction=4,force=f}
end
if args.ordinary then
 if not d or d.world_id~=args.world_id then return {ok=false,reason="coal_ordinary_source_world_changed"}end
 local path={};local origin=args.source.position;local declared=false
 for _,spec in ipairs(args.planned)do if spec.name=="transport-belt"and spec.direction==4
  and spec.position.x==origin.x and spec.position.y==origin.y and not spec.mining_resource then declared=true end end
 local live=target(origin,"transport-belt")
 if live and(not args.source.unit_number or live.unit_number~=args.source.unit_number or live.force~=f or live.surface~=s
  or live.direction~=4 or live.position.x~=origin.x or live.position.y~=origin.y)then
  return {ok=false,reason="coal_ordinary_source_native_identity_changed"}end
 if not declared then return {ok=false,reason="coal_ordinary_source_not_in_whole_plan"}end
 for step=0,4 do local p={x=origin.x+step,y=origin.y};local occupied=mining_patch_at(p,"coal",args.zones)
  if occupied then break end
  if step>0 then
   local conflict=false
   for _,spec in ipairs(args.planned)do local proto=prototypes.entity[spec.name];local w=spec._width or proto.tile_width;local h=spec._height or proto.tile_height
    if spec.direction==4 or spec.direction==12 then w,h=h,w end
    if math.abs(spec.position.x-p.x)<w/2+.49 and math.abs(spec.position.y-p.y)<h/2+.49 then conflict=true;break end
   end
   if conflict or not s.can_place_entity{name="transport-belt",position=p,direction=4,force=f}then break end
  end
  path[#path+1]=p
  if hub_clear(p)then local connector={};for i=2,#path do connector[#connector+1]=path[i]end
   return {ok=true,exit_prefix={origin},exit_length=-1,hub_connector=connector,hub_position=p,ordinary_offore_source=true}end
 end
 return {ok=false,reason="no_clear_ordinary_coal_distribution_hub"}
end
return select_mining_exit(args.source,args.zones,{},args.planned,args.exit_candidates,hub_clear,false,4)
"""
        )

    @staticmethod
    def _hub_geometry(source, hub):
        path = hub.get("exit_prefix")
        if path is None:
            x, y = xy(source)
            path = [{"x": x + i, "y": y} for i in range(hub["offset"] + 1)]
        else:
            path = path + (hub.get("hub_connector") or [])
        last = path[-1]
        return path, last["x"], last["y"]

    def _distribution_area(self, bank, hub):
        path, x, y = self._hub_geometry(bank["plan"]["ports"][0], hub)
        area = deepcopy(bank["reserved_area"])
        area[0][0] = min(area[0][0], min(p["x"] - 0.5 for p in path))
        area[0][1] = min(area[0][1], y - 1.5, min(p["y"] - 0.5 for p in path))
        area[1][0] = max(area[1][0], x + 4.5, max(p["x"] + 0.5 for p in path))
        area[1][1] = max(area[1][1], y + 2.5, max(p["y"] + 0.5 for p in path))
        return area

    @staticmethod
    def _output_access(x, y):
        return {
            "coal": [{"x": x + 2, "y": y}, {"x": x + 3, "y": y}, {"x": x + 4, "y": y}, {"x": x + 4, "y": y - 1}],
            "brick-coal": [
                {"x": x + 2, "y": y + 1},
                {"x": x + 3, "y": y + 1},
                {"x": x + 4, "y": y + 1},
                {"x": x + 4, "y": y + 2},
            ],
        }

    @staticmethod
    def _published_power_keepouts(banks):
        points = set()
        for bank in banks:
            distribution = bank.get("material_distribution", {})
            paths = distribution.get("output_access")
            if paths:
                points.update((p["x"], p["y"]) for path in paths.values() for p in path[1:])
            else:
                ports = list(distribution.get("ports", {}).values()) or bank["plan"]["ports"]
                for source in ports:
                    if source.get("direction") == "output" and source.get("facing") == 4:
                        x, y = xy(source)
                        points.update(((x + 1, y), (x + 2, y)))
        return [entity("transport-belt", x, y, 4) for x, y in sorted(points)]

    def prepare(self, obs, parameters):
        validate_material(parameters)
        resource = parameters["resource"]
        if self.purpose and resource != "coal":
            return {"ok": False, "reason": "chemistry_material_supply_requires_coal"}
        if self.state and not self._checkpoint_valid(self.state, obs["world_id"]):
            return {"ok": False, "reason": "material_supply_purpose_or_identity_changed"}
        if self.purpose and self.perimeter is None:
            return {"ok": False, "reason": "chemistry_material_supply_requires_perimeter"}
        if not obs.get("technologies", {}).get("electric-mining-drill"):
            return {"ok": False, "reason": "native_electric_mining_required"}
        saved = self._outposts()
        if not self._valid(saved, obs["world_id"]):
            return {"ok": False, "reason": "material_outposts_world_or_catalog_changed"}
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if not self._valid(board, obs["world_id"]):
            return {"ok": False, "reason": "material_board_world_or_catalog_changed"}
        supply_id = self._supply_id(obs["world_id"], resource)
        existing = [b for b in saved.get("banks", []) if b.get("material_supply_id") == supply_id]
        if len(existing) > 1:
            return {"ok": False, "reason": "duplicate_dedicated_material_bank_identity"}
        seed = mine_trunk.patch_seed(obs, resource)
        patch = (
            None if existing else mine_trunk.patch_bank(saved.get("banks"), resource, seed, exclude_supply=supply_id)
        )
        second_group = None
        if patch is not None:
            # One mining block per patch (mine_trunk rule 1): tap the patch bank's trunk.
            tapped = self._prepare_trunk(obs, resource, supply_id, patch, board)
            if tapped.get("ok"):
                return tapped
            second_group = {k: tapped[k] for k in ("reason", "rows_rejected", "failures") if k in tapped}
        if existing:
            bank = deepcopy(existing[0])
            if (
                bank["plan"].get("resource") != resource
                or not bank["plan"].get("paired")
                or bank.get("material_supply_purpose") != self.purpose
            ):
                return {"ok": False, "reason": "dedicated_material_bank_configuration_changed"}
        else:
            budget = PlanningBudget(self.game) if self.purpose else None
            planning_game = budget.game if budget else self.game
            reservations = [b["reserved_area"] for b in board.get("blocks", {}).values() if b.get("reserved_area")]
            reservations += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", [])]
            reservations += [
                b.get("reserved_area", b["plan"].get("reserved_area"))
                for b in saved.get("banks", [])
                if b.get("reserved_area", b["plan"].get("reserved_area"))
            ]
            planning_reservations = reservations
            if resource == "coal":
                planning_reservations = [[[r[0][0] - 2, r[0][1]], list(r[1])] for r in reservations]
            hub = None
            failures = []
            for attempt in range(6 if resource == "coal" else 1):
                if budget and budget.expired():
                    return budget.failure(candidate_failures=failures, attempted_banks=attempt)
                bank = prepare_new_bank(planning_game, self.builder, obs, resource, 2, planning_reservations)
                if not bank.get("ok"):
                    return dict(bank, candidate_failures=failures, attempted_banks=attempt + 1)
                if resource != "coal":
                    break
                hub = self._coal_hub(
                    bank["plan"]["ports"][0],
                    reservations,
                    bank["plan"]["entities"],
                    bank["plan"].get("mining_reserved_area", bank["reserved_area"]),
                    game=planning_game,
                    layout_contract=bank.get("layout_contract"),
                )
                if hub.get("ok"):
                    grown = self._distribution_area(bank, hub)
                    if not any(overlap(grown, r) for r in reservations):
                        break
                    hub = {"ok": False, "reason": "coal_distribution_reservation_conflict"}
                failures.append(hub.get("reason", "coal_hub_unavailable"))
                planning_reservations = planning_reservations + [deepcopy(bank["reserved_area"])]
            else:
                return {
                    "ok": False,
                    "reason": "no_independent_coal_bank_with_ore_free_hub",
                    "candidate_failures": failures,
                    "attempted_banks": 6,
                }
            bank = deepcopy(bank)
            bank.update(material_supply_id=supply_id, ready=False, consumer_connection=None)
            if self.purpose:
                bank["material_supply_purpose"] = self.purpose
            drills = [e for row in bank["plan"]["producer_rows"].values() for e in row]
            drill_positions = {xy(e) for e in drills}
            pole_positions = {(x + 2, y) for x, y in drill_positions}
            contracted = bool(bank.get("layout_contract"))
            if contracted:
                # Keep the generator's narrow local and exit annotations intact.
                core = [
                    e
                    for e in bank["plan"]["entities"]
                    if e["name"] != "small-electric-pole" or e.get("mining_resource")
                ]
                core_area = bank["plan"]["mining_footprint"]
            else:
                output_x, output_y = xy(bank["plan"]["ports"][0])
                left_x = min(x for x, _ in drill_positions) - 1
                core = [
                    e
                    for e in bank["plan"]["entities"]
                    if (e["name"] == "electric-mining-drill" and xy(e) in drill_positions)
                    or (e["name"] == "small-electric-pole" and xy(e) in pole_positions)
                    or (e["name"] == "transport-belt" and xy(e)[1] == output_y and left_x <= xy(e)[0] <= output_x)
                ]
                occupied = self.builder._occupied_by_plan(core)
                core_area = [
                    [min(x for x, _ in occupied) - 0.5, min(y for _, y in occupied) - 0.5],
                    [max(x for x, _ in occupied) + 0.5, max(y for _, y in occupied) + 0.5],
                ]
                bank["plan"]["mining_footprint"] = core_area
            core_keys = {(e["name"], *xy(e)) for e in core}
            for spec in bank["plan"]["entities"]:
                if contracted:
                    continue
                if (spec["name"], *xy(spec)) in core_keys:
                    spec["mining_resource"] = resource
                    spec["mining_footprint"] = core_area
                    spec["resource_role"] = "mining_core"
                else:
                    spec.pop("mining_resource", None)
                    spec.pop("mining_footprint", None)
            if resource == "coal":
                source = bank["plan"]["ports"][0]
                if not contracted:
                    bank["plan"]["mining_reserved_area"] = deepcopy(bank["reserved_area"])
                path, hub_x, hub_y = self._hub_geometry(source, hub)
                mining_path = hub.get("exit_prefix", path)
                if len(path) > 1:
                    vectors = {(0, -1): 0, (1, 0): 4, (0, 1): 8}
                    directions = [vectors[(b["x"] - a["x"], b["y"] - a["y"])] for a, b in zip(path, path[1:])] + [4]
                else:
                    directions = [4]
                splitter = dict(
                    entity("splitter", hub_x + 1, hub_y + 0.5, 4),
                    _width=2,
                    _height=1,
                    splitter_input_priority="none",
                    splitter_output_priority="none",
                )
                additions = [entity("transport-belt", p["x"], p["y"], directions[i]) for i, p in enumerate(path) if i]
                additions += [
                    splitter,
                    entity("transport-belt", hub_x + 2, hub_y, 4),
                    entity("transport-belt", hub_x + 2, hub_y + 1, 4),
                ]
                area = self._distribution_area(bank, hub)
                if any(overlap(area, r) for r in reservations):
                    return {"ok": False, "reason": "coal_distribution_reservation_conflict"}
                es = merge_entities(core, additions)
                access = self._output_access(hub_x, hub_y)
                if self.purpose:
                    access = {"plastic-coal": access["coal"], "chemical-coal-reserve": access["brick-coal"]}
                empty = {(p["x"], p["y"]) for path in access.values() for p in path[1:]}
                old_bridge = [e for e in bank["plan"]["entities"] if (e["name"], *xy(e)) not in core_keys]
                if empty & self.builder._occupied_by_plan(es + old_bridge):
                    return {"ok": False, "reason": "coal_output_access_obstructed_by_prospective_entity"}
                keepouts = self._published_power_keepouts(saved.get("banks", []))
                keepouts += [entity("transport-belt", x, y, 4) for x, y in sorted(empty)]
                if contracted:
                    keepouts += bank["layout_contract"]["growth_entities"]
                pole = next(
                    e
                    for e in core
                    if e["name"] == "small-electric-pole"
                    and (not contracted or e["position"] == bank["layout_contract"]["fixed_power_port"])
                )
                power = power_connection(
                    planning_game,
                    self.builder,
                    obs,
                    pole["position"],
                    [e for e in es if e["name"] != "small-electric-pole"] + keepouts,
                )
                if not power.get("ok"):
                    return {"ok": False, "reason": "coal_power_bridge_cannot_preserve_output_access", "power": power}
                es = merge_entities(power["entities"], es)
                if empty & self.builder._occupied_by_plan(es):
                    return {"ok": False, "reason": "coal_output_access_obstructed_by_prospective_entity"}
                bank["wire_hops"] = power["wire_hops"]
                last_resource = hub.get("exit_length", hub.get("last_resource_step", 0))
                if last_resource >= 0 and len(mining_path) > 1:
                    annotate_mining_exit(es, resource, source["position"], last_resource, path=mining_path)
                    approved = [e for e in es if e.get("mining_exit")]
                    for spec in approved:
                        spec["resource_role"] = "mining_escape"
                    bank["plan"]["resource_escape_area"] = [
                        [min(p["x"] for p in mining_path[:-1]) - 0.5, min(p["y"] for p in mining_path[:-1]) - 0.5],
                        [max(p["x"] for p in mining_path[:-1]) + 0.5, max(p["y"] for p in mining_path[:-1]) + 0.5],
                    ]
                check = preflight(planning_game, self.builder, obs, es)
                if not check.get("ok"):
                    return {"ok": False, "reason": "whole_coal_distribution_preflight_failed", "preflight": check}
                old = bank["plan"]
                extra = {
                    k: v
                    for k, v in old.items()
                    if k not in ("ok", "reason", "entities", "ports", "required_items", "clear_obstacles")
                }
                obstacles = merge_entities(old.get("clear_obstacles", []), check.get("clear_obstacles", []))
                bank["plan"] = _plan(es, old["ports"], **extra, clear_obstacles=obstacles, output_access=access)
                bank["reserved_area"] = area
                bank["material_distribution"] = {
                    "splitter": splitter,
                    "ports": {
                        ("plastic-coal" if self.purpose else "coal"): port("coal", hub_x + 2, hub_y, 4, "output"),
                        ("chemical-coal-reserve" if self.purpose else "brick-coal"): port(
                            "coal", hub_x + 2, hub_y + 1, 4, "output"
                        ),
                    },
                    "output_access": access,
                    "output_access_policy": "reserve two clear east cells then independent outward turns; no power or production entities",
                }
        extra = {}
        if self.purpose:
            defense = self.perimeter.prepare_coverage(obs, supply_id, [bank["reserved_area"]])
            if not defense.get("ok"):
                return {"ok": False, "reason": "chemistry_coal_defense_preflight_failed", "evidence": defense}
            extra = {"purpose": self.purpose, "defense_preview": defense}
        return {
            "ok": True,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "resource": resource,
            "material_supply_id": supply_id,
            "bank": bank,
            "entity_units": deepcopy(bank.get("material_entity_units", [])),
            "phase": "build",
            **extra,
            # Recorded exception: the patch bank's trunk had no tap site, so this supply opens
            # its own group; it is to be retired once a trunk tap serves the consumer.
            **({"second_group_reason": second_group} if second_group is not None else {}),
        }

    def _tap_name(self, resource):
        return "dedicated_materials:" + resource + (":" + self.purpose if self.purpose else "")

    def _port_keys(self, resource):
        if resource != "coal":
            return None
        return ("plastic-coal", "chemical-coal-reserve") if self.purpose else ("coal", "brick-coal")

    def _prepare_trunk(self, obs, resource, supply_id, bank, board):
        """A splitter tap on the patch bank's trunk plus its output ports (no ``act``)."""
        from .board import blocks_without_bus, surface_conflict

        drills = sum(e["name"] == "electric-mining-drill" for e in bank["plan"]["entities"]) or 1
        # What an independent pair would have supplied; the trunk receipt grows the bank to cover it.
        demand = 2 * float(bank.get("nominal_rate_per_minute", 0)) / drills
        root = self.path.parent
        name = self._tap_name(resource)
        key = mine_trunk.trunk_key(bank)
        blocks = blocks_without_bus(board)
        keys = self._port_keys(resource)
        failures = []

        def accept(tap):
            extra = mine_trunk.hub(tap, keys) if keys else None
            entities = list(tap["entities"]) + (extra["entities"] if extra else [])
            clash = surface_conflict(self.builder, obs, blocks, entities)
            if clash is not None:
                failures.append({"reason": "mine_trunk_tap_meets_planned_hardware", "along": tap["along"]})
                return False
            check = preflight(self.game, self.builder, obs, entities, ignored_units=tuple(tap["remove_units"]))
            if not check.get("ok"):
                failures.append({"reason": check.get("reason"), "along": tap["along"], "side": tap["side"]})
                return False
            tap["hub"] = extra
            return True

        planned = mine_trunk.plan_tap(
            bank,
            obs,
            owner="dedicated_materials",
            name=name,
            demand=demand,
            serve="after",
            stations=mine_trunk.stations(root, obs["world_id"], key, exclude=name),
            accept=accept,
        )
        if not planned.get("ok"):
            return dict(planned, failures=failures[-6:])
        tap = planned["taps"][0]
        extra = tap.pop("hub")
        ports = extra["ports"] if extra else {resource: tap["port"]}
        hardware = list(tap["entities"]) + (extra["entities"] if extra else [])
        entry = {
            "ok": True,
            "mode": TRUNK_MODE,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "resource": resource,
            "material_supply_id": supply_id,
            "purpose": self.purpose,
            "trunk": key,
            "tap": tap,
            "hub": extra,
            "ports": ports,
            "reserved_area": mine_trunk.tap_area([e for e in hardware if e is not tap["splitter"]]),
            "bank": deepcopy(bank),
            "entity_units": [],
            "phase": "build",
        }
        if self.purpose:
            defense = self.perimeter.prepare_coverage(obs, supply_id, [bank["reserved_area"]])
            if not defense.get("ok"):
                return {"ok": False, "reason": "chemistry_coal_defense_preflight_failed", "evidence": defense}
            entry["defense_preview"] = defense
        return entry

    def _trunk_hardware(self, entry):
        return list(entry["tap"]["entities"]) + list((entry.get("hub") or {}).get("entities", []))

    def _trunk_block(self, entry):
        plan = mine_trunk.tap_plan(entry["tap"], (entry.get("hub") or {}).get("entities", []))
        plan["ports"] = list(entry["ports"].values())
        return "material_tap:" + entry["material_supply_id"], dict(
            plan, reserved_area=entry["reserved_area"], owner="dedicated_materials", trunk=entry["trunk"]
        )

    def _step_trunk(self, obs, parameters, entry):
        saved = self._outposts()
        bank = mine_trunk.find_bank(saved.get("banks"), entry["trunk"])
        if bank is None or not bank.get("ready"):
            return {"status": "blocked", "reason": "mine_trunk_patch_bank_missing", "trunk": entry["trunk"]}
        root = self.path.parent
        result = mine_trunk.record_tap(root, obs["world_id"], entry["tap"], block=self._trunk_block(entry))
        if result:
            return result
        if self.perimeter is not None:
            result = self.perimeter.ensure_coverage(obs, entry["material_supply_id"], [bank["reserved_area"]])
            if result is not None:
                return result
        # The owner's plan is republished before its trunk belt is mined.
        changed = mine_trunk.release_trunk_belts(root, obs["world_id"], entry["tap"])
        if "smelting_district" in changed and self.smelting is not None:
            self.smelting.state = json.loads((root / "smelting-district.json").read_text(encoding="utf-8"))
        if any(owner != "smelting_district" for owner in changed):
            self.outposts.state = self._outposts()
        hub = (entry.get("hub") or {}).get("entities", [])
        action = mine_trunk.cutover(self.builder, obs, entry["tap"], hub)
        if entry["tap"].get("built"):
            mine_trunk.record_tap(root, obs["world_id"], entry["tap"])
        self._save()
        if action is not None:
            return action
        entry["bank"] = deepcopy(bank)
        return self._finish(obs, parameters, entry, "material output tapped from the patch bank's trunk")

    def _finish(self, obs, parameters, entry, reason):
        """Trunk mode: proof, then the typed sources (the patch bank is not this supply's to register)."""
        proof = self.proof(obs, parameters)
        if not proof:
            return {
                "status": "waiting",
                "reason": "waiting for the trunk tap and both native collector lanes of the patch bank",
            }
        saved = (
            json.loads(self.sources_path.read_text(encoding="utf-8"))
            if self.sources_path.exists()
            else {"world_id": obs["world_id"], "catalog_fingerprint": self.catalog.fingerprint, "sources": {}}
        )
        if not self._valid(saved, obs["world_id"]):
            return {"status": "blocked", "reason": "material_source_registry_identity_changed"}
        for key, value in proof["sources"].items():
            old = saved["sources"].get(key)
            if old and (
                old.get("material_supply_id") != entry["material_supply_id"]
                or old.get("unit_number") != value["unit_number"]
            ):
                return {"status": "blocked", "reason": "published_material_output_identity_changed"}
            saved["sources"][key] = value
        _atomic_json(self.sources_path, saved)
        entry["phase"] = "verified"
        entry["proof"] = proof
        self._save()
        return {"status": "succeeded", "reason": reason, "evidence": proof}

    def _register(self, obs, entry, *, ready=False):
        if not self._entry_valid(entry, obs["world_id"], entry["resource"]):
            return {"status": "blocked", "reason": "material_supply_purpose_or_identity_changed"}
        saved = self._outposts()
        if not self._valid(saved, obs["world_id"]):
            return {"status": "blocked", "reason": "material_outposts_world_or_catalog_changed"}
        matches = [b for b in saved.get("banks", []) if b.get("material_supply_id") == entry["material_supply_id"]]
        if len(matches) > 1 or (
            matches
            and (not _same_plan(matches[0], entry["bank"]) or matches[0].get("material_supply_purpose") != self.purpose)
        ):
            return {"status": "blocked", "reason": "registered_material_bank_changed"}
        bank = deepcopy(entry["bank"])
        bank["ready"] = ready
        bank["material_entity_units"] = deepcopy(entry["entity_units"])
        if matches:
            saved["banks"][saved["banks"].index(matches[0])] = bank
        else:
            saved.setdefault("banks", []).append(bank)
        saved.setdefault("catalog_fingerprint", self.catalog.fingerprint)
        self.outposts.state = saved
        _atomic_json(self.outposts.path, saved)
        result = self.outposts.publish_reservations(obs)
        if result:
            return result
        return None

    def _bind(self, obs, entry):
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        bound = {(e["name"], *xy(e)): e for e in entry["entity_units"]}
        owned = self._trunk_hardware(entry) if entry.get("mode") == TRUNK_MODE else entry["bank"]["plan"]["entities"]
        for spec in owned:
            if spec["name"] == "small-electric-pole" and not self.purpose:
                continue
            key = spec["name"], *xy(spec)
            live, previous = actual.get(key), bound.get(key)
            if previous and (not live or live.get("unit_number") != previous["unit_number"]):
                return {
                    "status": "blocked",
                    "reason": "dedicated_material_entity_identity_changed",
                    "position": spec["position"],
                }
            if live:
                if live.get("direction", 0) != spec.get("direction", 0) or type(live.get("unit_number")) is not int:
                    return {"status": "blocked", "reason": "dedicated_material_entity_configuration_changed"}
                if not previous:
                    entry["entity_units"].append({k: live[k] for k in ("name", "position", "unit_number")})
        return None

    def step(self, obs, parameters):
        validate_material(parameters)
        if self.purpose and parameters["resource"] != "coal":
            return {"status": "blocked", "reason": "chemistry_material_supply_requires_coal"}
        if self.purpose and self.perimeter is None:
            return {"status": "blocked", "reason": "chemistry_material_supply_requires_perimeter"}
        if self.state and not self._valid(self.state, obs["world_id"]):
            return {"status": "blocked", "reason": "material_supply_world_or_catalog_changed"}
        if self.state and not self._checkpoint_valid(self.state, obs["world_id"]):
            return {"status": "blocked", "reason": "material_supply_purpose_or_identity_changed"}
        if not self.state:
            self.state = {"world_id": obs["world_id"], "catalog_fingerprint": self.catalog.fingerprint, "supplies": {}}
            if self.purpose:
                self.state["purpose"] = self.purpose
        resource = parameters["resource"]
        if resource not in self.state["supplies"]:
            prepared = self.prepare(obs, parameters)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state["supplies"][resource] = prepared
            self._save()
        entry = self.state["supplies"][resource]
        result = self._bind(obs, entry)
        if result:
            return result
        self._save()
        if entry.get("mode") == TRUNK_MODE:
            return self._step_trunk(obs, parameters, entry)
        result = self._register(obs, entry, ready=entry["phase"] == "verified")
        if result:
            return result
        from .mining_layout_policy import validate_retained_units

        retained = validate_retained_units(obs, entry["bank"])
        if not retained.get("ok"):
            return dict(retained, status="blocked")
        if self.perimeter is not None:
            result = self.perimeter.ensure_coverage(obs, entry["material_supply_id"], [entry["bank"]["reserved_area"]])
            if result is not None:
                return result
        result = self.builder.ensure_plan(obs, entry["bank"]["plan"])
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs, parameters)
        if not proof:
            return {
                "status": "waiting",
                "reason": "waiting for independent powered material drill rows and both native collector lanes",
            }
        result = self._register(obs, entry, ready=True)
        if result:
            return result
        saved = (
            json.loads(self.sources_path.read_text(encoding="utf-8"))
            if self.sources_path.exists()
            else {"world_id": obs["world_id"], "catalog_fingerprint": self.catalog.fingerprint, "sources": {}}
        )
        if not self._valid(saved, obs["world_id"]):
            return {"status": "blocked", "reason": "material_source_registry_identity_changed"}
        for key, value in proof["sources"].items():
            old = saved["sources"].get(key)
            if old and (
                old.get("material_supply_id") != entry["material_supply_id"]
                or old.get("unit_number") != value["unit_number"]
            ):
                return {"status": "blocked", "reason": "published_material_output_identity_changed"}
            saved["sources"][key] = value
        _atomic_json(self.sources_path, saved)
        entry["phase"] = "verified"
        entry["proof"] = proof
        self._save()
        return {
            "status": "succeeded",
            "reason": "independent normally built material bank publishes proven typed outputs",
            "evidence": proof,
        }

    def proof(self, obs, parameters):
        validate_material(parameters)
        if self.purpose and self.perimeter is None:
            return None
        if not self.state or not self._checkpoint_valid(self.state, obs["world_id"]):
            return None
        entry = self.state.get("supplies", {}).get(parameters["resource"])
        if not entry or self._bind(obs, entry):
            return None
        defense = None
        if self.perimeter is not None:
            defense = self.perimeter.coverage_proof(obs, entry["material_supply_id"])
            if defense is None:
                return None
        bank = entry["bank"]
        trunk = entry.get("mode") == TRUNK_MODE
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        if trunk:
            # The patch bank as saved now (it may have grown in place since the tap was planned).
            current = mine_trunk.find_bank(self._outposts().get("banks"), entry["trunk"])
            hub = (entry.get("hub") or {}).get("entities", [])
            if current is None or not mine_trunk.tap_standing(obs, entry["tap"], hub):
                return None
            bank = current
        elif any((e["name"], *xy(e)) not in actual for e in bank["plan"]["entities"]):
            return None
        native = self.outposts.bank_proof(obs, bank)
        lanes = (native or {}).get("native_producer_lanes") or {}
        item = parameters["resource"]
        if not lanes.get("ok") or any(set(lanes.get(side, {})) - {item} for side in ("left", "right")):
            return None
        drill_units = native["drill_units"]
        if all(lanes.get(side, {}).get(item, 0) > 0 for side in ("left", "right")):
            entry["lane_witness"] = {"tick": obs["tick"], "drill_units": drill_units}
        witness = entry.get("lane_witness")
        if not witness or witness["drill_units"] != drill_units or not 0 <= obs["tick"] - witness["tick"] <= 600:
            return None
        if trunk:
            ports = entry["ports"]
        else:
            ports = {"stone": bank["plan"]["ports"][0]} if item == "stone" else bank["material_distribution"]["ports"]
        if item == "coal":
            spec = entry["hub"]["splitter"] if trunk else bank["material_distribution"]["splitter"]
            splitter = actual.get(("splitter", *xy(spec)))
            if not splitter or any(
                splitter.get(k) != "none" for k in ("splitter_input_priority", "splitter_output_priority")
            ):
                return None
        sources = {}
        for key, spec in ports.items():
            if self.purpose and key != "plastic-coal":
                continue
            live = actual.get(("transport-belt", *xy(spec)))
            if not live or live.get("direction") != spec["facing"]:
                return None
            sources[key] = dict(
                spec,
                name="transport-belt",
                unit_number=live["unit_number"],
                world_id=obs["world_id"],
                material_supply_id=entry["material_supply_id"],
                nominal_shared_rate_per_minute=bank["nominal_rate_per_minute"],
                capacity_scope="shared bank capacity; separate outlets do not multiply production",
            )
        if self.purpose:
            checked = self.game.query(
                """local q=helpers.json_to_table("""
                + json.dumps(json.dumps(sources["plastic-coal"], separators=(",", ":")))
                + """);local e=target(q.position,"transport-belt")
if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.direction~=q.facing or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="chemistry_coal_output_identity_changed"}end
local count=0;for lane=1,2 do for _,row in pairs(e.get_transport_line(lane).get_contents())do
 if row.name~="coal"or row.quality~="normal"then return {ok=false,reason="chemistry_coal_output_contaminated"}end;count=count+row.count end end
return {ok=count>0,coal=count,tick=game.tick}"""
            )
            if not checked.get("ok"):
                return None
        return {
            "tick": obs["tick"],
            "resource": item,
            "material_supply_id": entry["material_supply_id"],
            "sources": sources,
            "native_bank": native,
            "both_lane_witness": deepcopy(witness),
            "defense_coverage": defense,
            "scope": "normal independent extraction and typed output commissioning; nominal rates are not measured throughput",
        }
