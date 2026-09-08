"""Normal-material ammunition production and observed automatic turret supply."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .deterministic_state import _atomic_json
from .factory_templates import DIRECTIONS


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
        center = turret["position"]
        key = "armaments:" + self._key(turret)
        occupied = self.builder._occupied_by_plan(self.factory._reserved(exclude=key)) | self.factory._port_clearances()
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
                    entities = [*equipment, pole]
                    if self.builder._occupied_by_plan(entities) & occupied:
                        continue
                    plan = {"ok": True, "entities": [
                        {"name": "gun-turret", "position": center, "direction": turret.get("direction", 0), "_width": 2, "_height": 2},
                        *entities], "ports": [{"kind": "item", "item": "firearm-magazine", "direction": "input",
                                                "position": belts[-1]["position"], "facing": (outward + 8) % 16}]}
                    if self.builder.can_place(plan["entities"]).get("ok"):
                        yield plan

    def _ensure_intake(self, obs: dict, turret: dict, row: dict, source: dict) -> dict:
        key = "armaments:" + self._key(turret)
        if "plan" not in row:
            candidates = self._intake_candidates(turret)
            for candidate in candidates:
                reserved = self.factory.register_plan(key, candidate, obs)
                if not reserved.get("ok"):
                    continue
                row["plan"] = reserved
                self._save()
                result = self.factory.connect_input(obs, source, reserved["ports"][0], key)
                # A failed route has built nothing. Try another turret side;
                # once a link exists its ordinary construction must be resumed.
                if result.get("status") == "blocked" and key not in self.factory.state.get("links", {}):
                    row.pop("plan", None)
                    continue
                return result if not _ready(result) else self._finish_intake(obs, key, row)
            return _report("blocked", "no reachable automatic ammunition intake around turret", turret=turret["position"])
        reserved = self.factory.register_plan(key, row["plan"], obs)
        if not reserved.get("ok"):
            return _report("blocked", "ammunition intake reservation conflicts with another block", turret=turret["position"])
        result = self.factory.connect_input(obs, source, row["plan"]["ports"][0], key)
        return result if not _ready(result) else self._finish_intake(obs, key, row)

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

    def _verify_supply(self, obs: dict, turret: dict, row: dict) -> dict:
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
        for turret in turrets:
            row = self.state["turrets"][self._key(turret)]
            result = self._ensure_intake(obs, turret, row, outputs[0])
            if not _ready(result):
                row["owned"] = False
                row.pop("sample", None)
                self._save()
                return result
            proof = self._verify_supply(obs, turret, row)
            if not _ready(proof):
                pending = proof
        return pending or _report("succeeded", "all managed turrets have observed automatic ammunition supply",
                                  turrets=len(turrets), requested_magazines_per_minute=rate, input_handcarry=False)
