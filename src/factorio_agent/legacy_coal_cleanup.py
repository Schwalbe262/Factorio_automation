"""Normally recover one proven isolated bootstrap burner-to-coal-cache cell."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_bootstrap import SURVEY_LAYOUT_TICKS
from factorio_agent.core.deterministic_state import _atomic_json


LEGACY_CACHE_PROBE_LUA = r"""
if not d or d.world_id~=x.world_id then return failure("legacy_cleanup_world_changed") end
local function exact(spec,box)
 local e=target(spec.position,spec.name)
 if not e then
  local query=box and {area=box,force=f} or {position=spec.position,radius=.1,force=f}
  for _,other in pairs(s.find_entities_filtered(query)) do
   if other~=a and other.type~="resource" and other.type~="item-entity" and other.type~="corpse" then
    return nil,"legacy_target_replaced"
   end
  end
  return nil,nil
 end
 if e.unit_number~=spec.unit_number or e.force~=f or e.surface~=s or e.direction~=spec.direction
  or e.position.x~=spec.position.x or e.position.y~=spec.position.y then return nil,"legacy_target_identity_changed" end
 return e,nil
end
local drill,err=exact(x.drill,x.drill_box);if err then return failure(err) end
local chest,why=exact(x.receiver,x.receiver_box);if why then return failure(why) end
if x.require_emitter and not drill then return failure("legacy_emitter_missing") end
if not x.require_emitter and drill then return failure("legacy_emitter_still_present") end
if not chest and not x.allow_receiver_missing then return failure("legacy_receiver_missing") end
if drill and (drill.name~="burner-mining-drill" or not drill.burner) then return failure("legacy_emitter_is_not_burner") end
if chest and (chest.name~="wooden-chest" or chest.type~="container") then return failure("legacy_receiver_is_not_plain_wooden_chest") end
local function inside(p,b) return p.x>=b.left_top.x and p.x<b.right_bottom.x and p.y>=b.left_top.y and p.y<b.right_bottom.y end
local box=chest and chest.bounding_box or x.receiver_box
local drill_box=drill and drill.bounding_box or x.drill_box
if not box or not drill_box then return failure("legacy_geometry_missing") end
if drill then
 if not inside(drill.drop_position,box) then return failure("legacy_drill_no_longer_drops_into_cache") end
 local radius=drill.prototype.mining_drill_radius;local count=0
 for _,ore in pairs(s.find_entities_filtered{area={{drill.position.x-radius,drill.position.y-radius},
   {drill.position.x+radius,drill.position.y+radius}},type="resource"}) do
  if ore.amount>0 then if ore.name~="coal" then return failure("legacy_drill_mixed_resource") end;count=count+1 end
 end
 if count<1 then return failure("legacy_coal_provenance_unavailable") end
end
for _,arm in pairs(s.find_entities_filtered{type="inserter"}) do
 if inside(arm.pickup_position,box) or inside(arm.drop_position,box)
  or inside(arm.pickup_position,drill_box) or inside(arm.drop_position,drill_box) then
  return failure("legacy_cache_connected_to_inserter")
 end
end
for _,other in pairs(s.find_entities_filtered{type="mining-drill"}) do
 if (not drill or other.unit_number~=drill.unit_number) and inside(other.drop_position,box) then
  return failure("legacy_cache_has_another_emitter")
 end
end
for _,e in pairs(s.find_entities_filtered{area={{box.left_top.x-1.1,box.left_top.y-1.1},
 {box.right_bottom.x+1.1,box.right_bottom.y+1.1}},type={"transport-belt","underground-belt","splitter"}}) do
 return failure("legacy_cache_adjacent_to_transport")
end
if chest then
 local ok,connectors=pcall(function() return chest.get_wire_connectors(false) end)
 if not ok then return failure("legacy_cache_wire_state_unavailable") end
 for _,connector in pairs(connectors) do
  local good,count=pcall(function()return connector.connection_count end)
  if not good then return failure("legacy_cache_wire_state_unavailable") end
  if count>0 then return failure("legacy_cache_has_circuit_consumers") end
 end
end
local function inventory_coal(inv)
 local count=0;if not inv then return count end
 for index=1,#inv do local stack=inv[index]
  if stack.valid_for_read then
   if stack.name~="coal" or stack.prototype.type~="item" or stack.quality.name~="normal"
    or stack.health~=1 or stack.spoil_tick~=0 or stack.item~=nil then return nil end
   count=count+stack.count
  end
 end
 return count
end
local receiver_coal=0
if chest then receiver_coal=inventory_coal(chest.get_inventory(defines.inventory.chest)) end
if receiver_coal==nil then return failure("legacy_cache_contains_nonplain_or_noncoal_items") end
local drill_coal=0
if drill then
 for index=1,drill.get_max_inventory_index() do
  local n=inventory_coal(drill.get_inventory(index));if n==nil then return failure("legacy_emitter_contains_nonplain_or_noncoal_items") end
  drill_coal=drill_coal+n
 end
end
if not chest and s.count_entities_filtered{area=box,type="item-entity"}>0 then return failure("legacy_cache_left_ground_goods") end
local inv=a and a.valid and a.get_main_inventory();if not inv then return failure("legacy_actor_missing") end
local empty=0;for i=1,#inv do if not inv[i].valid_for_read then empty=empty+1 end end
return {ok=true,world_id=d.world_id,receiver_coal=receiver_coal,drill_coal=drill_coal,
 emitter_present=drill~=nil,receiver_present=chest~=nil,empty_slots=empty,inventory_size=#inv,
 receiver_box=box,drill_box=drill_box,drop_position=drill and pos(drill.drop_position) or nil,
 isolation="direct burner drop; no inserter, transport, other emitter or circuit consumer"}
"""


class LegacyCoalCleanup:
    def __init__(self, game, bootstrap, builder, catalog, storage, steam, outposts):
        self.game, self.bootstrap, self.builder, self.catalog = game, bootstrap, builder, catalog
        self.storage, self.steam, self.outposts = storage, steam, outposts
        self.path = Path(game.cfg.runtime_dir) / "legacy-coal-cleanup.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _save(self):
        _atomic_json(self.path, self.state)

    def _load(self, component):
        path = getattr(component, "path", None)
        return (
            json.loads(Path(path).read_text(encoding="utf-8"))
            if path and Path(path).exists()
            else deepcopy(component.state)
        )

    def _probe(self, entry, *, require_emitter, allow_receiver_missing=False):
        args = {k: deepcopy(entry[k]) for k in ("world_id", "drill", "receiver")}
        args.update(
            require_emitter=require_emitter,
            allow_receiver_missing=allow_receiver_missing,
            receiver_box=entry.get("receiver_box"),
            drill_box=entry.get("drill_box"),
        )
        encoded = json.dumps(json.dumps(args, separators=(",", ":")))
        return self.game.query("local x=helpers.json_to_table(" + encoded + ");" + LEGACY_CACHE_PROBE_LUA)

    def _automatic(self, obs):
        saved = self._load(self.outposts)
        if (
            saved.get("world_id") != obs["world_id"]
            or saved.get("catalog_fingerprint", self.catalog.fingerprint) != self.catalog.fingerprint
        ):
            return None
        banks = [b for b in saved.get("banks", []) if b.get("ready") and b["plan"].get("resource") == "coal"]
        if not banks or any(
            not any(e["name"] == "electric-mining-drill" for e in b["plan"]["entities"])
            or any(e["name"] == "burner-mining-drill" for e in b["plan"]["entities"])
            for b in banks
        ):
            return None
        proofs = [self.outposts.bank_proof(obs, deepcopy(bank)) for bank in banks]
        if not all(proofs):
            return None
        for bank, proof in zip(banks, proofs):
            if not bank["plan"].get("paired"):
                continue
            lanes = proof.get("native_producer_lanes") or {}
            if not lanes.get("ok") or any(set(lanes.get(side, {})) - {"coal"} for side in ("left", "right")):
                return None
        return proofs

    def _sample(self, obs, history=None):
        saved = self._load(self.steam)
        if saved.get("world_id") != obs["world_id"] or saved.get("catalog_fingerprint") != self.catalog.fingerprint:
            return None, None
        previous = self.steam.state
        try:
            self.steam.state = saved
            sample = self.steam.sample(obs)
            if history is not None:
                self.steam.state["samples"] = deepcopy(history)
            proof = self.steam.proof(obs) if sample and sample.get("ok") else None
            return sample, proof
        finally:
            self.steam.state = previous

    def prepare(self, obs, parameters=None):
        if parameters not in (None, {}):
            return {"ok": False, "reason": "legacy_coal_cleanup_takes_no_parameters"}
        stack_size = self.catalog.items.get("coal", {}).get("stack_size")
        if type(stack_size) is not int or stack_size < 1:
            return {"ok": False, "reason": "catalog_normal_coal_stack_size_required"}
        survey = self.bootstrap._existing_cells("coal", max_age_ticks=SURVEY_LAYOUT_TICKS)
        if not survey.get("ok"):
            return {"ok": False, "reason": "legacy_coal_cell_survey_failed"}
        cells = [
            c
            for c in survey.get("cells", [])
            if c["drill"]["name"] == "burner-mining-drill"
            and not c.get("electric")
            and not c.get("direct_belt")
            and (c.get("receiver") or {}).get("name") == "wooden-chest"
        ]
        if len(cells) != 1:
            return {
                "ok": False,
                "reason": "one_unambiguous_legacy_burner_coal_cache_required",
                "candidates": len(cells),
            }
        actual = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in obs["entities"]}
        entry = {
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "phase": "emitter",
            "samples": [],
        }
        for key in ("drill", "receiver"):
            spec = cells[0][key]
            live = actual.get((spec["name"], spec["position"]["x"], spec["position"]["y"]))
            if (
                not live
                or type(live.get("unit_number")) is not int
                or live["unit_number"] < 1
                or (key == "drill" and live.get("direction", 0) != spec["direction"])
            ):
                return {"ok": False, "reason": "legacy_cell_observation_identity_missing_or_changed"}
            entry[key] = {k: live[k] for k in ("name", "position", "direction", "unit_number")}
        source = self._load(self.outposts)
        steam = self._load(self.steam)
        owned = [e for b in source.get("banks", []) for e in b["plan"]["entities"]] + steam.get("plan", {}).get(
            "entities", []
        )
        for key in ("power_plan", "coal_plan"):
            plan = getattr(self.builder, "state", {}).get(key, {})
            owned += plan.get("entities", [])
            if plan.get("drill"):
                owned.append(plan["drill"])
        if any(
            e["name"] == entry[k]["name"] and e["position"] == entry[k]["position"]
            for e in owned
            for k in ("drill", "receiver")
        ):
            return {"ok": False, "reason": "legacy_target_still_owned_by_automatic_plan"}
        probe = self._probe(entry, require_emitter=True)
        if not probe.get("ok"):
            return probe
        automatic = self._automatic(obs)
        sample, proof = self._sample(obs)
        if not automatic or not sample or not sample.get("ok"):
            return {"ok": False, "reason": "healthy_automatic_coal_and_loaded_steam_required_before_cleanup"}
        entry.update(
            receiver_box=probe["receiver_box"],
            drill_box=probe["drill_box"],
            initial_probe=probe,
            protected=[
                {k: e[k] for k in ("name", "position", "direction", "unit_number")}
                for e in obs["entities"]
                if e["name"] in ("electric-mining-drill", "boiler", "steam-engine")
            ],
        )
        return dict(entry, ok=True)

    def retires_legacy(self, e, obs):
        return bool(
            self.state
            and self.state.get("world_id") == obs["world_id"]
            and any(
                e.get("name") == self.state[k]["name"]
                and e.get("position") == self.state[k]["position"]
                and e.get("unit_number") == self.state[k]["unit_number"]
                for k in ("drill", "receiver")
            )
        )

    def _health(self, obs):
        if not self._automatic(obs):
            self.state["samples"] = []
            self._save()
            return None
        sample, _ = self._sample(obs)
        if not sample or not sample.get("ok") or len(sample.get("engines", [])) != 2:
            self.state["samples"] = []
            self._save()
            return None
        history = self.state["samples"]
        if history and (
            not 0 < sample["tick"] - history[-1]["tick"] <= 600
            or [e["unit_number"] for e in sample["engines"]] != [e["unit_number"] for e in history[-1]["engines"]]
        ):
            history.clear()
        history.append(sample)
        while len(history) > 2 and history[1]["tick"] <= sample["tick"] - 1800:
            history.pop(0)
        self._save()
        if len(history) < 2 or history[-1]["tick"] - history[0]["tick"] < 1800:
            return None
        _, proof = self._sample(obs, history)
        return proof

    def _presence(self, obs, key):
        wanted = self.state[key]
        live = next(
            (e for e in obs["entities"] if e["name"] == wanted["name"] and e["position"] == wanted["position"]), None
        )
        if live and (
            live.get("unit_number") != wanted["unit_number"] or live.get("direction", 0) != wanted["direction"]
        ):
            return "changed"
        if not live and any(
            e.get("position") == wanted["position"] and e["name"] not in ("character", "item-on-ground")
            for e in obs["entities"]
        ):
            return "changed"
        return "present" if live else "missing"

    def _protected(self, obs):
        return all(
            any(
                e.get("unit_number") == spec["unit_number"]
                and e["name"] == spec["name"]
                and e["position"] == spec["position"]
                and e.get("direction", 0) == spec["direction"]
                for e in obs["entities"]
            )
            for spec in self.state["protected"]
        )

    def step(self, obs, parameters=None):
        if parameters not in (None, {}):
            return {"status": "blocked", "reason": "legacy_coal_cleanup_takes_no_parameters"}
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "legacy_cleanup_world_or_catalog_changed"}
        if not self._protected(obs):
            return {"status": "blocked", "reason": "protected_automatic_coal_or_power_identity_changed"}
        for key in ("drill", "receiver"):
            if self._presence(obs, key) == "changed":
                return {"status": "blocked", "reason": "legacy_cleanup_will_not_retarget_replacement"}
        if not self._health(obs):
            return {
                "status": "waiting",
                "reason": "sampling healthy automatic coal and loaded steam for thirty seconds before legacy recovery",
            }
        if self.state["phase"] == "emitter":
            if self._presence(obs, "drill") == "missing":
                if self.state.get("pending") != "drill":
                    return {"status": "blocked", "reason": "legacy_emitter_missing_before_guarded_recovery"}
                self.state.pop("pending")
                self.state["phase"] = "receiver"
                self._save()
            else:
                probe = self._probe(self.state, require_emitter=True)
                if not probe.get("ok"):
                    return {"status": "blocked", **probe}
                slots = max(4, math.ceil(probe["drill_coal"] / self.catalog.items["coal"]["stack_size"]) + 2)
                upkeep = self.storage.step(dict(obs, inventory_empty_slots=probe["empty_slots"]), minimum_slots=slots)
                if upkeep:
                    return upkeep
                self.state["pending"] = "drill"
                self.state["last_emitter_probe"] = probe
                self._save()
                return self._mine(self.state["drill"], obs["world_id"])
        if self.state["phase"] == "receiver":
            if self._presence(obs, "drill") != "missing":
                return {"status": "blocked", "reason": "legacy_emitter_must_be_recovered_first"}
            probe = self._probe(self.state, require_emitter=False, allow_receiver_missing=True)
            if not probe.get("ok"):
                return {"status": "blocked", **probe}
            if not probe["receiver_present"]:
                if self.state.get("pending") != "receiver":
                    return {"status": "blocked", "reason": "legacy_receiver_missing_before_guarded_recovery"}
                self.state.pop("pending")
                self.state["phase"] = "verified"
                self._save()
            else:
                slots = max(4, math.ceil(probe["receiver_coal"] / self.catalog.items["coal"]["stack_size"]) + 2)
                upkeep = self.storage.step(dict(obs, inventory_empty_slots=probe["empty_slots"]), minimum_slots=slots)
                if upkeep:
                    return upkeep
                self.state["pending"] = "receiver"
                self.state["last_receiver_probe"] = probe
                self.state["receiver_headroom"] = slots
                self._save()
                return self._mine(self.state["receiver"], obs["world_id"])
        proof = self.proof(obs)
        return (
            {
                "status": "succeeded",
                "reason": "isolated legacy burner and plain coal cache normally recovered; automatic supply preserved",
                "evidence": proof,
            }
            if proof
            else {"status": "waiting", "reason": "waiting for final native legacy-removal and automatic-power proof"}
        )

    @staticmethod
    def _mine(spec, world):
        return {
            "type": "mine",
            "name": spec["name"],
            "position": spec["position"],
            "count": 1,
            "expected_entity_unit": spec["unit_number"],
            "expected_entity_world_id": world,
            "reason": "normally recover only the exact isolated legacy coal cache asset after proving replacement supply and inventory headroom",
        }

    def proof(self, obs):
        if (
            not self.state
            or self.state["phase"] != "verified"
            or self.state["world_id"] != obs["world_id"]
            or self.state["catalog_fingerprint"] != self.catalog.fingerprint
            or not self._protected(obs)
        ):
            return None
        if any(self._presence(obs, key) != "missing" for key in ("drill", "receiver")):
            return None
        probe = self._probe(self.state, require_emitter=False, allow_receiver_missing=True)
        automatic = self._automatic(obs)
        _, steam = self._sample(obs, self.state["samples"])
        if not probe.get("ok") or probe["receiver_present"] or not automatic or not steam:
            return None
        return {
            "tick": obs["tick"],
            "recovered_emitter_unit": self.state["drill"]["unit_number"],
            "recovered_receiver_unit": self.state["receiver"]["unit_number"],
            "receiver_coal_checked_before_mining": self.state.get("last_receiver_probe", {}).get("receiver_coal"),
            "receiver_headroom_slots": self.state.get("receiver_headroom"),
            "automatic_coal": automatic,
            "native_steam": steam,
            "scope": "ordinary native mining of an isolated plain-normal coal cache; no resource grants or actor feeding of automatic machines",
        }
