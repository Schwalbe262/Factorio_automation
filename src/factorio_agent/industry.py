"""Normal machine-built science to unlock basic logistics, with finite funding.

This small fixed-port cell is a bootstrap bridge. It is not sustained 30 SPM or
automated mining input. Gear/science crafting and internal transfers are real.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from factorio_ai.deterministic_state import _atomic_json
from .board import Board


class BootstrapLogistics:
    def __init__(self,game,bootstrap,builder,catalog):
        self.game,self.bootstrap,self.builder,self.catalog=game,bootstrap,builder,catalog
        self.board=Board(game,builder,catalog)
        self.path=Path(game.cfg.runtime_dir)/"bootstrap-logistics.json"
        self.state=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @staticmethod
    def find(obs,part):
        return next((e for e in obs["entities"] if e["name"]==part["name"] and e["position"]==part["position"]),None)

    def requirements(self):
        tech=self.catalog.technologies["logistics"]
        if tech.get("research_trigger") or tech["ingredients"]!=[{"name":"automation-science-pack","type":"item","amount":1}]:
            raise ValueError("logistics science rules changed")
        count=int(tech["unit_count"])
        science=self.catalog.recipe_for_product("automation-science-pack")
        gears=self.catalog.recipe_for_product("iron-gear-wheel")
        def product(recipe,item):
            return sum(p["amount"] for p in recipe["products"] if p["name"]==item and p.get("probability",1)==1)
        science_cycles=math.ceil(count/product(science,"automation-science-pack"))
        ingredients={p["name"]:p["amount"]*science_cycles for p in science["ingredients"]}
        gear_cycles=math.ceil(ingredients["iron-gear-wheel"]/product(gears,"iron-gear-wheel"))
        iron=next(p["amount"] for p in gears["ingredients"] if p["name"]=="iron-plate")*gear_cycles
        return count,{"iron-plate":int(iron),"copper-plate":int(ingredients["copper-plate"])}

    def step(self,obs):
        if obs.get("technologies",{}).get("logistics"):
            return {"status":"succeeded","reason":"logistics naturally researched","evidence":self.proof(obs)}
        if not obs.get("technologies",{}).get("automation"):
            return {"status":"blocked","reason":"automation_required_for_machine_science"}
        if self.state and self.state["world_id"]!=obs["world_id"]:
            return {"status":"blocked","reason":"industry_world_changed"}
        count,inputs=self.requirements()
        if not self.state:
            self.state={"world_id":obs["world_id"],"allocated_inputs":{},"required_packs":count,
                        "baseline_produced":obs["production"]["automation-science-pack"]["produced"],
                        "baseline_consumed":obs["production"]["automation-science-pack"]["consumed"]}
            _atomic_json(self.path,self.state)
        plan=self.board.reserve_startup_science(obs)
        if not plan.get("ok"):
            return {"status":"blocked","reason":plan.get("reason","board_reservation_failed")}
        result=self.builder.ensure_plan(obs,plan)
        if result.get("status")!="succeeded":return result
        machines=[self.find(obs,e) for e in plan["entities"] if e["name"]=="assembling-machine-1"]
        if any(not e or not e.get("electric_network_connected") for e in machines):
            return {"status":"blocked","reason":"science_cell_power_not_connected"}
        for item,total in inputs.items():
            allocated=self.state["allocated_inputs"].get(item,0)
            remaining=total-allocated
            if remaining<=0:continue
            if obs.get("inventory",{}).get(item,0)<remaining:
                return self.bootstrap.ensure_item(obs,item,remaining)
            port=next(p for p in plan["ports"] if p["item"]==item)
            self.state["allocated_inputs"][item]=total
            _atomic_json(self.path,self.state)
            return {"type":"insert","name":"wooden-chest","position":port["position"],"inventory":"chest",
                    "item":item,"count":remaining,"reason":"fund finite normal machine-science inputs"}
        labs=[e for e in obs["entities"] if e["name"]=="lab" and e.get("electric_network_connected")]
        if not labs:return {"status":"blocked","reason":"powered_research_lab_missing"}
        if obs.get("research") not in (None,"logistics"):
            return {"status":"blocked","reason":"another_research_is_running"}
        if obs.get("research")!="logistics":
            return {"type":"research","technology":"logistics","reason":"consume normally assembled science for logistics"}
        held=obs.get("inventory",{}).get("automation-science-pack",0)
        if held:
            return {"type":"insert","name":"lab","position":labs[0]["position"],"inventory":"lab_input",
                    "item":"automation-science-pack","count":min(held,count),"reason":"supply machine-produced logistics science"}
        port=next(p for p in plan["ports"] if p["direction"]=="output")
        output=self.find(obs,{"name":"wooden-chest","position":port["position"]})
        available=(output or {}).get("inventory",{}).get("automation-science-pack",0)
        if available:
            return {"type":"take","name":"wooden-chest","position":port["position"],"inventory":"chest",
                    "item":"automation-science-pack","count":min(available,count),"reason":"collect real assembled bootstrap science"}
        return {"status":"waiting","reason":"waiting for machine science and natural logistics research",
                "evidence":{"required_packs":count,"machine_products_finished":[e.get("products_finished",0) for e in machines],
                            "research_progress":obs.get("research_progress",0)}}

    def proof(self,obs):
        if not obs.get("technologies",{}).get("logistics"):return None
        block=self.board.state.get("blocks",{}).get("startup_science")
        if block is None:return {"tick":obs["tick"],"technology":"logistics","already_researched":True}
        red=next(e for e in block["entities"] if e.get("recipe")=="automation-science-pack")
        actual=self.find(obs,red)
        produced=(actual or {}).get("products_finished",0)
        count=self.state.get("required_packs",0)
        if produced<count:return None
        return {"tick":obs["tick"],"technology":"logistics","normally_assembled_packs":produced,
                "research_science_consumed":obs["production"]["automation-science-pack"]["consumed"]-self.state["baseline_consumed"],
                "allocated_normal_inputs":self.state["allocated_inputs"],"reserved_area":block["reserved_area"],
                "scope":"finite starter machine funding; not sustained automated raw input or30SPM"}
