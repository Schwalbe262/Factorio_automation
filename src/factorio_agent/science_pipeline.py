"""One approved science batch ahead; v1 electric-mining-drill -> logistics."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from factorio_agent.core.deterministic_state import _atomic_json
from .industry import BootstrapLogistics


class SciencePipeline:
    def __init__(self,game,bootstrap,builder,catalog):
        self.game,self.bootstrap,self.builder,self.catalog=game,bootstrap,builder,catalog
        self.path=Path(game.cfg.runtime_dir)/'science-pipeline.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}
        self.last_diagnostic=None;self.target_controller=None

    def _read(self,name):
        path=self.path.parent/name
        return json.loads(path.read_text(encoding='utf-8'))if path.exists()else{}
    def _save(self):_atomic_json(self.path,self.state)
    def _decline(self,reason,**evidence):
        self.last_diagnostic=dict(reason=reason,**evidence);return None
    @staticmethod
    def _target(node):return {k:deepcopy(node[k])for k in('id','job_key','capability','parameters')}

    def _surplus(self,obs,controller):
        """Leave both the known construction bill and one full plate batch."""
        budget=obs.get('known_project_construction_budget')
        if budget is None:
            from .diagnostics import construction_budget
            budget=construction_budget(self.path.parent,obs,self.catalog)
        if(not isinstance(budget,dict)or not isinstance(budget.get('base_material_upper_bound'),dict)
                or budget.get('unresolved_materials')):raise ValueError('science_pipeline_construction_cost_unknown')
        raw=budget['base_material_upper_bound']
        if any(type(n)not in(int,float)or n<0 or n%1 for n in raw.values()):
            raise ValueError('science_pipeline_construction_cost_invalid')
        _,reserve=controller.requirements();result={}
        for item,kept in reserve.items():
            kept+=raw.get(item,0)
            filtered=obs;filter_=getattr(self.bootstrap,'_unreserved_stock_observation',None)
            if callable(filter_):filtered=filter_(obs,item)
            seen=set();public=0
            for e in filtered.get('entities',[]):
                if e.get('name')not in('stone-furnace','steel-furnace'):continue
                unit=e.get('unit_number')
                if type(unit)is not int or unit in seen:continue
                seen.add(unit);amount=e.get('output_inventory',{}).get(item,0)
                if type(amount)is int and amount>0:public+=amount
            held=obs.get('inventory',{}).get(item,0)
            if type(held)is not int or held<0:raise ValueError('observed integer actor plate inventory required')
            result[item]=max(0,public+held-kept)
        return result

    def next_action(self,obs,owner,*,owner_key=None,surplus_budget=None):
        self.last_diagnostic=None
        if owner.technology!='electric-mining-drill':return self._decline('science_pipeline_v1_edge_not_supported')
        if not obs.get('ok',True)or not obs.get('world_id'):return self._decline('science_pipeline_observation_invalid')
        if any((self.path.parent/name).exists()for name in('stop.json','supervisor-stop.json')):return self._decline('science_pipeline_stop_requested')
        complete=owner.production_complete(obs)
        if(not complete or obs.get('research')!=owner.technology or obs.get('technologies',{}).get(owner.technology)):
            return self._decline('science_pipeline_current_research_has_priority')
        try:
            ledger=self._read('jobs.json');queue=self._read('work_queue.json')
            if(ledger.get('schema_version')!=2 or queue.get('schema_version')!=1
                    or ledger.get('world_id')!=obs['world_id']or queue.get('world_id')!=obs['world_id']):
                return self._decline('science_pipeline_queue_or_ledger_world_changed')
            jobs=ledger.get('jobs',{})
            if any('pending_action'in j or'outcome_unknown'in str(j.get('reason',''))for j in jobs.values()if j.get('world_id')==obs['world_id']):
                return self._decline('science_pipeline_unresolved_world_action')
            if owner_key is None:
                owners=[k for k,j in jobs.items()if j.get('world_id')==obs['world_id']and j.get('capability')in('bootstrap_research','starter_research')
                    and j.get('parameters')=={'technology':owner.technology}and j.get('status')in('running','waiting')]
                if len(owners)!=1:return self._decline('science_pipeline_unique_active_owner_required')
                owner_key=owners[0]
            job=jobs.get(owner_key,{})
            if(job.get('world_id')!=obs['world_id']or job.get('status')not in('running','waiting')
                    or job.get('capability')not in('bootstrap_research','starter_research')or job.get('parameters')!={'technology':owner.technology}):
                return self._decline('science_pipeline_owner_job_changed')
            nodes={q['id']:q for q in queue.get('nodes',[])}
            owner_nodes=[n for n in nodes.values()if n.get('job_key')==owner_key and not n.get('paused')]
            if len(owner_nodes)!=1:return self._decline('science_pipeline_approved_owner_node_required')
            owner_id=owner_nodes[0]['id']
            eligible=[]
            for node in nodes.values():
                technology=node.get('parameters',{}).get('technology')if node.get('capability')=='bootstrap_research'else'logistics'
                supported=(node.get('capability')=='bootstrap_logistics'and node.get('parameters')=={}
                    or node.get('capability')=='bootstrap_research'and technology in('gun-turret','stone-wall','electric-mining-drill'))
                if(not supported or technology==owner.technology or obs.get('technologies',{}).get(technology)or node.get('paused')
                        or node.get('status')not in('pending','ready')or type(node.get('priority'))is not int
                        or not isinstance(node.get('job_key'),str)or not node['job_key']):continue
                if any(d not in nodes or d!=owner_id and nodes[d].get('status')!='completed'for d in node.get('depends_on',[])):continue
                future=jobs.get(node['job_key'])
                if future and(future.get('world_id')!=obs['world_id']or future.get('status')in('paused','blocked','failed','succeeded')):continue
                eligible.append(node)
            if self.state:
                if(self.state.get('world_id')!=obs['world_id']or self.state.get('catalog_fingerprint')!=self.catalog.fingerprint
                        or self.state.get('owner_key')!=owner_key or self.state.get('owner_guard')!=complete['owner_guard']):
                    return self._decline('science_pipeline_reserved_owner_changed')
                node=next((q for q in eligible if self._target(q)==self.state['target']),None)
                if node is None:return self._decline('science_pipeline_reserved_target_not_approved_or_paused')
            else:
                if not eligible:return self._decline('science_pipeline_no_approved_successor')
                node=min(eligible,key=lambda q:(-q['priority'],q.get('created_at',0),q['id']))
                if node['capability']!='bootstrap_logistics':return self._decline('science_pipeline_v1_next_edge_not_supported',target=self._target(node))
            controller=BootstrapLogistics(self.game,self.bootstrap,self.builder,self.catalog,technology='logistics')
            self.target_controller=controller
            if controller.state and not controller.state.get('prewarm'):
                return self._decline('science_pipeline_existing_target_requires_primary_resume')
            budget=self._surplus(obs,controller)if surplus_budget is None else surplus_budget
            if not self.state:
                self.state=dict(schema_version=1,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,
                    owner_key=owner_key,owner_guard=complete['owner_guard'],target=self._target(node),phase='prefill')
                self._save()
            result=controller.prewarm(obs,owner=owner,surplus_budget=budget)
            self.last_diagnostic=result
            if result.get('type'):return result
            return None
        except(OSError,KeyError,TypeError,ValueError)as exc:return self._decline('science_pipeline_context_invalid',detail=str(exc))

    def record_action(self,action,receipt):
        if not self.state:return
        controller=BootstrapLogistics(self.game,self.bootstrap,self.builder,self.catalog,technology='logistics')
        if controller.state.get('pending_transfer',{}).get('action')!=action:return
        if(controller.state.get('world_id')!=self.state.get('world_id')or controller.state.get('catalog_fingerprint')!=self.catalog.fingerprint):
            raise ValueError('science pipeline pending world/catalog changed; reconcile')
        warm=controller.state.get('prewarm',{})
        if warm.get('owner_guard')!=self.state.get('owner_guard'):raise ValueError('science pipeline pending owner changed; reconcile')
        controller.record_action(action,receipt);self.target_controller=controller
        if controller.state['allocated_inputs']==controller.state['required_inputs']:
            self.state['phase']='funded';self._save()
