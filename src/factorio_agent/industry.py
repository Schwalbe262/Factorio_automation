"""Normal machine-built science to unlock basic logistics, with finite funding.

This small fixed-port cell is a bootstrap bridge. It is not sustained 30 SPM or
automated mining input. Gear/science crafting and internal transfers are real.
"""
from __future__ import annotations

import json
import math
import hashlib
from copy import deepcopy
from pathlib import Path

from factorio_ai.deterministic_state import _atomic_json
from .board import Board


BOOTSTRAP_RESEARCH_TARGETS={'gun-turret','stone-wall','electric-mining-drill'}


def validate_bootstrap_research(parameters):
    if(not isinstance(parameters,dict)or set(parameters)!={'technology'}
            or parameters['technology']not in BOOTSTRAP_RESEARCH_TARGETS):
        raise ValueError('bootstrap_research requires gun-turret, stone-wall or electric-mining-drill')


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _powered(entity):
    energy=(entity or{}).get('energy')
    return bool(entity and entity.get('electric_network_connected')and type(energy)in(int,float)and math.isfinite(energy)and energy>0)


class BootstrapLogistics:
    def __init__(self,game,bootstrap,builder,catalog,technology='logistics'):
        if technology!='logistics':validate_bootstrap_research({'technology':technology})
        self.game,self.bootstrap,self.builder,self.catalog=game,bootstrap,builder,catalog
        self.technology=technology
        self.board=Board(game,builder,catalog)
        filename='bootstrap-logistics.json'if technology=='logistics'else'bootstrap-research-'+technology+'.json'
        self.path=Path(game.cfg.runtime_dir)/filename
        self.state=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @staticmethod
    def find(obs,part):
        return next((e for e in obs["entities"] if e["name"]==part["name"] and e["position"]==part["position"]),None)

    def requirements(self):
        name=getattr(self,'technology','logistics');tech=self.catalog.technologies[name]
        if tech.get("research_trigger") or tech["ingredients"]!=[{"name":"automation-science-pack","type":"item","amount":1}]:
            raise ValueError(name+" science rules changed")
        if name!='logistics'and(type(tech.get('unit_count'))is not int or not 1<=tech['unit_count']<=100 or tech.get('count_formula')):
            raise ValueError('bootstrap research requires a fixed positive unit count no greater than100')
        count=int(tech["unit_count"])
        science=self.catalog.recipe_for_product("automation-science-pack")
        gears=self.catalog.recipe_for_product("iron-gear-wheel")
        def product(recipe,item):
            return sum(p["amount"] for p in recipe["products"] if p["name"]==item and p.get("probability",1)==1)
        if name!='logistics':
            if({p['name']for p in science['ingredients']}!={'iron-gear-wheel','copper-plate'}
                    or {p['name']for p in gears['ingredients']}!={'iron-plate'}
                    or any(p.get('type','item')!='item'or type(p.get('amount'))is not int or p['amount']<=0
                        for r in (science,gears)for p in r['ingredients'])
                    or any(type(product(r,item))is not int or product(r,item)<=0
                        for r,item in ((science,'automation-science-pack'),(gears,'iron-gear-wheel')))):
                raise ValueError('bootstrap machine recipe rules changed')
        science_cycles=math.ceil(count/product(science,"automation-science-pack"))
        ingredients={p["name"]:p["amount"]*science_cycles for p in science["ingredients"]}
        gear_cycles=math.ceil(ingredients["iron-gear-wheel"]/product(gears,"iron-gear-wheel"))
        iron=next(p["amount"] for p in gears["ingredients"] if p["name"]=="iron-plate")*gear_cycles
        return count,{"iron-plate":int(iron),"copper-plate":int(ingredients["copper-plate"])}

    def step(self,obs):
        if self.technology=='logistics':return self._legacy_step(obs)
        return self._research_step(obs)

    def proof(self,obs):
        if self.technology=='logistics':return self._legacy_proof(obs)
        return self._research_proof(obs)

    def _legacy_step(self,obs):
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

    def _legacy_proof(self,obs):
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

    def _save(self):_atomic_json(self.path,self.state)

    def _binding_error(self,obs,plan):
        state=self.state
        if state.get('world_id')!=obs['world_id']:return 'bootstrap_research_world_changed'
        if state.get('catalog_fingerprint')!=self.catalog.fingerprint:return 'bootstrap_research_catalog_changed'
        if state.get('technology')!=self.technology:return 'bootstrap_research_target_changed'
        if state.get('plan_hash')!=_hash(plan):return 'bootstrap_research_startup_plan_changed'
        red=self.find(obs,state['red_machine'])
        if not red or red.get('unit_number')!=state['red_machine']['unit_number']:return 'bootstrap_research_red_machine_unit_changed'
        for pin in state.get('machines',[])+state.get('ports',[])+[state['lab']]:
            actual=self.find(obs,pin)
            if not actual or actual.get('unit_number')!=pin['unit_number']:return 'bootstrap_research_bound_entity_changed'
            if pin.get('recipe')and actual.get('recipe')!=pin['recipe']:return 'bootstrap_research_machine_recipe_changed'
        finished=red.get('products_finished')
        if type(finished)not in(int,float)or not math.isfinite(finished)or finished<state['baseline_products_finished']:
            return 'bootstrap_research_machine_counter_rollback'
        consumed=obs.get('production',{}).get('automation-science-pack',{}).get('consumed')
        if type(consumed)not in(int,float)or not math.isfinite(consumed)or consumed<state['baseline_consumed']:
            return 'bootstrap_research_science_counter_rollback'
        return None

    def _startup_plan(self,obs):
        board=self.board.state
        if board.get('world_id')!=obs['world_id']or board.get('catalog_fingerprint')!=self.catalog.fingerprint:return None
        return board.get('blocks',{}).get('startup_science')

    def _research_step(self,obs):
        name=self.technology;researched=obs.get('technologies',{}).get(name)
        if self.state:
            for field,expected,reason in (('world_id',obs['world_id'],'bootstrap_research_world_changed'),
                    ('catalog_fingerprint',self.catalog.fingerprint,'bootstrap_research_catalog_changed'),
                    ('technology',name,'bootstrap_research_target_changed')):
                if self.state.get(field)!=expected:return {'status':'blocked','reason':reason}
        if not self.state and researched:
            return {'status':'succeeded','reason':'bootstrap target already natively researched','evidence':self._research_proof(obs)}
        try:count,inputs=self.requirements()
        except (KeyError,TypeError,ValueError,ZeroDivisionError)as exc:
            return {'status':'blocked','reason':'bootstrap_research_rules_unsupported','evidence':{'technology':name,'detail':str(exc)}}
        tech=self.catalog.technologies[name]
        missing=[p for p in tech.get('prerequisites',[])if not obs.get('technologies',{}).get(p)]
        if not obs.get('technologies',{}).get('automation'):missing.append('automation')
        if missing:return {'status':'blocked','reason':'bootstrap_research_native_prerequisites_missing','evidence':{'missing_technologies':sorted(set(missing))}}
        if tech.get('enabled')is False:return {'status':'blocked','reason':'bootstrap_research_technology_disabled'}
        recipes={self.catalog.recipe_for_product(item)['name']for item in ('iron-gear-wheel','automation-science-pack')}
        recipes.update(('assembling-machine-1','inserter','lab'))
        locked=sorted(r for r in recipes if not obs.get('enabled_recipes',{}).get(r))
        if locked:return {'status':'blocked','reason':'bootstrap_research_native_recipes_locked','evidence':{'missing_recipes':locked}}
        if obs.get('research')not in(None,name):return {'status':'blocked','reason':'another_research_is_running','evidence':{'technology':obs['research']}}
        plan=self._startup_plan(obs)
        if plan is None:
            if self.state:return {'status':'blocked','reason':'bootstrap_research_registered_cell_missing'}
            plan=self.board.reserve_startup_science(obs)
            if not plan.get('ok'):return {'status':'blocked','reason':plan.get('reason','bootstrap_science_reservation_failed')}
        if self.state:
            error=self._binding_error(obs,plan)
            if error:return {'status':'blocked','reason':error}
            if self.state.get('required_packs')!=count or self.state.get('required_inputs')!=inputs:
                return {'status':'blocked','reason':'bootstrap_research_funding_contract_changed'}
            if self.state.get('pending_transfer'):
                return {'status':'blocked','reason':'bootstrap_research_transfer_outcome_requires_reconciliation'}
            if self.state.get('transfer_error'):return {'status':'blocked','reason':self.state['transfer_error']}
        result=self.builder.ensure_plan(obs,plan)
        if result.get('status')!='succeeded':return result
        machines=[self.find(obs,e)for e in plan['entities']if e['name']=='assembling-machine-1']
        if len(machines)!=2 or any(not _powered(e)for e in machines):
            return {'status':'blocked','reason':'science_cell_power_not_connected'}
        red=next((e for e in machines if e.get('recipe')=='automation-science-pack'),None)
        gear=next((e for e in machines if e.get('recipe')=='iron-gear-wheel'),None)
        if red is None or gear is None:return {'status':'blocked','reason':'bootstrap_research_machine_recipe_changed'}
        labs=[e for e in obs['entities']if e['name']=='lab'and _powered(e)]
        if not labs:return {'status':'blocked','reason':'powered_research_lab_missing'}
        if not self.state:
            ports=[self.find(obs,{'name':'wooden-chest','position':p['position']})for p in plan['ports']]
            pins=machines+ports+[labs[0]]
            if any(not e or type(e.get('unit_number'))is not int or e['unit_number']<1 for e in pins):
                return {'status':'blocked','reason':'bootstrap_research_native_entity_identity_required'}
            finished=red.get('products_finished');consumed=obs.get('production',{}).get('automation-science-pack',{}).get('consumed')
            if any(type(n)not in(int,float)or not math.isfinite(n)or n<0 for n in (finished,consumed)):
                return {'status':'blocked','reason':'bootstrap_research_native_baseline_required'}
            def pin(e):return {k:deepcopy(e[k])for k in ('name','position','unit_number','recipe')if k in e}
            self.state=dict(schema_version=1,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,technology=name,
                plan_hash=_hash(plan),required_packs=count,required_inputs=inputs,allocated_inputs={},
                red_machine=pin(red),machines=[pin(e)for e in machines],ports=[pin(e)for e in ports],lab=pin(labs[0]),
                baseline_products_finished=finished,baseline_consumed=consumed,collected_packs=0,supplied_packs=0,transfer_sequence=0)
            self._save()
        proof=self._research_proof(obs)
        if proof:return {'status':'succeeded','reason':'finite machine-built science naturally researched the target','evidence':proof}
        for item,total in inputs.items():
            remaining=total-self.state['allocated_inputs'].get(item,0)
            if remaining<=0:continue
            if obs.get('inventory',{}).get(item,0)<remaining:
                return self.bootstrap.ensure_item(obs,item,remaining)
            port=next(p for p in plan['ports']if p['item']==item)
            return self._transfer('fund',dict(type='insert',name='wooden-chest',position=port['position'],inventory='chest',
                item=item,count=remaining,reason='fund one finite machine research batch for '+name))
        if researched:
            return {'status':'waiting','reason':'bootstrap_target_researched_waiting_for_fresh_machine_proof','evidence':self._progress(obs)}
        if obs.get('research')!=name:return {'type':'research','technology':name,'reason':'consume normally machine-produced bootstrap science'}
        held=obs.get('inventory',{}).get('automation-science-pack',0)
        collected=self.state['collected_packs'];supplied=self.state['supplied_packs']
        if held and collected>supplied:
            return self._transfer('lab',dict(type='insert',name='lab',position=self.state['lab']['position'],inventory='lab_input',
                item='automation-science-pack',count=min(held,collected-supplied),reason='supply collected machine-built bootstrap science'))
        port=next(p for p in plan['ports']if p['direction']=='output')
        output=self.find(obs,{'name':'wooden-chest','position':port['position']})
        available=output.get('inventory',{}).get('automation-science-pack',0)
        if available and collected<count:
            return self._transfer('collect',dict(type='take',name='wooden-chest',position=port['position'],inventory='chest',
                item='automation-science-pack',count=min(available,count-collected),reason='collect owned machine-built bootstrap science'))
        return {'status':'waiting','reason':'waiting_for_finite_machine_science_and_native_research','evidence':self._progress(obs)}

    def _transfer(self,kind,action):
        self.state['transfer_sequence']+=1
        self.state['pending_transfer']=dict(kind=kind,action=deepcopy(action),sequence=self.state['transfer_sequence'])
        self._save();return action

    def record_action(self,action,receipt):
        """Acknowledge only this target's exact known ordinary transfer receipt."""
        pending=self.state.get('pending_transfer')
        if not pending or pending['action']!=action:return
        moved=receipt.get('moved')
        if receipt.get('ok')is not True or type(moved)is not int or not 0<=moved<=action['count']:
            raise ValueError('bootstrap research transfer outcome unknown; retain durable intent')
        kind=pending['kind'];item=action['item']
        if kind=='fund':self.state['allocated_inputs'][item]=self.state['allocated_inputs'].get(item,0)+moved
        elif kind=='collect':self.state['collected_packs']+=moved
        elif kind=='lab':self.state['supplied_packs']+=moved
        else:raise ValueError('unknown bootstrap research transfer kind')
        self.state['last_transfer']=dict(sequence=pending['sequence'],kind=kind,item=item,moved=moved)
        self.state.pop('pending_transfer')
        if not moved:self.state['transfer_error']='bootstrap_research_normal_transfer_moved_zero'
        self._save()

    def _progress(self,obs):
        actual=self.find(obs,self.state['red_machine'])
        return dict(technology=self.technology,required_packs=self.state['required_packs'],
            machine_products_finished_delta=actual['products_finished']-self.state['baseline_products_finished'],
            research_science_consumed=obs['production']['automation-science-pack']['consumed']-self.state['baseline_consumed'],
            allocated_normal_inputs=deepcopy(self.state['allocated_inputs']))

    def _research_proof(self,obs):
        if not obs.get('technologies',{}).get(self.technology):return None
        if not self.state:return {'tick':obs['tick'],'technology':self.technology,'already_researched':True}
        plan=self._startup_plan(obs)
        if plan is None or self._binding_error(obs,plan):return None
        if self.state.get('pending_transfer')or self.state.get('transfer_error'):return None
        if any(not _powered(self.find(obs,pin))for pin in self.state['machines']+[self.state['lab']]):return None
        evidence=self._progress(obs)
        recipe=self.catalog.recipe_for_product('automation-science-pack')
        output=sum(p['amount']for p in recipe['products']if p['name']=='automation-science-pack')
        packs=evidence['machine_products_finished_delta']*output
        if packs<self.state['required_packs']or evidence['research_science_consumed']<=0:return None
        return dict(evidence,tick=obs['tick'],normally_assembled_packs=packs,red_machine_unit=self.state['red_machine']['unit_number'],
            baseline_products_finished=self.state['baseline_products_finished'],plan_hash=self.state['plan_hash'],
            scope='finite assisted raw buffers; normal gear/science machines and native research, no sustained science rate claim')
