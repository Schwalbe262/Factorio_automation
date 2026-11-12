"""Continuous paid recovery of the exact commissioned Hardening cluster."""
from __future__ import annotations

import json
from .defense_deployment import SCOPE_FIELDS,bastion_deployment_action,bastion_group_deployment_action
from .defense_hardening import _key


class BastionMaintenance:
    def __init__(self,game,bootstrap,catalog,perimeter,hardening):
        self.game,self.bootstrap,self.catalog,self.perimeter,self.hardening=game,bootstrap,catalog,perimeter,hardening

    def step(self,obs):
        h=self.hardening
        if not h.path.exists():return None
        state=json.loads(h.path.read_text(encoding='utf-8'))
        if state.get('phase')!='verified':return None
        if not h._identity(obs,state):return {'status':'blocked','reason':'bastion_maintenance_world_or_catalog_changed'}
        if len(state.get('posts',[]))!=3:return {'status':'blocked','reason':'bastion_maintenance_exact_three_post_cluster_required'}
        board=json.loads((h.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
        disk=json.loads(self.perimeter.path.read_text(encoding='utf-8'))
        if(not h._identity(obs,board)or not h._identity(obs,disk)
                or board.get('blocks',{}).get(state['block'])!=state['plan']):
            return {'status':'blocked','reason':'bastion_maintenance_registered_authority_changed'}
        scope=disk.get('coverage_scopes',{}).get(state['scope_id'],{})
        if any(scope.get(k)!=state['scope'].get(k)for k in SCOPE_FIELDS):
            return {'status':'blocked','reason':'bastion_maintenance_registered_scope_changed'}
        for post in state['posts']:
            registered=[p for p in disk.get('posts',[])if p.get('name')==post['name']]
            if len(registered)!=1 or any(registered[0].get(k)!=post.get(k)for k in('name','position','coverage_scope','local_walk_access')):
                return {'status':'blocked','reason':'bastion_maintenance_registered_post_geometry_changed'}
        h.state=state;self.perimeter.state=disk
        report=self.perimeter.inspect(obs)
        if not report.get('ok'):return {'status':'blocked','reason':'bastion_maintenance_native_inspection_failed'}
        rows={p['name']:p for p in report['posts']}
        if any(p['name']not in rows or rows[p['name']].get('identity_changed')for p in state['posts']):
            return {'status':'blocked','reason':'bastion_maintenance_registered_post_identity_changed'}
        if all(not rows[p['name']].get('missing')and rows[p['name']].get('unit_number')==state['bindings'].get(_key(dict(p,name='gun-turret')))
               and state.get('stocked_units',{}).get(p['name'])==rows[p['name']].get('unit_number')for p in state['posts']):return None
        bound=h._bind_paid_posts(obs,disk,include_walls=False)
        if bound is not None:return bound
        missing=[p for p in state['posts']if rows[p['name']].get('missing')]
        for post in state['posts']:
            if post in missing:continue
            unit=h.state['bindings'].get(_key(dict(post,name='gun-turret')))
            if unit:h._observe_delivery(post,unit,rows[post['name']].get('full_magazines',0),h._current_receipts)
        h._save()
        if not missing:return None
        count=len(missing)
        if obs.get('inventory',{}).get('gun-turret',0)<count:
            return self.bootstrap.ensure_item(obs,'gun-turret',count)
        if report.get('actor_full_magazines',0)<20*count:
            return self.perimeter._ensure_magazines(obs,report,20*count)
        try:
            if count==3:return bastion_group_deployment_action(self.game,obs,owner_kind='hardening')
            return bastion_deployment_action(self.game,obs,missing[0]['name'],owner_kind='hardening')
        except(ValueError,KeyError,TypeError,OSError)as exc:
            return {'status':'blocked','reason':'bastion_maintenance_atomic_context_changed','detail':str(exc)}
