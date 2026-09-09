"""Resource-backed steam capacity, self-fueling coal feeds, and depletion repair."""
from __future__ import annotations

from copy import deepcopy
from collections import Counter, deque
import json
import math
from pathlib import Path
from typing import Any

from .deterministic_state import _atomic_json
from .factory_templates import build_template, DIRECTIONS


def _report(status: str, reason: str, **evidence: Any) -> dict:
    return {"status": status, "reason": reason, "evidence": evidence}


def _ready(result: dict) -> bool:
    return result.get("status") == "succeeded" and "type" not in result


class EnergyExpansion:
    """Call before starter power verification once it has previously operated.

    ``None`` yields to ordinary factory work. Every mutation is an ordinary
    builder/material action; new drills receive one bounded startup fuel seed.
    """
    def __init__(self, game: Any, bootstrap: Any, builder: Any, factory: Any, catalog: Any):
        self.game, self.bootstrap, self.builder, self.factory, self.catalog = game, bootstrap, builder, factory, catalog
        self._fingerprint = getattr(catalog, "fingerprint", None)
        self.path = Path(game.cfg.runtime_dir) / "energy-expansion.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if self.state and self.state.get("schema_version") != 1:
            raise ValueError("unsupported energy checkpoint")

    def _save(self) -> None:
        _atomic_json(self.path, self.state)

    def _sync(self, obs: dict) -> bool:
        self.builder._sync(obs)
        self.factory._sync(obs)
        if not self.builder.state.get("power_plan") or not self.builder.state.get("coal_plan"):
            return False
        if self.state.get("world_id") != obs.get("world_id"):
            if "power_sample_tick" not in self.builder.state and not self.builder.state.get("power_verified_once"):
                return False
            self.state = {"schema_version": 1, "world_id": obs["world_id"],
                          "catalog_fingerprint": self._fingerprint,
                          "banks": [deepcopy(self.builder.state["power_plan"])],
                          "feeds": [{"plan": deepcopy(self.builder.state["coal_plan"]), "bank": 0,
                                     "primary": True, "seeded": True, "complete": True}], "power_links": {}}
            self.builder.state["seeds"].setdefault("energy:feed:0", {"observed": True, "attempts": 0})
            self.builder._save()
            self._save()
        tick = int(obs.get("tick", 0))
        changed = ("catalog_fingerprint" in self.state and self.state["catalog_fingerprint"] != self._fingerprint)
        rollback = tick < int(self.state.get("last_tick", 0))
        if changed or rollback:
            self.state.pop("last_evidence", None)
            for feed in self.state["feeds"]:
                feed["complete"] = False
            self.state["reconciliation"] = "catalog_changed" if changed else "world_rolled_back"
        # The builder clears transient seed state on rollback. Preserve every
        # fuel insertion already observed by this durable controller, so an old
        # cold drill receives no repeated handfeed. It can retire and be replaced.
        for index, feed in enumerate(self.state["feeds"]):
            if feed.get("seeded"):
                self.builder.state["seeds"].setdefault(f"energy:feed:{index}", {"observed": True, "attempts": 0})
        self.builder._save()
        self.state["catalog_fingerprint"] = self._fingerprint
        self.state["last_tick"] = tick
        self._save()
        return True

    def _managed(self, entity: dict) -> None:
        key = self.factory._entity_key(entity)
        owned = self.factory.state.setdefault("automated_burners", [])
        if key not in owned:
            owned.append(key)
            self.factory._save()

    def evidence(self, obs: dict) -> dict:
        """Read demand, connected generation and actual remaining coal under drills."""
        pole = next(e for e in self.state["banks"][0]["entities"] if e["name"] == "small-electric-pole")
        payload = json.dumps(json.dumps({"pole": pole, "feeds": self.state["feeds"],
                                         "banks": self.state["banks"]}, separators=(",", ":")))
        measured = self.game.query('''
local args=helpers.json_to_table(''' + payload + ''');local pole=target(args.pole.position,args.pole.name)
if not pole then return {ok=false,reason="primary generator pole missing"} end
local network=pole.electric_network_id;local stats=pole.electric_network_statistics
local names={};local demand=0;local consumers={};local connected_engines=0
for _,e in pairs(s.find_entities_filtered{force=f}) do
 local ok,id=pcall(function() return e.electric_network_id end)
 if ok and id==network then
  names[e.name]=true
  if e.type=="generator" then connected_engines=connected_engines+1 end
  local nominal=0
  if e.type=="electric-energy-interface" then nominal=e.power_usage*60/1000 end
  consumers[#consumers+1]={name=e.name,nominal_kw=nominal}
 end
end
for name in pairs(names) do demand=demand+stats.get_flow_count{name=name,category="input",precision_index=defines.flow_precision_index.five_seconds}*60/1000 end
local feeds={};local mining_time=prototypes.entity.coal.mineable_properties.mining_time
for index,feed in ipairs(args.feeds) do
 local row=feed.plan.drill;local drill=target(row.position,row.name);local coal=0
 local radius=prototypes.entity[row.name].mining_drill_radius
 for _,r in pairs(s.find_entities_filtered{area={{row.position.x-radius,row.position.y-radius},{row.position.x+radius,row.position.y+radius}},name="coal"}) do coal=coal+r.amount end
 local fuel=0;local belt_coal=0
 if drill and drill.burner then fuel=drill.burner.remaining_burning_fuel+drill.burner.inventory.get_item_count("coal")*prototypes.item.coal.fuel_value end
 for _,e in ipairs(feed.plan.entities) do
  if e.name=="transport-belt" then local belt=target(e.position,e.name)
   if belt then for lane=1,2 do belt_coal=belt_coal+belt.get_transport_line(lane).get_item_count("coal") end end
  end
 end
 feeds[index]={remaining=coal,fuel=fuel,belt_coal=belt_coal,gross_coal_per_minute=prototypes.entity[row.name].mining_speed*60/mining_time}
end
local banks={}
for index,bank in ipairs(args.banks) do
 local water=0;local steam=0;local fuel=0
 for _,row in ipairs(bank.entities) do
  if row.name=="boiler" or row.name=="steam-engine" then local e=target(row.position,row.name)
   if e then local fluids=e.get_fluid_contents();water=water+(fluids.water or 0);steam=steam+(fluids.steam or 0)
    if e.burner then fuel=fuel+e.burner.remaining_burning_fuel+e.burner.inventory.get_item_count("coal")*prototypes.item.coal.fuel_value end
   end
  end
 end
 banks[index]={water=water,steam=steam,fuel=fuel}
end
return {ok=true,network_id=network,demand_kw=demand,consumers=consumers,feeds=feeds,connected_engines=connected_engines,
 banks=banks,generation_kw=stats.get_flow_count{name="steam-engine",category="output",precision_index=defines.flow_precision_index.five_seconds}*60/1000,tick=game.tick}
''')
        if not measured.get("ok"):
            return measured
        nominal = 0.0
        for row in measured.get("consumers", []):
            prototype = self.catalog.entities.get(row["name"], {})
            # Interfaces can advertise infinite prototype limits; their actual
            # configurable load is the only meaningful demand (also used by QA).
            usage = (float(prototype.get("energy_usage", 0)) * 60 / 1000
                     if prototype.get("electric") and row["name"] != "electric-energy-interface" else 0)
            demand = max(float(row.get("nominal_kw", 0)), usage)
            if not math.isfinite(demand):
                return {"ok": False, "reason": "consumer has unsupported nonfinite power demand", "entity": row["name"]}
            nominal += demand
        measured["nominal_demand_kw"] = nominal
        measured["target_kw"] = max(nominal, float(measured.get("demand_kw", 0))) * 1.2
        if self.state.get("coal_links"):
            measured["coal_routes"] = self._coal_routes(obs)
            if measured["coal_routes"] is None:
                return {"ok": False, "reason": "owned coal connectivity proof is unavailable"}
            measured["coal_route_rate_kind"] = "nominal_single_item_geometry_model"
        return measured

    def capacity(self, evidence: dict) -> dict:
        fuel_joules = float(self.catalog.items["coal"]["fuel_value"])
        engine_kw = float(self.catalog.entities["steam-engine"]["energy_production"]) * 60 / 1000
        drill_kw = float(self.catalog.entities["burner-mining-drill"]["energy_usage"]) * 60 / 1000
        inserter_kw = float(self.catalog.entities["burner-inserter"]["energy_usage"]) * 60 / 1000
        if fuel_joules <= 0 or engine_kw <= 0:
            raise ValueError("live fuel/generation capacity is unavailable")
        per_bank = [0.0 for _ in self.state["banks"]]
        for feed, row in zip(self.state["feeds"], evidence["feeds"]):
            if row["remaining"] <= 0 or row["fuel"] <= 0 or not feed.get("complete") or feed.get("retired"):
                continue
            # Conservatively charge both drill and self-feeding inserter at their
            # prototype maximum consumption, even when the inserter is idle.
            net = max(0, row["gross_coal_per_minute"] * fuel_joules / 60000 - drill_kw - inserter_kw)
            per_bank[feed["bank"]] += net
        per_bank = [max(0, min(2 * engine_kw, coal_kw - inserter_kw * (2 if index else 1)))
                    for index, coal_kw in enumerate(per_bank)]
        if "coal_routes" in evidence:
            # A feed is a single source even when its owned trunk reaches several
            # banks. Charge each distinct downstream burner once, then consume
            # source and shared transit budgets while filling banks in order.
            routes = evidence["coal_routes"]
            shared_banks = {0} | {int(key) for key in self.state.get("coal_links", {})}
            sources = {}
            drills = set()
            for index, (feed, row) in enumerate(zip(self.state["feeds"], evidence["feeds"])):
                drill = feed["plan"]["drill"]
                identity = drill["name"], drill["position"]["x"], drill["position"]["y"]
                if (row["remaining"] > 0 and row["fuel"] > 0 and feed.get("complete")
                        and not feed.get("retired") and routes.get(str(index)) and identity not in drills):
                    if feed["bank"] not in shared_banks:
                        continue
                    drills.add(identity)
                    sources[str(index)] = max(0, row["gross_coal_per_minute"] * fuel_joules / 60000
                                              - drill_kw - inserter_kw)
            transit = {str(row["unit_number"]): row for index in sources
                       for path in routes[index].values() for row in path}
            # A transit edge only carries fuel for burners downstream of it.
            # Union all observed path suffixes: reserve each possible downstream
            # burner once, including the edge itself, without charging other banks.
            downstream = {key: set() for key in transit}
            for source in sources:
                for path in routes[source].values():
                    burners = set()
                    for row in reversed(path):
                        key = str(row["unit_number"])
                        if row["name"] == "burner-inserter":
                            burners.add(key)
                        downstream[key].update(burners)
            budget = {key: max(0, row["coal_per_minute"] * fuel_joules / 60000
                               - len(downstream[key]) * inserter_kw)
                      for key, row in transit.items()}
            for arm, row in transit.items():
                if row["name"] != "burner-inserter":
                    continue
                remaining_loss = inserter_kw
                for source in sources:
                    if any(str(e["unit_number"]) == arm for path in routes[source].values() for e in path):
                        charge = min(sources[source], remaining_loss)
                        sources[source] -= charge
                        remaining_loss -= charge
                if remaining_loss > .01:
                    # A disconnected arm cannot burn coal from another trunk.
                    return {"bank_kw": 2 * engine_kw, "fuel_backed_kw": [0.0 for _ in per_bank],
                            "total_kw": 0.0, "target_kw": float(evidence["target_kw"])}
            per_bank = [0.0 if bank in shared_banks else value for bank, value in enumerate(per_bank)]
            for bank in sorted(shared_banks):
                for source in sources:
                    path = routes[source].get(str(bank))
                    if not path:
                        continue
                    keys = {str(row["unit_number"]) for row in path}
                    amount = max(0, min(2 * engine_kw - per_bank[bank], sources[source],
                                        *(budget[key] for key in keys)))
                    sources[source] -= amount
                    per_bank[bank] += amount
                    for key in keys:
                        budget[key] -= amount
        return {"bank_kw": 2 * engine_kw, "fuel_backed_kw": per_bank, "total_kw": sum(per_bank),
                "target_kw": float(evidence["target_kw"])}

    def _coal_plans(self) -> list[dict]:
        return (self.state["banks"] + [feed["plan"] for feed in self.state["feeds"]]
                + list(self.state.get("coal_links", {}).values()))

    def _coal_routes(self, obs: dict) -> dict | None:
        """Reobserve exact owned identities, power and actual arm endpoints."""
        if obs.get("world_id") != self.state.get("world_id"):
            return None
        wanted, conflicts = {}, set()
        actual = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in obs.get("entities", [])}
        for plan in self._coal_plans():
            for row in plan["entities"]:
                if row["name"] not in {"transport-belt", "burner-inserter", "inserter", "fast-inserter", "long-handed-inserter", "steam-engine", "burner-mining-drill", "boiler"}:
                    continue
                key = row["name"], row["position"]["x"], row["position"]["y"]
                if key in wanted and wanted[key].get("direction", 0) != row.get("direction", 0):
                    conflicts.add(key)
                wanted[key] = row
        rows = []
        for key, row in wanted.items():
            found = actual.get(key, {})
            if (key not in conflicts and type(found.get("unit_number")) is int and found["unit_number"] > 0
                    and (found.get("direction", 0) == row.get("direction", 0)
                         or (row["name"] == "steam-engine" and found.get("direction", 0) == (row.get("direction", 0) + 8) % 16))):
                rows.append({**row, "direction": found.get("direction", 0), "unit_number": found["unit_number"]})
        pole = next(e for e in self.state["banks"][0]["entities"] if e["name"] == "small-electric-pole")
        payload = json.dumps(json.dumps({"world": obs["world_id"], "rows": rows, "pole": pole}, separators=(",", ":")))
        proof = self.game.query('''
--[[ owned_coal_transit: inert identity, direction, material and power proof. ]]
local x=helpers.json_to_table(''' + payload + ''');local pole=target(x.pole.position,x.pole.name)
if not d or d.world_id~=x.world or not pole then return {ok=false} end
local network=pole.electric_network_id;local rows={}
for _,row in ipairs(x.rows) do
 local e=target(row.position,row.name)
 if e and e.force==f and e.unit_number==row.unit_number and e.direction==row.direction then
  local good=true;local rate=0;local pickup=nil;local drop=nil;local outputs=nil;local receiver=nil
  if e.type=="transport-belt" then
   rate=e.prototype.belt_speed*4*3600 --[[ one lane, even on a two-lane belt ]]
   outputs={};for _,other in pairs(e.belt_neighbours.outputs) do outputs[#outputs+1]=pos(other.position) end
   for lane=1,2 do for _,item in pairs(e.get_transport_line(lane).get_contents()) do
    if item.name~="coal" and item.count>0 then good=false end
   end end
  elseif e.type=="inserter" then
   pickup=pos(e.pickup_position);drop=pos(e.drop_position)
   local receiving=s.find_entities_filtered{position=drop,name="boiler",force=f}
   if #receiving==1 then receiver=receiving[1].unit_number end
   local rotation=e.prototype.get_inserter_rotation_speed("normal");local extension=e.prototype.get_inserter_extension_speed("normal")
   if not rotation or rotation<=0 or not extension or extension<=0 then good=false
   else
    local function radius(p) return math.sqrt((p.x-e.position.x)^2+(p.y-e.position.y)^2) end
    rate=3600/(1/rotation+2*math.abs(radius(drop)-radius(pickup))/extension)
   end --[[ nominal one-item cycle; serialize the full turn and radial movement ]]
   if e.burner then good=good and (e.burner.remaining_burning_fuel+e.burner.inventory.get_item_count("coal")*prototypes.item.coal.fuel_value)>0
   else good=good and e.electric_network_id==network and e.energy>0 end
   if e.held_stack.valid_for_read and e.held_stack.name~="coal" then good=false end
  elseif e.type=="mining-drill" then drop=pos(e.drop_position)
  elseif e.type=="generator" then good=e.electric_network_id==network end
  if good then rows[#rows+1]={name=row.name,position=row.position,direction=row.direction,unit_number=row.unit_number,
    coal_per_minute=rate,pickup_position=pickup,drop_position=drop,belt_outputs=outputs,boiler_unit=receiver} end
 end
end
return {ok=true,world_id=d.world_id,rows=rows}
''')
        if not proof.get("ok") or proof.get("world_id") != obs["world_id"]:
            return None
        verified = {**obs, "entities": proof.get("rows", [])}
        for link in self.state.get("coal_links", {}).values():
            source = link.get("source_port", {})
            found = next((e for e in verified["entities"] if e["name"] == "transport-belt"
                          and e["position"] == source.get("position")), {})
            if (not source.get("unit_number") or found.get("unit_number") != source["unit_number"]
                    or found.get("direction") != source.get("facing")):
                arms = {(e["name"], e["position"]["x"], e["position"]["y"]) for e in link["entities"]
                        if "inserter" in e["name"]}
                verified["entities"] = [e for e in verified["entities"]
                    if (e["name"], e["position"]["x"], e["position"]["y"]) not in arms]
        self._coal_observation = verified
        intact, _ = self._coal_transit(verified)
        live = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in verified["entities"]}
        routes = {}
        for bank_index, bank in enumerate(self.state["banks"]):
            if bank_index and str(bank_index) not in self.state.get("coal_links", {}):
                continue
            link = self.state.get("coal_links", {}).get(str(bank_index))
            if link:
                source = link.get("source_port", {})
                p = source.get("position", {})
                observed_source = live.get(("transport-belt", p.get("x"), p.get("y")), {})
                if (not source.get("unit_number") or observed_source.get("unit_number") != source["unit_number"]
                        or observed_source.get("direction") != source.get("facing")):
                    continue
            engines = [e for e in bank["entities"] if e["name"] == "steam-engine"]
            if len(engines) != 2 or any((e["name"], e["position"]["x"], e["position"]["y"]) not in live for e in engines):
                continue
            arms = [live.get((e["name"], e["position"]["x"], e["position"]["y"]))
                    for e in bank["entities"] if e["name"] == "burner-inserter"]
            if len(arms) != 1 or not arms[0] or not arms[0].get("pickup_position"):
                continue
            arm = arms[0]
            boilers = [live.get((e["name"], e["position"]["x"], e["position"]["y"]), {})
                       for e in bank["entities"] if e["name"] == "boiler"]
            if len(boilers) != 1 or not arm.get("boiler_unit") or boilers[0].get("unit_number") != arm["boiler_unit"]:
                continue
            intake = tuple(math.floor(arm["pickup_position"][axis]) + .5 for axis in ("x", "y"))
            for feed_index, feed in enumerate(self.state["feeds"]):
                drill = feed["plan"]["drill"]
                observed_drill = live.get((drill["name"], drill["position"]["x"], drill["position"]["y"]), {})
                drop = observed_drill.get("drop_position")
                if not drop:
                    continue
                port = next((p for p in feed["plan"]["ports"] if p.get("item") == "coal" and p.get("direction") == "output"), None)
                start = {axis: math.floor(drop[axis]) + .5 for axis in ("x", "y")}
                prefix = self._coal_tail(start, intact, {(port["position"]["x"], port["position"]["y"])}) if port else None
                if not prefix:
                    continue
                path = self._coal_tail(port["position"], intact, {intake}) if port else None
                if path:
                    routes.setdefault(str(feed_index), {})[str(bank_index)] = [
                        live[(e["name"], e["position"]["x"], e["position"]["y"])] for e in prefix[:-1] + path] + [arm]
        return routes

    def _coal_transit(self, obs: dict, bank_index: int | None = None) -> tuple[dict, set]:
        """Only observed, consistently reserved belts can carry a new coal join."""
        reserved, conflicts = {}, set()
        for plan in self._coal_plans():
            for entity in plan["entities"]:
                if entity["name"] != "transport-belt":
                    continue
                p = entity["position"]
                key = p["x"], p["y"]
                if key in reserved and reserved[key].get("direction", 0) != entity.get("direction", 0):
                    conflicts.add(key)
                reserved[key] = entity
        observed = {(e["position"]["x"], e["position"]["y"]): e for e in obs.get("entities", [])
                    if e["name"] == "transport-belt"}
        intact = {key: row for key, row in reserved.items() if key not in conflicts and key in observed
                  and row.get("direction", 0) == observed[key].get("direction", 0)}
        for key in intact:
            if "belt_outputs" in observed[key]:
                intact[key] = {**intact[key], "_coal_outputs": observed[key]["belt_outputs"]}
        arms = {(e["name"], e["position"]["x"], e["position"]["y"]): e for plan in self._coal_plans()
                for e in plan["entities"] if e["name"] in {"burner-inserter", "inserter", "fast-inserter", "long-handed-inserter"}}
        for actual in obs.get("entities", []):
            identity = actual["name"], actual["position"]["x"], actual["position"]["y"]
            arm = arms.get(identity)
            if not arm or actual.get("direction", 0) != arm.get("direction", 0) or not actual.get("unit_number"):
                continue
            # Only the inert live proof supplies endpoints and transfer limits.
            pickup, drop = actual.get("pickup_position"), actual.get("drop_position")
            if not pickup or not drop or actual.get("coal_per_minute", 0) <= 0:
                continue
            source = tuple(math.floor(pickup[axis]) + .5 for axis in ("x", "y"))
            target = tuple(math.floor(drop[axis]) + .5 for axis in ("x", "y"))
            if source in intact and target in intact:
                intact[source] = {**intact[source], "_coal_transfers":
                                  intact[source].get("_coal_transfers", []) + [(arm, target)]}
        # Existing feeds may join downstream of the declared first coal belt.
        # The bank owns and repairs its entire coal conveyor, so any belt along
        # that directed chain is a safe terminal for inherited feed ownership.
        intakes = set()
        banks = self.state["banks"] if bank_index is None else [self.state["banks"][bank_index]]
        for bank in banks:
            belts = {(e["position"]["x"], e["position"]["y"]): e for e in bank["entities"]
                     if e["name"] == "transport-belt"}
            for port in bank["ports"]:
                if port["kind"] != "item" or port["item"] != "coal":
                    continue
                key = port["position"]["x"], port["position"]["y"]
                while key in belts and key not in intakes:
                    intakes.add(key)
                    delta = DIRECTIONS.get(belts[key].get("direction", 0))
                    if delta is None:
                        break
                    key = key[0] + delta[0], key[1] + delta[1]
        return intact, intakes

    @staticmethod
    def _coal_tail(position: dict, intact: dict, intakes: set) -> list[dict] | None:
        key = position["x"], position["y"]
        queue, visited = deque([(key, [])]), set()
        while queue:
            key, path = queue.popleft()
            if key in visited:
                continue
            row = intact.get(key)
            if row is None:
                continue
            visited.add(key)
            path = path + [{k: v for k, v in row.items() if not k.startswith("_coal_")}]
            if key in intakes:
                return path
            delta = DIRECTIONS.get(row.get("direction", 0))
            if "_coal_outputs" in row:
                for p in row["_coal_outputs"]:
                    queue.append(((p["x"], p["y"]), path))
            elif delta is not None:
                queue.append(((key[0] + delta[0], key[1] + delta[1]), path))
            for arm, target in row.get("_coal_transfers", []):
                queue.append((target, path + [arm]))
        return None

    def _reserve_feed(self, obs: dict, bank_index: int, *, replacement: bool = False) -> dict:
        from .deterministic_layout import mining_clearances, mining_site_clear
        production_clearances = mining_clearances(self.factory)
        clearances = self.factory._port_clearances()
        clearance_entities = [{"name": "reserved-port-approach", "position": {"x": x, "y": y}} for x, y in clearances]
        occupied = self.builder._occupied_by_plan(self.factory._reserved() + obs.get("entities", [])) | clearances
        destination = next(p for p in self.state["banks"][bank_index]["ports"] if p["kind"] == "item" and p["item"] == "coal")
        destinations = [destination] + [
            {**destination, "position": e["position"], "facing": e.get("direction", 0)}
            for e in self.state["banks"][bank_index]["entities"]
            if e["name"] == "transport-belt" and e["position"] != destination["position"]]
        proof_obs = getattr(self, "_coal_observation", {})
        transit_obs = (proof_obs if proof_obs.get("world_id") == obs.get("world_id")
                       and proof_obs.get("tick") == obs.get("tick") else obs)
        intact, intakes = self._coal_transit(transit_obs, bank_index)
        for feed_index, feed in enumerate(self.state["feeds"]):
            if not feed.get("complete"):
                continue
            drill = feed["plan"]["drill"]["position"]
            owned_belts = {(e["position"]["x"], e["position"]["y"], e.get("direction", 0))
                           for e in feed["plan"]["entities"] if e["name"] == "transport-belt"}
            owner_key = f"energy:feed:{feed_index}"
            for entity in feed["plan"]["entities"]:
                p = entity["position"]
                if entity["name"] == "transport-belt" and max(abs(p["x"] - drill["x"]), abs(p["y"] - drill["y"])) > 3:
                    tail = self._coal_tail(p, intact, intakes)
                    if tail is not None:
                        destinations.append({**destination, "position": p, "facing": entity.get("direction", 0),
                                             "downstream": tail, "owned_plan_key": owner_key})
            # The declared output is already downstream of the drill's fuel
            # pickup. Its intact tail can accept coal even inside the old
            # three-tile exclusion, where a neighbouring conveyor may enclose it.
            for port in feed["plan"]["ports"]:
                if port.get("kind") != "item" or port.get("item") != "coal" or port.get("direction") != "output":
                    continue
                tail = self._coal_tail(port["position"], intact, intakes)
                if tail and tail[0].get("direction", 0) != port.get("facing"):
                    continue
                for offset, entity in enumerate(tail or []):
                    if entity["name"] != "transport-belt":
                        continue
                    identity = (entity["position"]["x"], entity["position"]["y"], entity.get("direction", 0))
                    destinations.append({**destination, "position": entity["position"],
                                         "facing": entity.get("direction", 0), "downstream": tail[offset:],
                                         **({"owned_plan_key": owner_key} if identity in owned_belts else {})})
        destinations = list({(p["position"]["x"], p["position"]["y"], p["facing"]): p
                             for p in destinations}.values())
        positions = self.builder.coal_sites()
        payload = json.dumps(json.dumps(positions, separators=(",", ":")))
        survey = self.game.query('''
local positions=helpers.json_to_table(''' + payload + ''');local rows={}
local radius=prototypes.entity["burner-mining-drill"].mining_drill_radius
for _,p in ipairs(positions) do
 local remaining=0
 for _,r in pairs(s.find_entities_filtered{area={{p.x-radius,p.y-radius},{p.x+radius,p.y+radius}},name="coal"}) do remaining=remaining+r.amount end
 if remaining>0 then rows[#rows+1]={position=p,remaining=remaining} end
end
return {ok=true,sites=rows}
''')
        if not survey.get("ok"):
            return _report("blocked", "cannot survey remaining coal for power expansion")
        # Prefer a useful remaining lifetime over the closest nearly exhausted
        # fringe tile. Beyond 1000 coal, proximity decides construction cost.
        sites = sorted(survey.get("sites", []), key=lambda row: (-min(1000, row["remaining"]),
                       math.dist((row["position"]["x"], row["position"]["y"]),
                                 (destination["position"]["x"], destination["position"]["y"]))))
        surveyed = 0
        bridge_attempts = 0
        drop_attempts = 0
        for row in sites:
            site = row["position"]
            plan = self.builder._coal_plan(site)
            if not mining_site_clear(self.builder, plan["entities"], production_clearances):
                continue
            if self.builder._occupied_by_plan(plan["entities"]) & occupied:
                continue
            if not self.builder.can_place(plan["entities"]).get("ok"):
                continue
            surveyed += 1
            if surveyed > 16:
                break
            route = None
            nearby = sorted(destinations, key=lambda p: math.dist((p["position"]["x"], p["position"]["y"]),
                                                                  (plan["ports"][0]["position"]["x"], plan["ports"][0]["position"]["y"])))
            for intake in nearby[:16]:
                trial = self.builder.route(plan["ports"][0]["position"], intake["position"], "transport-belt",
                                           self.factory._reserved() + plan["entities"] + clearance_entities,
                                           start_direction=plan["ports"][0]["facing"])
                if trial.get("ok"):
                    if len(trial["segments"]) > 1 and trial["segments"][-2]["direction"] == (intake["facing"] + 8) % 16:
                        continue
                    trial["segments"][-1]["direction"] = intake["facing"]
                    route = trial
                    break
            if route is None:
                # A dedicated coal tail may be enclosed even when a new paid
                # long arm can drop into it. Keep its observed facing and inherit
                # the already verified path to this bank, including later repair.
                for intake in [p for p in nearby if p.get("owned_plan_key") and p.get("downstream")][:4]:
                    if drop_attempts >= 4:
                        break
                    drop_attempts += 1
                    consumer = {key: value for key, value in intake.items()
                                if key not in {"downstream", "owned_plan_key"}}
                    trial = self.factory._consumer_drop_bridge_route(
                        obs, plan["ports"][0]["position"], consumer,
                        self.factory._reserved() + plan["entities"] + clearance_entities,
                        start_direction=plan["ports"][0]["facing"], owned_plan_key=intake["owned_plan_key"])
                    segments = trial.get("segments") or []
                    if (trial.get("ok") and segments and segments[-1].get("name") == "transport-belt"
                            and segments[-1].get("position") == intake["position"]
                            and segments[-1].get("direction") == intake["facing"]
                            and self.builder.can_place(plan["entities"] + segments).get("ok")):
                        route = trial
                        break
            if route is None:
                # Reuse the factory's ordinary long-arm crossing when a
                # reserved conveyor encloses an otherwise usable coal field.
                # Keep the expensive fallback bounded across the whole survey.
                for intake in nearby[:4]:
                    if bridge_attempts >= 4:
                        break
                    bridge_attempts += 1
                    trial = self.factory._belt_bridge_route(plan["ports"][0]["position"], intake["position"],
                        self.factory._reserved() + plan["entities"] + clearance_entities,
                        start_direction=plan["ports"][0]["facing"])
                    if trial.get("ok"):
                        if len(trial["segments"]) > 1 and trial["segments"][-2]["direction"] == (intake["facing"] + 8) % 16:
                            continue
                        trial["segments"][-1]["direction"] = intake["facing"]
                        route = trial
                        break
            if route is None:
                continue
            plan["entities"] += [{"name": "transport-belt", **segment} for segment in route["segments"]]
            if not mining_site_clear(self.builder, plan["entities"], production_clearances,
                                     existing=self.factory._reserved()):
                continue
            # The original drill may be depleted or retire later. Give the new
            # live feed ownership of the whole shared tail so ensure_plan repairs
            # a broken transit belt even when the old feed no longer runs.
            plan["entities"] += deepcopy(intake.get("downstream", []))
            plan["entities"] = list({(e["name"], e["position"]["x"], e["position"]["y"]): e for e in plan["entities"]}.values())
            plan["required_items"] = dict(Counter(e.get("item") or e["name"] for e in plan["entities"]))
            index = len(self.state["feeds"])
            registered = self.factory.register_plan(f"energy:feed:{index}", plan, obs)
            if not registered.get("ok"):
                continue
            record = {"plan": plan, "bank": bank_index, "primary": False, "replacement": replacement,
                      "seeded": False, "complete": False}
            self.state["feeds"].append(record)
            self._managed(plan["drill"])
            self._save()
            return _report("waiting", "reserved an additional self-fueling coal feed", feed=index, bank=bank_index)
        return _report("blocked", "no pure unoccupied coal site has a route to its boiler", bank=bank_index)

    def _inherit_feed_power(self, obs: dict, index: int) -> dict | None:
        """Keep a borrowed crossing repairable after its original drill retires."""
        plan = self.state["feeds"][index]["plan"]
        identity = lambda e: (e["name"], e["position"]["x"], e["position"]["y"], e.get("direction", 0))
        arms = {identity(e) for e in plan["entities"] if e["name"] == "long-handed-inserter"}
        if not arms:
            return None
        existing = {identity(e) for e in plan["entities"]}
        poles = {}
        for category in ("blocks", "links"):
            for owner in self.factory.state.get(category, {}).values():
                if not any(identity(e) in arms for e in owner.get("entities", [])):
                    continue
                for entity in owner["entities"]:
                    key = identity(entity)
                    if entity["name"] == "small-electric-pole" and key not in existing:
                        poles[key] = deepcopy(entity)
        if not poles:
            return None
        candidate = deepcopy(plan)
        candidate["entities"].extend(poles.values())
        candidate["required_items"] = dict(Counter(e.get("item") or e["name"] for e in candidate["entities"]))
        registered = self.factory.register_plan(f"energy:feed:{index}", candidate, obs)
        if not registered.get("ok"):
            return _report("blocked", registered.get("reason", "shared coal crossing power reservation failed"), feed=index)
        self.state["feeds"][index]["plan"] = registered
        self._save()
        return None

    def _ensure_feed(self, obs: dict, index: int, row: dict) -> dict | None:
        feed = self.state["feeds"][index]
        if feed.get("retired"):
            return None
        if feed.get("seeded") and row["fuel"] <= 0 and row["belt_coal"] <= 0:
            feed["retired"] = True
            self._save()
            return None
        if row["remaining"] <= 0:
            if not feed.get("depleted"):
                feed["depleted"] = True
                self._save()
            return None
        inherited = self._inherit_feed_power(obs, index)
        if inherited is not None:
            return inherited
        result = self.builder.ensure_plan(obs, feed["plan"])
        if not _ready(result):
            return result
        if any(e["name"] == "long-handed-inserter" for e in feed["plan"]["entities"]):
            result = self.factory.ensure_power_connection(obs, f"energy:feed:{index}", feed["plan"])
            if not _ready(result):
                return result
        seed = self.builder._seed(obs, f"energy:feed:{index}", feed["plan"]["drill"], 8)
        if seed:
            return seed
        if row["fuel"] > 0 and not feed.get("seeded"):
            feed["seeded"] = True
            self._save()
        if row["fuel"] <= 0 or row["belt_coal"] <= 0:
            if feed.get("complete"):
                return None
            return _report("waiting", "waiting for new self-fueling coal feed", feed=index, **row)
        if not feed.get("complete"):
            feed["complete"] = True
            feed["seeded"] = True
            if feed.get("replacement"):
                for other in self.state["feeds"]:
                    other["primary"] = False
                feed["primary"] = True
                self.builder.state["coal_plan"] = deepcopy(feed["plan"])
                self.builder.state["seeds"]["coal_drill"] = {"observed": True, "attempts": 0}
                self.builder._save()
            self._save()
        return None

    def _reserve_bank(self, obs: dict) -> dict:
        index = len(self.state["banks"])
        initial = self.state["banks"][0]
        reference = next(e["position"] for e in initial["entities"] if e["name"] == "boiler")
        plan = self.factory.reserve_site(build_template("steam_bank"), f"energy:bank:{index}", obs, reference=reference)
        if not plan.get("ok"):
            return _report("blocked", plan.get("reason", "no additional steam bank site"))
        self.state["banks"].append(plan)
        self._managed(next(e for e in plan["entities"] if e["name"] == "boiler"))
        self._save()
        return _report("waiting", "reserved additional steam capacity", bank=index)

    def _connect_bank_power(self, obs: dict, index: int) -> dict:
        # A new bank's poles already touch its own idle generators; merely asking
        # whether they touch ANY generator would falsely accept an isolated grid.
        source = next(e for e in self.state["banks"][0]["entities"] if e["name"] == "small-electric-pole")
        destination = next(e for e in self.state["banks"][index]["entities"] if e["name"] == "small-electric-pole")
        actual = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in obs.get("entities", [])}
        get = lambda e: actual.get((e["name"], e["position"]["x"], e["position"]["y"]), {}).get("electric_network_id")
        if get(source) is not None and get(source) == get(destination):
            return _report("succeeded", "additional bank shares the primary factory grid")
        key = str(index)
        if key not in self.state["power_links"]:
            route = self.factory._power_route(source["position"], destination["position"])
            if not route.get("ok"):
                return _report("blocked", route.get("reason", "steam bank cannot reach primary grid"))
            plan = {"ok": True, "entities": [{"name": "small-electric-pole", "position": p, "direction": 0} for p in route["path"]], "ports": []}
            registered = self.factory.register_plan(f"energy:power:{index}", plan, obs)
            if not registered.get("ok"):
                return _report("blocked", registered.get("reason", "power connection reservation failed"))
            self.state["power_links"][key] = plan
            self._save()
        built = self.builder.ensure_plan(obs, self.state["power_links"][key])
        return built if not _ready(built) else _report("waiting", "waiting for primary/additional grid connection")

    def _ensure_bank(self, obs: dict, index: int) -> dict | None:
        plan = self.state["banks"][index]
        result = self.builder.ensure_plan(obs, plan)
        if not _ready(result):
            return result
        if index == 0:
            return None
        result = self._connect_bank_power(obs, index)
        if not _ready(result):
            return result
        # Reserve the fuel approach before water routing. Otherwise a water line
        # can loop around the boiler and enclose its only coal access corridor.
        result = self._connect_bank_coal(obs, index)
        if not _ready(result):
            return result
        fluids = self.factory.fluids
        if fluids is None:
            return _report("blocked", "additional steam bank requires the fluid router")
        source = fluids.ensure_source(obs, "water")
        if not _ready(source):
            return source
        destination = next(p for p in plan["ports"] if p["kind"] == "fluid" and p["item"] == "water")
        result = fluids._connect_pipe(obs, source["evidence"]["ports"][0], destination, f"energy:water:{index}", plan)
        if not _ready(result):
            return result
        return None

    def _connect_bank_coal(self, obs: dict, index: int) -> dict:
        """Branch the shared power coal trunk with a self-fueled inserter.

        Separate parallel belts eventually enclose one another. A same-coal trunk
        plus independent boiler branches keeps each new drill near the coal field
        and each new bank supplied without crossing existing conveyor lines.
        """
        links = self.state.setdefault("coal_links", {})
        key = str(index)
        if key not in links:
            if not obs.get("world_id") or obs["world_id"] != self.state.get("world_id"):
                return _report("blocked", "coal branch world identity changed", bank=index)
            bank = self.state["banks"][index]
            destination = next(p for p in bank["ports"] if p["kind"] == "item" and p["item"] == "coal")
            intact, _ = self._coal_transit(obs)
            observed = {(e["position"]["x"], e["position"]["y"]): e for e in obs.get("entities", [])
                        if e["name"] == "transport-belt" and type(e.get("unit_number")) is int and e["unit_number"] > 0}
            candidates = {}
            for feed in self.state["feeds"]:
                if not feed.get("complete") or feed.get("depleted") or feed.get("retired"):
                    continue
                for e in feed["plan"]["entities"]:
                    point = e["position"]["x"], e["position"]["y"]
                    if e["name"] == "transport-belt" and point in intact and point in observed:
                        candidates[point] = observed[point]
            candidates = sorted(candidates.values(), key=lambda e: math.dist(
                (e["position"]["x"], e["position"]["y"]), (destination["position"]["x"], destination["position"]["y"])))
            clearances = self.factory._port_clearances()
            ddx, ddy = DIRECTIONS[destination["facing"]]
            clearances.discard((destination["position"]["x"] - ddx, destination["position"]["y"] - ddy))
            reserved = self.factory._reserved()
            obstacles = reserved + [{"name": "reserved-port-approach", "position": {"x": x, "y": y}} for x, y in clearances]
            occupied = self.builder._occupied_by_plan(reserved + obs.get("entities", [])) | clearances
            plan = None
            bridge_attempts = 0
            for belt in candidates[:64]:
                for side in ((belt["direction"] + 4) % 16, (belt["direction"] + 12) % 16):
                    dx, dy = DIRECTIONS[side]
                    p = belt["position"]
                    inserter = {"name": "burner-inserter", "position": {"x": p["x"] + dx, "y": p["y"] + dy}, "direction": (side + 8) % 16}
                    entry = {"name": "transport-belt", "position": {"x": p["x"] + 2 * dx, "y": p["y"] + 2 * dy}, "direction": side}
                    if self.builder._occupied_by_plan([inserter, entry]) & occupied:
                        continue
                    if not self.builder.can_place([inserter, entry]).get("ok"):
                        continue
                    route = self.builder.route(entry["position"], destination["position"], "transport-belt", obstacles + [inserter, entry],
                                               start_direction=side, end_direction=destination["facing"], margin=24)
                    # The old coal trunk may sit behind another material's
                    # conveyor. Reuse paid, powered crossings without joining
                    # or rotating that conveyor; bound the expensive fallback.
                    if (not route.get("ok") and bridge_attempts < 8
                            and route.get("reason") in {"no route within bounds", "route search budget exhausted"}):
                        bridge_attempts += 1
                        route = self.factory._belt_bridge_route(entry["position"], destination["position"], obstacles + [inserter, entry],
                                                                start_direction=side, end_direction=destination["facing"])
                    if not route.get("ok"):
                        continue
                    segments = route.get("segments") or []
                    if (not segments or segments[0].get("position") != entry["position"]
                            or segments[0].get("direction") != side
                            or segments[-1].get("position") != destination["position"]
                            or segments[-1].get("direction") != destination["facing"]):
                        continue
                    entities = [inserter] + [{"name": "transport-belt", **segment} for segment in segments]
                    if not self.builder.can_place(entities).get("ok"):
                        continue
                    payload = json.dumps(json.dumps({"world": obs["world_id"], "belt": {
                        field: belt[field] for field in ("position", "direction", "unit_number")}}, separators=(",", ":")))
                    proof = self.game.query('''
--[[ coal_bank_branch_source: verify the exact owned pickup before reservation. ]]
local x=helpers.json_to_table(''' + payload + ''');local e=target(x.belt.position,"transport-belt")
if not d or d.world_id~=x.world or not e or e.force~=f or e.unit_number~=x.belt.unit_number
 or e.direction~=x.belt.direction then return {ok=false,reason="coal branch pickup identity changed"} end
for lane=1,2 do for _,row in pairs(e.get_transport_line(lane).get_contents()) do
 if row.name~="coal" and row.count>0 then return {ok=false,reason="coal branch pickup carries another material"} end
end end
return {ok=true,coal_pickup_verified=true}
''')
                    if not proof.get("ok") or not proof.get("coal_pickup_verified"):
                        continue
                    candidate = {"ok": True, "entities": entities, "ports": [],
                                 "required_items": dict(Counter(e["name"] for e in entities)),
                                 "source_port": {"kind": "item", "item": "coal", "direction": "output",
                                                 "position": belt["position"], "facing": belt["direction"],
                                                 "unit_number": belt["unit_number"]},
                                 "consumer_port": deepcopy(destination)}
                    registered = self.factory.register_plan(f"energy:coal-bank:{index}", candidate, obs)
                    if registered.get("ok"):
                        plan = candidate
                        break
                if plan:
                    break
            if plan is None:
                return _report("blocked", "no self-fueling coal branch reaches the new bank", bank=index)
            links[key] = plan
            self._save()
        result = self.builder.ensure_plan(obs, links[key])
        if not _ready(result):
            return result
        if any(e["name"] == "long-handed-inserter" for e in links[key]["entities"]):
            return self.factory.ensure_power_connection(obs, f"energy:coal-bank:{index}", links[key])
        return result

    def next_action(self, obs: dict) -> dict | None:
        if not self._sync(obs):
            return None
        for index, feed in enumerate(self.state["feeds"]):
            if feed.get("depleted") or feed.get("retired"):
                continue
            registered = self.factory.register_plan(f"energy:feed:{index}", feed["plan"], obs)
            if not registered.get("ok"):
                return _report("blocked", registered.get("reason", "existing coal reservation conflicts"))
        for index, plan in enumerate(self.state["banks"]):
            registered = self.factory.register_plan(f"energy:bank:{index}", plan, obs)
            if not registered.get("ok"):
                return _report("blocked", registered.get("reason", "existing power reservation conflicts"))
            result = self._ensure_bank(obs, index)
            if result:
                return result
        evidence = self.evidence(obs)
        if not evidence.get("ok"):
            return _report("blocked", evidence.get("reason", "cannot inspect energy supply"))
        for index, row in enumerate(evidence["feeds"]):
            self._managed(self.state["feeds"][index]["plan"]["drill"])
            result = self._ensure_feed(obs, index, row)
            if result:
                return result
        primary = next((i for i, feed in enumerate(self.state["feeds"]) if feed.get("primary")), None)
        if (primary is None or evidence["feeds"][primary]["remaining"] <= 0
                or (evidence["feeds"][primary]["fuel"] <= 0 and evidence["feeds"][primary]["belt_coal"] <= 0)):
            pending = any(f.get("replacement") and not f.get("complete") and not f.get("depleted") and not f.get("retired") for f in self.state["feeds"])
            if not pending:
                return self._reserve_feed(obs, 0, replacement=True)
        capacity = self.capacity(evidence)
        self.state["last_evidence"] = {**evidence, **capacity}
        self._save()
        target = max(capacity["target_kw"], 1)
        # Finish coal for each constructed bank before adding another one.
        for index, supplied in enumerate(capacity["fuel_backed_kw"]):
            share = max(1, min(capacity["bank_kw"], max(0, target - index * capacity["bank_kw"])))
            if supplied + .01 < share:
                return self._reserve_feed(obs, index)
        if capacity["bank_kw"] * len(self.state["banks"]) + .01 < target:
            return self._reserve_bank(obs)
        for index, row in enumerate(evidence.get("banks", [])):
            if row["water"] <= 0 or row["steam"] <= 0 or row["fuel"] <= 0:
                return _report("waiting", "waiting for constructed bank water, coal and steam", bank=index, **row)
        return None
