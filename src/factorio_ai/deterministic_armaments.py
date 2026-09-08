"""Normal-material ammunition production and observed automatic turret supply."""
from __future__ import annotations

import json
from itertools import islice
from pathlib import Path
from typing import Any, Iterator

from .deterministic_state import _atomic_json
from .factory_templates import DIRECTIONS


MAX_INTAKE_CANDIDATES = 8 * 24  # Four sides, two offsets, at most 24 nearby poles.


def _report(status: str, reason: str, **evidence: Any) -> dict:
    return {"status": status, "reason": reason, "evidence": evidence}


def _ready(result: dict) -> bool:
    return result.get("status") == "succeeded" and "type" not in result


class Armaments:
    def __init__(self, game: Any, bootstrap: Any, builder: Any, factory: Any, catalog: Any, *, seed_limit: int = 10):
        if isinstance(seed_limit, bool) or not isinstance(seed_limit, int) or seed_limit < 0:
            raise ValueError("seed_limit must be a nonnegative integer")
        self.game, self.bootstrap, self.builder, self.factory, self.catalog = game, bootstrap, builder, factory, catalog
        self.seed_limit = seed_limit
        self.path = Path(game.cfg.runtime_dir) / "armaments.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if self.state and self.state.get("schema_version") != 1:
            raise ValueError("unsupported armaments checkpoint")

    @staticmethod
    def _key(entity: dict) -> str:
        position = entity["position"]
        return f'gun-turret:{position["x"]:g},{position["y"]:g}'

    def _save(self) -> None:
        _atomic_json(self.path, self.state)

    def _sync(self, obs: dict) -> None:
        if not obs.get("world_id"):
            raise ValueError("armaments observation requires world_id")
        if self.state.get("world_id") != obs["world_id"] or self.state.get("catalog_fingerprint") != self.catalog.fingerprint:
            self.state = {"schema_version": 1, "world_id": obs["world_id"], "catalog_fingerprint": self.catalog.fingerprint,
                          "turrets": {}, "last_tick": 0}
        tick = int(obs.get("tick", 0))
        if tick < self.state.get("last_tick", 0):
            for row in self.state["turrets"].values():
                row["owned"] = False
                row.pop("sample", None)
                # Issued seed actions stay debited across rollback. A checkpoint
                # rollback cannot authorize an unbounded new ammunition batch.
        self.state["last_tick"] = tick
        present = {self._key(e) for e in obs.get("entities", []) if e.get("name") == "gun-turret"}
        for key, row in self.state["turrets"].items():
            if key not in present:
                row["owned"] = False
        self._save()

    def manages_turret(self, entity: dict) -> bool:
        if entity.get("name") != "gun-turret":
            return False
        row = self.state.get("turrets", {}).get(self._key(entity))
        return bool(row and row.get("unit_number") == entity.get("unit_number"))

    def owns_automated_turret(self, entity: dict) -> bool:
        row = self.state.get("turrets", {}).get(self._key(entity)) if entity.get("name") == "gun-turret" else None
        return bool(row and row.get("owned") and row.get("unit_number") == entity.get("unit_number"))

    def _track(self, turret: dict) -> dict:
        key = self._key(turret)
        previous = self.state["turrets"].get(key)
        if previous is None or previous.get("unit_number") != turret.get("unit_number"):
            stock = int((turret.get("inventory") or {}).get("firearm-magazine", 0))
            self.state["turrets"][key] = {"unit_number": turret.get("unit_number"), "position": turret["position"],
                                         "seed_remaining": max(0, self.seed_limit - stock), "owned": False}
            self._save()
        return self.state["turrets"][key]

    def _seed(self, obs: dict, turret: dict, row: dict) -> dict | None:
        stock = int((turret.get("inventory") or {}).get("firearm-magazine", 0))
        if stock or row["seed_remaining"] <= 0:
            return None
        held = int((obs.get("inventory") or {}).get("firearm-magazine", 0))
        if not held:
            result = self.bootstrap.ensure_item(obs, "firearm-magazine", row["seed_remaining"])
            return result if not _ready(result) else None
        count = min(held, row["seed_remaining"])
        row["seed_remaining"] -= count
        row["last_seed_tick"] = int(obs.get("tick", 0))
        row.pop("sample", None)
        self._save()
        return {"type": "insert", "name": "gun-turret", "position": turret["position"], "item": "firearm-magazine",
                "inventory": "turret_ammo", "count": count, "reason": "bounded initial turret ammunition seed"}

    def _intake_candidates(self, turret: dict) -> Iterator[dict]:
        ranked, _ = self._intake_order(turret)
        yield from self._validated_intakes(plan for plan, _ in ranked)

    def _intake_order(self, turret: dict, previous: dict | None = None):
        from .deterministic_intake_order import order_intakes
        return order_intakes(self, turret, list(islice(self._intake_geometry(turret), MAX_INTAKE_CANDIDATES)), previous)

    def _validated_intakes(self, plans) -> Iterator[dict]:
        for plan in plans:
            port = plan["ports"][0]
            dx, dy = DIRECTIONS[port["facing"]]
            arrival = {"name": "transport-belt", "direction": port["facing"], "position": {
                "x": port["position"]["x"] - dx, "y": port["position"]["y"] - dy}}
            if self.builder.can_place([*plan["entities"], arrival]).get("ok"):
                yield plan

    def _intake_geometry(self, turret: dict) -> Iterator[dict]:
        from .deterministic_intake_order import reusable_poles
        from .deterministic_intake_receiver import receiver_identity
        center = turret["position"]
        key = "armaments:" + self._key(turret)
        reserved = self.factory._reserved(exclude=key)
        clearances = self.factory._port_clearances(exclude=key)
        occupied = self.builder._occupied_by_plan(reserved) | clearances
        shared = reusable_poles(self, turret)
        shared_obstacles = {}
        for outward in (12, 4, 0, 8):
            dx, dy = DIRECTIONS[outward]
            for tangent in (-.5, .5):
                position = {"x": center["x"] + dx * 1.5 - dy * tangent,
                            "y": center["y"] + dy * 1.5 + dx * tangent}
                inserter = {"name": "inserter", "position": position, "direction": outward}
                belts = [{"name": "transport-belt", "position": {"x": position["x"] + dx * step,
                                                                        "y": position["y"] + dy * step},
                          "direction": (outward + 8) % 16} for step in (1, 2)]
                approach = (belts[-1]["position"]["x"] + dx, belts[-1]["position"]["y"] + dy)
                if approach in occupied:
                    continue
                equipment = [inserter, *belts]
                for pole in self.factory._intake_poles(position, equipment):
                    point = pole["position"]["x"], pole["position"]["y"]
                    proof = shared.get(point)
                    obstacles = occupied
                    if proof and max(abs(pole["position"][axis] - position[axis]) for axis in ("x", "y")) <= proof["reach"]:
                        if point not in shared_obstacles:
                            shared_obstacles[point] = self.builder._occupied_by_plan([e for e in reserved
                                if not (e["name"] == "small-electric-pole" and e["position"] == pole["position"])]) | clearances
                        obstacles = shared_obstacles[point]
                        pole = {**pole, "_shared_power": proof}
                    entities = [*equipment, pole]
                    if self.builder._occupied_by_plan(entities) & obstacles:
                        continue
                    plan = {"ok": True, "entities": entities, "existing_receiver": receiver_identity(self, turret),
                            "ports": [{"kind": "item", "item": "firearm-magazine", "direction": "input",
                                                "position": belts[-1]["position"], "facing": (outward + 8) % 16}]}
                    yield plan

    def _intake_approach_clear(self, plan: dict) -> bool:
        port = (plan.get("ports") or [{}])[0]
        if port.get("facing") not in DIRECTIONS or not port.get("position"):
            return False
        dx, dy = DIRECTIONS[port["facing"]]
        arrival = {"name": "transport-belt", "direction": port["facing"], "position": {
            "x": port["position"]["x"] - dx, "y": port["position"]["y"] - dy}}
        return bool(self.builder.can_place([arrival]).get("ok"))

    def _intake_owner_matches(self, obs: dict, turret: dict, row: dict) -> bool:
        return bool(obs.get("world_id") and self.state.get("world_id") == obs["world_id"]
                    and self.factory.state.get("world_id") == obs["world_id"]
                    and self.state.get("catalog_fingerprint") == self.catalog.fingerprint
                    and self.factory.state.get("catalog_fingerprint") == self.catalog.fingerprint
                    and type(turret.get("unit_number")) is int and turret["unit_number"] > 0
                    and row.get("unit_number") == turret["unit_number"]
                    and any(e.get("unit_number") == turret["unit_number"] and e.get("name") == "gun-turret"
                            and e.get("position") == turret["position"] for e in obs.get("entities", [])))

    def _discard_unbuilt_intake(self, obs: dict, turret: dict, key: str, row: dict) -> bool:
        plan = row.get("plan")
        if (not plan or key in self.factory.state.get("links", {})
                or not self._intake_owner_matches(obs, turret, row)
                or self.factory.state.get("blocks", {}).get(key) != plan
                or self._intake_hardware_present(obs, plan)):
            return False
        # Release only this unbuilt reservation. Saving it first makes a crash
        # between checkpoints safely retry the still-recorded old candidate.
        self.factory.state["blocks"].pop(key)
        self.factory._save()
        row.pop("plan")
        row["owned"] = False
        row.pop("sample", None)
        row.pop("proof", None)
        self._save()
        return True

    def _intake_hardware_present(self, obs: dict, plan: dict) -> bool:
        from .deterministic_intake_order import inherited_pole
        return any(e.get("name") == planned["name"] and e.get("position") == planned["position"]
                   and not inherited_pole(self, obs, planned, e, plan.get("key"))
                   for planned in plan.get("entities", []) if planned["name"] != "gun-turret"
                   for e in obs.get("entities", []))

    def _ensure_intake(self, obs: dict, turret: dict, row: dict, source: dict) -> dict:
        key = "armaments:" + self._key(turret)
        if not self._intake_owner_matches(obs, turret, row):
            return _report("blocked", "ammunition intake world or turret identity changed")
        previous = row.get("plan")
        existing = self.factory.state.get("blocks", {}).get(key)
        from .deterministic_intake_receiver import receiver_identity, support_entities, verify_receiver
        proof_plan = previous or existing
        support_only = proof_plan is not None and not any(e["name"] == "gun-turret" for e in proof_plan["entities"])
        if ((previous is None or support_only or "existing_receiver" in (proof_plan or {}))
                and not verify_receiver(self, obs, turret, proof_plan if support_only or "existing_receiver" in (proof_plan or {}) else None)):
            return _report("blocked", "existing ammunition receiver identity or intake geometry changed")
        if (previous is None and existing is not None and existing.get("key") == key
                and key not in self.factory.state.get("links", {}) and not self._intake_hardware_present(obs, existing)):
            # Recover a crash after register_plan saved, before the armaments
            # record saved. Turret aim can rotate without changing the intake;
            # compare a copy while preserving every other field and saved plan.
            entities = support_entities(existing, turret)
            for candidate in islice(self._intake_candidates(turret), MAX_INTAKE_CANDIDATES):
                if (candidate.get("entities") == entities and candidate.get("ports") == existing.get("ports")
                        and candidate.get("existing_receiver") == receiver_identity(self, turret)
                        and (existing.get("existing_receiver") == receiver_identity(self, turret)
                             or any(e["name"] == "gun-turret" for e in existing["entities"]))):
                    previous = row["plan"] = existing
                    self._save()
                    break
        if existing is not None and existing != previous:
            return _report("blocked", "ammunition intake reservation differs from its owned record", turret=turret["position"])
        candidates = None
        if (previous and existing == previous and key not in self.factory.state.get("links", {})
                and not self._intake_hardware_present(obs, previous)):
            ranked, old_score = self._intake_order(turret, previous)
            better = [plan for plan, score in ranked if score is not None and old_score is not None and score > old_score]
            preferred = next(self._validated_intakes(better), None)
            if preferred is not None and self._discard_unbuilt_intake(obs, turret, key, row):
                previous = None
                # Preserve the live-placeable choice: earlier pole variants in
                # its entrance group may have failed the placement check.
                candidates = self._validated_intakes([preferred, *(plan for plan, _ in ranked if plan != preferred)])
        if previous:
            reserved = self.factory.register_plan(key, previous, obs)
            if not reserved.get("ok"):
                return _report("blocked", "ammunition intake reservation conflicts with another block", turret=turret["position"])
            result = (self.factory.connect_input(obs, source, previous["ports"][0], key)
                      if key in self.factory.state.get("links", {}) or self._intake_approach_clear(previous)
                      else _report("blocked", "ammunition intake approach is obstructed"))
            if (result.get("status") != "blocked" or result.get("type")
                    or not self._discard_unbuilt_intake(obs, turret, key, row)):
                return result if not _ready(result) else self._finish_intake(obs, key, row)
        # Cover every side within the finite geometry bound; never retry the
        # identical saved plan. Different poles may unblock the same intake.
        for candidate in islice(candidates if candidates is not None else self._intake_candidates(turret), MAX_INTAKE_CANDIDATES):
            if previous and all(candidate.get(field) == previous.get(field) for field in ("entities", "ports")):
                continue
            reserved = self.factory.register_plan(key, candidate, obs)
            if not reserved.get("ok"):
                continue
            row["plan"] = reserved
            self._save()
            result = self.factory.connect_input(obs, source, reserved["ports"][0], key)
            if (result.get("status") == "blocked" and not result.get("type")
                    and self._discard_unbuilt_intake(obs, turret, key, row)):
                continue
            return result if not _ready(result) else self._finish_intake(obs, key, row)
        return _report("blocked", "no reachable automatic ammunition intake around turret", turret=turret["position"])

    def _finish_intake(self, obs: dict, key: str, row: dict) -> dict:
        result = self.builder.ensure_plan(obs, row["plan"])
        return result if not _ready(result) else self.factory.ensure_power_connection(obs, key, row["plan"])

    def _supply_observation(self, turret: dict, row: dict) -> dict:
        inserter = next(e for e in row["plan"]["entities"] if e["name"] == "inserter")
        payload = json.dumps(json.dumps({"turret": turret["position"], "inserter": inserter["position"]}, separators=(",", ":")))
        return self.game.query('''
local p=helpers.json_to_table(''' + payload + ''');local turret=target(p.turret,"gun-turret");local arm=target(p.inserter,"inserter")
if not turret or not arm then return {ok=false,reason="turret or ammunition intake missing"} end
local ammo=turret.get_inventory(defines.inventory.turret_ammo)
local held=arm.held_stack.valid_for_read and arm.held_stack.name=="firearm-magazine" and arm.held_stack.count or 0
local produced=0
for _,e in pairs(s.find_entities_filtered{force=f,type="assembling-machine"}) do
 local recipe=e.get_recipe();if recipe and recipe.name=="firearm-magazine" then produced=produced+e.products_finished end
end
return {ok=true,ammo=ammo and ammo.get_item_count("firearm-magazine") or 0,held=held,
 powered=arm.energy>0 and arm.is_connected_to_electric_network(),producer_finished=produced,tick=game.tick}
''')

    def _supply_request(self, obs: dict, turret: dict, row: dict) -> dict | None:
        if not self._intake_owner_matches(obs, turret, row):
            return None
        arms = [e for e in row.get("plan", {}).get("entities", []) if e.get("name") == "inserter"]
        if len(arms) != 1:
            return None
        arm = arms[0]
        found = [e for e in obs.get("entities", []) if e.get("name") == "inserter"
                 and e.get("position") == arm["position"] and e.get("direction", 0) == arm.get("direction", 0)]
        if len(found) != 1 or type(found[0].get("unit_number")) is not int or found[0]["unit_number"] <= 0:
            return None
        return {"turret": dict(turret["position"]), "turret_unit": turret["unit_number"],
                "inserter": dict(arm["position"]), "inserter_unit": found[0]["unit_number"], "direction": arm.get("direction", 0)}

    def _supply_observations(self, obs: dict, turrets: list[dict]) -> dict:
        """Read one call's exact receivers and one common producer counter."""
        requests = {}
        for turret in turrets:
            request = self._supply_request(obs, turret, self.state["turrets"][self._key(turret)])
            if request is not None:
                requests[self._key(turret)] = request
        if not requests:
            return {}
        payload = json.dumps(json.dumps({"world_id": obs["world_id"], "tick": obs["tick"], "requests": requests},
                                        separators=(",", ":")))
        result = self.game.query('''
--[[ ammunition_supply_survey: read-only, exact identities, one next_action call. ]]
local x=helpers.json_to_table(''' + payload + ''')
if not d or d.world_id~=x.world_id or game.tick<x.tick then return {ok=false} end
local produced=0
for _,e in pairs(s.find_entities_filtered{force=f,type="assembling-machine"}) do
 local recipe=e.get_recipe();if recipe and recipe.name=="firearm-magazine" then produced=produced+e.products_finished end
end
local rows={}
for key,p in pairs(x.requests) do
 local turret=target(p.turret,"gun-turret");local arm=target(p.inserter,"inserter")
 local row={ok=false,identity=p};rows[key]=row
 if turret and arm and turret.force==f and arm.force==f and turret.surface==s and arm.surface==s
  and turret.unit_number==p.turret_unit and arm.unit_number==p.inserter_unit and arm.direction==p.direction
  and turret.position.x==p.turret.x and turret.position.y==p.turret.y
  and arm.position.x==p.inserter.x and arm.position.y==p.inserter.y then
  local ammo=turret.get_inventory(defines.inventory.turret_ammo)
  row.ok=true;row.ammo=ammo and ammo.get_item_count("firearm-magazine") or 0
  row.held=arm.held_stack.valid_for_read and arm.held_stack.name=="firearm-magazine" and arm.held_stack.count or 0
  row.powered=arm.energy>0 and arm.is_connected_to_electric_network()
 end
end
return {ok=true,world_id=d.world_id,tick=game.tick,producer_finished=produced,rows=rows}
''')
        valid = (isinstance(result, dict) and result.get("ok") is True and result.get("world_id") == obs["world_id"]
                 and type(result.get("tick")) is int and result["tick"] >= obs["tick"]
                 and type(result.get("producer_finished")) is int and result["producer_finished"] >= 0
                 and isinstance(result.get("rows"), dict) and set(result["rows"]) == set(requests))
        survey = {}
        for key, request in requests.items():
            live = result["rows"].get(key) if valid else None
            if (isinstance(live, dict) and live.get("ok") is True and live.get("identity") == request
                    and type(live.get("powered")) is bool
                    and all(type(live.get(field)) is int and live[field] >= 0 for field in ("ammo", "held"))):
                observation = {field: live[field] for field in ("ok", "powered", "ammo", "held")}
                observation.update(tick=result["tick"], producer_finished=result["producer_finished"])
            else:
                observation = {"ok": False, "reason": "ammunition supply survey missing or identity changed"}
            survey[key] = {"request": request, "observation": observation}
        return survey

    def _verify_supply(self, obs: dict, turret: dict, row: dict, *, live: dict | None = None) -> dict:
        if live is None:
            live = self._supply_observation(turret, row)
        if not live.get("ok") or not live.get("powered"):
            row["owned"] = False
            row.pop("sample", None)
            self._save()
            return _report("waiting", "waiting for powered ammunition intake", turret=turret["position"], observation=live)
        sample = row.get("sample")
        if sample and live["producer_finished"] < sample["producer_finished"]:
            sample = None
            row["owned"] = False
        if not live["ammo"]:
            row["owned"] = False
        if sample is None:
            row["sample"] = {"tick": live["tick"], "ammo": live["ammo"], "producer_finished": live["producer_finished"], "held": live["held"]}
            self._save()
            return _report("waiting", "sampling automatic ammunition transfer", turret=turret["position"])
        produced = live["producer_finished"] > sample["producer_finished"]
        transferred = live["ammo"] > sample["ammo"] or (sample.get("held", 0) > 0 and not live["held"] and live["ammo"] > 0)
        if produced and transferred and live["tick"] > sample["tick"] and int(obs.get("tick", 0)) > row.get("last_seed_tick", -1):
            row["owned"] = True
            row["proof"] = {"tick": live["tick"], "producer_cycles": live["producer_finished"] - sample["producer_finished"],
                            "ammo": live["ammo"], "powered": True, "input_handcarry": False}
        if live["held"]:
            sample["held"] = live["held"]
        self._save()
        if row.get("owned"):
            return _report("succeeded", "automatic ammunition production and turret supply observed", turret=turret["position"], **row["proof"])
        return _report("waiting", "waiting for actual producer and turret ammunition flow", turret=turret["position"],
                       produced=produced, transferred=transferred, ammo=live["ammo"])

    def _iron_rate(self, magazines_per_minute: float) -> float:
        recipe = self.catalog.recipe_for_product("firearm-magazine")
        iron = next(float(row["amount"]) for row in recipe["ingredients"] if row["name"] == "iron-plate")
        produced = sum(float(row.get("amount", 1)) for row in recipe["products"] if row["name"] == "firearm-magazine")
        science = list(self.factory.state.get("capacity_science", []))
        science_cycles, _ = self.factory.graph._continuous_rates(science) if science else ({}, {})
        return magazines_per_minute * iron / produced + float(science_cycles.get("iron-plate", 0))

    def _route_present(self, obs: dict, key: str, row: dict) -> bool:
        if not row.get("plan") or key not in self.factory.state.get("links", {}):
            return False
        plans = [row["plan"], self.factory.state["links"][key]]
        plans += [plan for name, plan in self.factory.state.get("power_links", {}).items() if name in {key, "tap:" + key}]
        observed = {(e["name"], round(e["position"]["x"], 2), round(e["position"]["y"], 2)): e
                    for e in obs.get("entities", [])}
        for plan in plans:
            for entity in plan["entities"]:
                found = observed.get((entity["name"], round(entity["position"]["x"], 2), round(entity["position"]["y"], 2)))
                if found is None:
                    return False
                if entity["name"] in {"inserter", "long-handed-inserter", "transport-belt"} and found.get("direction", 0) != entity.get("direction", 0):
                    return False
        return True

    def next_action(self, obs: dict) -> dict | None:
        self._sync(obs)
        turrets = [e for e in obs.get("entities", []) if e.get("name") == "gun-turret"]
        if not turrets or not (obs.get("technologies") or {}).get("automation"):
            return None
        turrets.sort(key=lambda e: int((e.get("inventory") or {}).get("firearm-magazine", 0)))
        for turret in turrets:
            row = self._track(turret)
            if row.get("owned") and not self._route_present(obs, "armaments:" + self._key(turret), row):
                row["owned"] = False
                row.pop("sample", None)
                self._save()
        for turret in turrets:
            seed = self._seed(obs, turret, self.state["turrets"][self._key(turret)])
            if seed is not None:
                return seed
        if not (obs.get("enabled_recipes") or {}).get("electric-mining-drill"):
            return self.factory.request_recipe_unlock(obs, "electric-mining-drill")
        # A cold routine lane can build before Factory.next_action has restored
        # its existing belt buffer's preferred collection source.
        from .deterministic_construction_buffer import BUFFER_KEY, ensure_construction_buffer
        self.factory._sync(obs)
        if BUFFER_KEY in self.factory.state["blocks"]:
            buffer = ensure_construction_buffer(self.factory, obs)
            if buffer is not None:
                return buffer
        rate = max(4, 2 * len(turrets))
        capacity = self.factory.ensure_product(obs, "iron-plate", rate_per_minute=self._iron_rate(rate))
        if not _ready(capacity):
            return capacity
        source = self.factory.ensure_product(obs, "firearm-magazine", rate_per_minute=rate)
        if not _ready(source):
            return source
        outputs = source.get("evidence", {}).get("ports") or []
        if not outputs:
            return _report("blocked", "ammunition producer has no material output port")
        pending = None
        supply = None
        for turret in turrets:
            row = self.state["turrets"][self._key(turret)]
            result = self._ensure_intake(obs, turret, row, outputs[0])
            if not _ready(result):
                row["owned"] = False
                row.pop("sample", None)
                self._save()
                return result
            # Start lazily so earlier seed/construction actions still return
            # without surveying. Never retain this data across next_action.
            if supply is None:
                supply = self._supply_observations(obs, turrets)
            surveyed = supply.get(self._key(turret), {})
            live = surveyed.get("observation") if surveyed.get("request") == self._supply_request(obs, turret, row) else None
            if live is None:
                live = {"ok": False, "reason": "ammunition supply intake changed after survey"}
            proof = self._verify_supply(obs, turret, row, live=live)
            if not _ready(proof):
                pending = proof
        return pending or _report("succeeded", "all managed turrets have observed automatic ammunition supply",
                                  turrets=len(turrets), requested_magazines_per_minute=rate, input_handcarry=False)
