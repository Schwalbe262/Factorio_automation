"""Acquire real tools and issue bounded native repairs for known defensive posts."""
from __future__ import annotations

import json

from factorio_ai.deterministic_repair_control import pending_repair


REPAIR_SITE_LUA=r'''
if not d or d.world_id~=x.world or not a or not a.valid or a.unit_number~=x.actor
 or a.force~=f or a.surface~=s then return {ok=false,reason="defense_repair_actor_changed"}end
for _,q in ipairs(x.posts)do
 local e=target(q.position,q.name or "gun-turret")
 if e and e.valid and e.unit_number==q.unit_number and e.force==f and e.surface==s
  and e.position.x==q.position.x and e.position.y==q.position.y and e.health>0 and e.health<e.max_health then
  local p=s.find_non_colliding_position("character",e.position,3,.5)
  local quiet=p and s.count_entities_filtered{position=e.position,radius=16,force="enemy",type="unit"}==0
  if quiet then
   for _,enemy in pairs(s.find_entities_filtered{position=e.position,radius=64,force="enemy",type="turret"})do
    local attack=enemy.prototype.attack_parameters
    if not attack or not attack.range then quiet=false;break end
    local distance=(enemy.position.x-p.x)^2+(enemy.position.y-p.y)^2
    if distance<=(attack.range+3)^2 then quiet=false;break end
   end
  end
  if quiet then
   local function armed(g)
    local inv=g.get_inventory(defines.inventory.turret_ammo);local rounds=0
    if inv then for i=1,#inv do local st=inv[i];if st.valid_for_read and st.name=="firearm-magazine"
     and st.quality.name=="normal"then rounds=rounds+(st.count-1)*st.prototype.magazine_size+st.ammo end end end
    return rounds>0 end
   local protected=q.name~="stone-wall"and armed(e)
   if q.name=="stone-wall"then for _,c in ipairs(x.guards or{})do local g=target(c.position,"gun-turret")
    if g and g.valid and g.force==f and g.surface==s and g.unit_number==c.unit_number and g.health>0
     and(g.position.x-e.position.x)^2+(g.position.y-e.position.y)^2<=(g.prototype.attack_parameters.range-3)^2
     and armed(g)then protected=true;break end
   end end
   if protected then return {ok=true,target=q,approach=p,reachable=a.can_reach_entity(e),tick=game.tick}end
  end
 end
end
return {ok=true,tick=game.tick}
'''


class DefenseRepairs:
    def __init__(self,game,bootstrap,perimeter):
        self.game,self.bootstrap,self.perimeter=game,bootstrap,perimeter

    def next_action(self,obs):
        cleanup=pending_repair(obs)
        if cleanup:return cleanup
        if not obs.get('enabled_recipes',{}).get('repair-pack'):return None
        if not obs.get('ok')or type(obs.get('actor_unit_number'))is not int:return None
        report=self.perimeter.inspect(obs)
        if not report.get('ok')or report.get('changed'):return None
        candidates=[p for p in report.get('posts',[])if not p.get('missing')and
            0<p.get('health',0)<p.get('max_health',0)and type(p.get('unit_number'))is int]
        candidates.sort(key=lambda p:(p['health']/p['max_health'],p['unit_number']))
        if not candidates:return None
        return self._issue(obs,[dict(name='gun-turret',position=p['position'],unit_number=p['unit_number'])for p in candidates])

    def wall_action(self,obs,spec,unit):
        cleanup=pending_repair(obs)
        if cleanup:return cleanup
        if(spec.get('name')!='stone-wall'or type(unit)is not int or unit<1
                or not obs.get('enabled_recipes',{}).get('repair-pack')):return None
        return self._issue(obs,[dict(name='stone-wall',position=spec['position'],unit_number=unit)])

    def _issue(self,obs,targets):
        payload=dict(world=obs['world_id'],actor=obs['actor_unit_number'],posts=targets,
            guards=[dict(position=p['position'],unit_number=p['unit_number'])
                    for p in getattr(self.perimeter,'state',{}).get('posts',[])if p.get('unit_number')])
        result=self.game.query('local x=helpers.json_to_table('+json.dumps(json.dumps(payload,separators=(',',':')))+');'+REPAIR_SITE_LUA)
        if not result.get('ok')or not result.get('target'):return None
        carried=obs.get('inventory',{}).get('repair-pack',0)
        if carried>1:return {'status':'blocked','reason':'exactly_one_carried_native_repair_pack_required'}
        pack=self.bootstrap.ensure_item(obs,'repair-pack',1)
        if pack.get('type')or pack.get('status')!='succeeded':return pack
        if not result['reachable']:
            return {'type':'move','position':result['approach'],'reason':'approach a quiet armed post for native repair'}
        return dict(type='repair',name=result['target'].get('name','gun-turret'),position=result['target']['position'],
            expected_world_id=obs['world_id'],expected_actor_unit=obs['actor_unit_number'],
            expected_entity_unit=result['target']['unit_number'],
            reason='repair a known armed post using one ordinarily acquired repair pack')
