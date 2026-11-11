"""Paid, staged electric upgrade of the dedicated coal source, preserving trunk."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .board import entity
from .power_grid import plan_connection


class ElectricCoal:
    def __init__(self,game,bootstrap,builder):
        self.game,self.bootstrap,self.builder=game,bootstrap,builder
        self.path=Path(game.cfg.runtime_dir)/"electric-coal.json"
        self.state=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @staticmethod
    def find(obs,part):
        return next((e for e in obs["entities"] if e["name"]==part["name"] and e["position"]==part["position"]),None)

    def prepare(self,obs):
        self.builder._sync(obs)
        old=self.builder.state.get("coal_plan")
        if not old or old.get("layout_policy")!="pcb-trunks-v1":
            return {"ok":False,"reason":"regular_starter_coal_plan_required"}
        original=self.find(obs,old["drill"])
        if not original:return {"ok":False,"reason":"owned_old_drill_missing"}
        if original["name"]!="burner-mining-drill":return {"ok":False,"reason":"coal_drill_already_upgraded"}
        p=original["position"]
        replacement=entity("electric-mining-drill",p["x"]-.5,p["y"]-.5,4)
        payload=json.dumps(json.dumps({"old":original["unit_number"],"new":replacement},separators=(",",":")))
        survey=self.game.query('''
local args=helpers.json_to_table('''+payload+''');local n=args.new;local proto=prototypes.entity[n.name]
local left=n.position.x-1.5;local top=n.position.y-1.5;local right=left+3;local bottom=top+3
for _,e in pairs(s.find_entities_filtered{area={{left+.01,top+.01},{right-.01,bottom-.01}}}) do
 if e.type~="resource" and e.unit_number~=args.old and e~=a and not e.prototype.collision_mask.colliding_with_tiles_only then
  return {ok=false,reason="electric_upgrade_footprint_obstructed"}
 end
end
for x=left,right-1 do for y=top,bottom-1 do
 for layer in pairs(proto.collision_mask.layers) do if s.get_tile(x,y).collides_with(layer) then return {ok=false,reason="electric_upgrade_terrain_obstructed"} end end
end end
local radius=proto.mining_drill_radius;local amount=0
for _,ore in pairs(s.find_entities_filtered{area={{n.position.x-radius,n.position.y-radius},{n.position.x+radius,n.position.y+radius}},type="resource"}) do
 if ore.name~="coal" then return {ok=false,reason="electric_upgrade_mixed_resource"} end;amount=amount+ore.amount
end
if amount<=0 then return {ok=false,reason="electric_upgrade_no_coal"} end
local v=proto.vector_to_place_result;local drop={x=n.position.x-(v.y or v[2]),y=n.position.y+(v.x or v[1])}
return {ok=true,drop=drop,remaining=amount}
''')
        if not survey.get("ok"):return survey
        recipient=next((e for e in old["entities"] if e["name"]=="transport-belt"
                        and abs(e["position"]["x"]-survey["drop"]["x"])<.49
                        and abs(e["position"]["y"]-survey["drop"]["y"])<.49),None)
        if recipient is None:return {"ok":False,"reason":"electric_drop_does_not_match_retained_belt"}
        destination={"x":replacement["position"]["x"]+2,"y":replacement["position"]["y"]-2}
        power=plan_connection(self.game,self.builder,obs,destination,[replacement])
        if not power.get("ok"):return power
        self.state={"world_id":obs["world_id"],"old":old["drill"],"old_unit_number":original["unit_number"],
                    "replacement":replacement,"power_plan":power,"receiver":recipient,"phase":"fund_and_power",
                    "obsolete_inserters":[self.find(obs,e) for e in old["entities"] if e["name"]=="burner-inserter"],
                    "coal_plan_before":deepcopy(old)}
        _atomic_json(self.path,self.state)
        return {"ok":True,"wire_hops":power["wire_hops"],"replacement":replacement,"retained_drop_belt":recipient}

    def step(self,obs):
        if not obs.get("technologies",{}).get("electric-mining-drill"):
            return {"status":"blocked","reason":"electric_mining_research_required"}
        if not self.state:
            result=self.prepare(obs)
            if not result.get("ok"):return {"status":"blocked","reason":result.get("reason","electric_upgrade_preflight_failed")}
        if self.state["world_id"]!=obs["world_id"]:
            return {"status":"blocked","reason":"electric_upgrade_world_changed"}
        new=self.state["replacement"]
        actual=self.find(obs,new)
        if actual is None:
            if self.state["phase"]=="fund_and_power":
                if obs.get("inventory",{}).get("electric-mining-drill",0)<1:
                    return self.bootstrap.ensure_item(obs,"electric-mining-drill",1)
                result=self.builder.ensure_plan(obs,self.state["power_plan"])
                if result.get("status")!="succeeded":return result
                self.builder._sync(obs)
                evidence=self.builder.power_evidence(self.builder.state["power_plan"],self.builder.state["coal_plan"])
                if not evidence.get("ok") or evidence.get("boiler_fuel",0)<8_000_000 or evidence.get("coal_on_belts",0)<8:
                    return {"status":"waiting","reason":"wait for ordinary coal reserve before paid electric cutover"}
                self.state["phase"]="replace";_atomic_json(self.path,self.state)
            old=self.find(obs,self.state["old"])
            if old is not None:
                if old["unit_number"]!=self.state["old_unit_number"]:
                    return {"status":"blocked","reason":"old_coal_drill_identity_changed"}
                return {"type":"mine","name":old["name"],"position":old["position"],"count":1,
                        "expected_entity_unit":old["unit_number"],"expected_entity_world_id":obs["world_id"],
                        "reason":"paid electric upgrade with ordinary reserve and prebuilt powered poles"}
            return {"type":"build",**new,"reason":"install normally crafted electric coal drill on preserved output"}
        self.builder._sync(obs)
        saved=self.builder.state["coal_plan"]
        if saved["drill"]["name"]=="burner-mining-drill":
            saved["entities"]=[new if e["name"]=="burner-mining-drill" and e["position"]==self.state["old"]["position"] else e
                               for e in saved["entities"] if e["name"]!="burner-inserter"]
            saved["drill"]=deepcopy(new)
            self.builder._save()
        if saved.get("required_items",{}).get("burner-mining-drill"):
            extra={k:v for k,v in saved.items() if k not in {"entities","ports","required_items","ok","reason"}}
            self.builder.state["coal_plan"]=_plan(saved["entities"],saved["ports"],**extra)
            self.builder._save()
        if not actual.get("electric_network_connected") or actual.get("energy",0)<=0:
            return {"status":"blocked","reason":"replacement_electric_drill_has_no_power"}
        if self.state["phase"]!="verify_output":
            self.state["phase"]="verify_output";self.state["sample_started_tick"]=obs["tick"]
            self.state["replacement_unit_number"]=actual["unit_number"]
            _atomic_json(self.path,self.state)
            return {"type":"take","name":"transport-belt","position":self.state["receiver"]["position"],
                    "item":"coal","count":100,"reason":"normal recovery clears output sample tile for native electric mining proof"}
        proof=self.proof(obs)
        if proof:
            for old in self.state.get("obsolete_inserters",[]):
                if old and self.find(obs,old):
                    if self.find(obs,old)["unit_number"]!=old["unit_number"]:
                        return {"status":"blocked","reason":"obsolete_fuel_inserter_identity_changed"}
                    return {"type":"mine","name":old["name"],"position":old["position"],"count":1,
                            "expected_entity_unit":old["unit_number"],"expected_entity_world_id":obs["world_id"],
                            "reason":"recover obsolete burner-only fuel inserter after electric output proof"}
            return {"status":"succeeded","reason":"electric coal drill produces onto preserved regular trunk","evidence":proof}
        return {"status":"waiting","reason":"verify native electric coal output and retained power supply"}

    def proof(self,obs):
        if not self.state or self.state.get("phase")!="verify_output" or obs["tick"]-self.state["sample_started_tick"]<300:return None
        actual=self.find(obs,self.state["replacement"])
        if not actual or not actual.get("electric_network_connected") or actual.get("energy",0)<=0:return None
        if self.state.get("replacement_unit_number") not in (None,actual["unit_number"]):return None
        if actual.get("status_name") in {"no_minable_resources","no_mineable_resources","no_power","low_power"}:return None
        receiver=self.find(obs,self.state["receiver"])
        if not receiver:return None
        coal=receiver.get("belt_inventory",{}).get("coal",0)
        if coal>0:
            self.state["output_evidence"]={"tick":obs["tick"],"coal":coal,"drill_unit":actual["unit_number"],
                                           "belt_unit":receiver["unit_number"]}
            _atomic_json(self.path,self.state)
        evidence=self.state.get("output_evidence")
        if (not evidence or evidence["drill_unit"]!=actual["unit_number"] or evidence["belt_unit"]!=receiver["unit_number"]
                or not 0<=obs["tick"]-evidence["tick"]<=600):return None
        return {"tick":obs["tick"],"electric_drill_unit":actual["unit_number"],"power_connected":True,
                "observed_coal_on_direct_output":evidence["coal"],"output_evidence_tick":evidence["tick"],
                "coal_on_direct_output_now":coal,"wire_hops":self.state["power_plan"]["wire_hops"],
                "verification_ticks":obs["tick"]-self.state["sample_started_tick"],
                "scope":"paid dedicated-source upgrade and native output, not full30SPM or full-load energy proof"}
