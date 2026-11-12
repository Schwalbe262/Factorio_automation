from copy import deepcopy
import json
import re
import unittest
from unittest.mock import Mock

from factorio_agent.defense_deployment import (ACTION,DEPLOY_BASTION_LUA,GROUP_DEPLOY_LUA,bastion_deployment_action,
    bastion_group_deployment_action,execute_bastion_deployment,execute_bastion_group,validate_bastion_deployment)
import test_v2_defense_hardening as hardening_tests

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


class DeploymentFixture(unittest.TestCase):
    def setUp(self):
        hardening_tests.HardeningControllerTests.setUp(self)
        self.c.state=self.c.prepare(self.obs);self.assertTrue(self.c.state['ok']);self.c._save();self.c.step(self.obs)
        self.game.expected_world='w';self.game.backend='assisted';self.obs['actor_unit_number']=11
        self.post=self.c.state['posts'][0];self.action=bastion_deployment_action(self.game,self.obs,self.post['name'])


class DeploymentContextTests(DeploymentFixture):
    def test_emitter_is_readonly_and_numeric_coordinates_have_one_generation(self):
        before=self.c.path.read_bytes();board=(self.root/'pcb-board.json').read_bytes();perimeter=self.perimeter.path.read_bytes()
        action=bastion_deployment_action(self.game,self.obs,self.post['name'])
        self.assertEqual(self.c.path.read_bytes(),before);self.assertEqual((self.root/'pcb-board.json').read_bytes(),board)
        self.assertEqual(self.perimeter.path.read_bytes(),perimeter)
        converted=deepcopy(action);converted['position']={k:float(n)for k,n in action['position'].items()}
        validate_bastion_deployment(converted,'w');self.assertEqual(converted['deployment_id'],action['deployment_id'])

    def test_malformed_world_actor_position_or_generation_reject_before_io(self):
        self.game.query.reset_mock()
        for change in({'expected_world_id':'foreign'},{'expected_actor_unit':True},{'position':{'x':float('nan'),'y':0}},
                       {'position':{'x':.5,'y':0}},{'expected_lost_unit':False},{'deployment_id':'0'*64},{'owner_kind':'../arbitrary'},{'extra':1}):
            with self.subTest(change=change),self.assertRaises(ValueError):execute_bastion_deployment(self.game,{**self.action,**change})
        self.game.query.assert_not_called()

    def test_changed_board_post_scope_or_foreign_future_reservation_rejects_before_io(self):
        board_path=self.root/'pcb-board.json';board=json.loads(board_path.read_text());before=deepcopy(board)
        board['blocks'][self.c.state['block']]['layout_policy']='changed';board_path.write_text(json.dumps(board))
        self.game.query.reset_mock()
        with self.assertRaises(ValueError):execute_bastion_deployment(self.game,self.action)
        self.game.query.assert_not_called();board_path.write_text(json.dumps(before))
        p=self.action['position'];board['blocks']=deepcopy(before['blocks']);board['blocks']['future-refinery']=dict(
            entities=[],ports=[],reserved_area=[[p['x']-2,p['y']-2],[p['x']+2,p['y']+2]])
        board_path.write_text(json.dumps(board))
        with self.assertRaises(ValueError):execute_bastion_deployment(self.game,self.action)
        self.game.query.assert_not_called()

    def test_unknown_registered_name_and_changed_loss_generation_are_rejected(self):
        with self.assertRaises(ValueError):bastion_deployment_action(self.game,self.obs,'unknown')
        post=next(p for p in self.perimeter.state['posts']if p['name']==self.post['name'])
        post['lost_unit_number']=900;self.perimeter._save();self.game.query.reset_mock()
        with self.assertRaises(ValueError):execute_bastion_deployment(self.game,self.action)
        self.game.query.assert_not_called()

    def test_foreign_unbuilt_buried_span_and_published_port_apron_remain_protected(self):
        path=self.root/'pcb-board.json';board=json.loads(path.read_text());p=self.action['position']
        board['blocks']['future-trunk']=dict(entities=[],ports=[],underground_pairs=[dict(
            input=[p['x']-2.5,p['y']+.5],output=[p['x']+2.5,p['y']+.5],direction=4,span=5)])
        path.write_text(json.dumps(board));self.game.query.reset_mock()
        with self.assertRaises(ValueError):execute_bastion_deployment(self.game,self.action)
        board['blocks'].pop('future-trunk');path.write_text(json.dumps(board))
        (self.root/'fluid-sources.json').write_text(json.dumps(dict(world_id='w',catalog_fingerprint='c',sources={
            'future-water':dict(kind='fluid',fluid='water',position={'x':p['x']-2.5,'y':p['y']+.5},direction='output',facing=4)})))
        with self.assertRaises(ValueError):execute_bastion_deployment(self.game,self.action)
        self.game.query.assert_not_called()


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeDeploymentTests(DeploymentFixture):
    def setUp(self):
        super().setUp();self.native_scene()

    def native_scene(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(a)for a in v])
            return v
        self.table=table;self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        self.lua.execute('''game={tick=100};d={world_id="w"};f={name="player"};enemy={name="enemy"};s={};a={valid=true,unit_number=11,force=f,surface=s,player={}}
defines={inventory={turret_ammo=1}};prototypes={item={["gun-turret"]={type="item",stack_size=50},["firearm-magazine"]={type="ammo",stack_size=100,magazine_size=10,ammo_category={name="bullet"}},["piercing-rounds-magazine"]={type="ammo",stack_size=100,magazine_size=10,ammo_category={name="bullet"}}},entity={}}
f.recipes={["firearm-magazine"]={enabled=true},["piercing-rounds-magazine"]={enabled=true}}
prototypes.entity["gun-turret"]={tile_width=2,tile_height=2,attack_parameters={range=18},collision_mask={layers={object=true}},get_inventory_size=function()return 1 end}
world={};next_unit=1000;built_calls=0;ore=false;water=false;blocked=false;failed_create=false;throw_after_create=false;worms={};auto_sort=false
function stack(name,count,ammo)
 local data={name=name,count=count or 0,ammo=ammo or 10,quality={name="normal"},health=1,spoil_tick=0,
  is_item_with_tags=false,is_item_with_inventory=false,is_item_with_entity_data=false,prototype=prototypes.item[name]}
 return setmetatable({},{__index=function(_,k)if k=="valid_for_read"then return data.count>0 end;return data[k]end,
  __newindex=function(_,k,v)data[k]=v;if k=="count"and auto_sort and main then sort_main()end end})end
function inventory(rows)
 local inv=rows;inv.valid=true
 inv.get_item_count=function(q)local n=0;for i=1,#inv do local st=inv[i];if st.valid_for_read and(not q or(st.name==q.name and st.quality.name==(q.quality or"normal")))then n=n+st.count end end;return n end
 inv.get_insertable_count=function(q)local n=0;for i=1,#inv do local st=inv[i]
  if not st.valid_for_read then n=n+prototypes.item[q.name].stack_size elseif st.name==q.name and st.quality.name==(q.quality or"normal")then n=n+prototypes.item[q.name].stack_size-st.count end
 end;return n end
 inv.insert=function(q)local left=q.count
  for i=1,#inv do local st=inv[i];if st.valid_for_read and st.name==q.name and st.quality.name==(q.quality or"normal")then
   local n=math.min(left,prototypes.item[q.name].stack_size-st.count);st.count=st.count+n;left=left-n end end
  for i=1,#inv do if left>0 and not inv[i].valid_for_read then local n=math.min(left,prototypes.item[q.name].stack_size);inv[i]=stack(q.name,n);left=left-n end end
  return q.count-left end
 return inv end
main=inventory({stack("gun-turret",2),stack("firearm-magazine",40),stack("firearm-magazine",5,3),stack(nil,0),stack(nil,0),stack(nil,0)})
function sort_main()
 local rows={};for i=1,#main do if main[i].valid_for_read then rows[#rows+1]=main[i]end end
 for i=1,#main do main[i]=rows[i]or stack(nil,0)end end
a.get_main_inventory=function()return main end
function key(name,p)return name..":"..p.x..":"..p.y end
function target(p,name)return world[key(name,p)]end
function turret(p,unit)
 local inv=inventory({stack(nil,0)});local e={valid=true,name="gun-turret",unit_number=unit,position=p,force=f,surface=s,quality={name="normal"},health=400,max_health=400,inv=inv}
 e.get_inventory=function()return inv end;world[key(e.name,p)]=e;return e end
s.count_entities_filtered=function()return ore and 1 or 0 end
s.find_entities_filtered=function(q)local found={};for _,e in pairs(worms)do
 if not q.radius or(e.position.x-q.position.x)^2+(e.position.y-q.position.y)^2<=q.radius^2 then found[#found+1]=e end
end;return found end
s.get_tile=function()return {collides_with=function()return water end}end
s.can_place_entity=function(q)return not blocked and not water and target(q.position,q.name)==nil end
s.create_entity=function(q)
 if failed_create then return nil end
 built_calls=built_calls+1;local e=turret(q.position,next_unit);next_unit=next_unit+1
 if throw_after_create then error("injected native callback interruption")end
 return e end
''')
        def python(v):
            if not hasattr(v,'items'):return v
            rows=dict(v.items())
            if rows and set(rows)==set(range(1,len(rows)+1)):return [python(rows[i])for i in range(1,len(rows)+1)]
            return {k:python(a)for k,a in rows.items()}
        self.game.query=Mock(side_effect=lambda body:python(self.lua.execute('return(function() '+' '.join(body.splitlines())+' end)()')))

    def marker(self):return self.lua.globals().d.defense_deployments[self.action['deployment_id']]

    def test_native_build_and_full_arm_share_tick_and_conserve_guns_magazines_rounds(self):
        result=execute_bastion_deployment(self.game,self.action);self.assertTrue(result['ok'],result)
        self.assertEqual((result['paid_turrets'],result['loaded_magazines'],result['loaded_rounds']),(1,20,200))
        self.assertEqual(result['start_tick'],result['armed_tick']);self.assertEqual(self.lua.globals().built_calls,1)
        self.assertEqual((result['before']['guns'],result['after']['guns']),(2,1))
        self.assertEqual((result['rounds_before'],result['rounds_after']),(443,443))
        self.assertEqual((self.lua.globals().main[3].count,self.lua.globals().main[3].ammo),(5,3))
        self.assertEqual(self.marker().phase,'complete')

    def test_completed_paid_receipt_replay_costs_nothing_and_never_adopts_replaced_entity(self):
        first=execute_bastion_deployment(self.game,self.action)
        replay=execute_bastion_deployment(self.game,self.action)
        self.assertTrue(replay['reused_receipt']);self.assertEqual((replay['paid_this_call'],replay['loaded_this_call']),(0,0))
        self.assertEqual(replay['unit_number'],first['unit_number']);self.assertEqual(self.lua.globals().built_calls,1)
        self.lua.globals().turret(self.table(self.action['position']),9999)
        self.assertEqual(execute_bastion_deployment(self.game,self.action)['reason'],'paid_bastion_missing_or_replaced')
        self.assertEqual(self.lua.globals().built_calls,1)

    def test_preexisting_unmarked_native_entity_never_becomes_a_paid_receipt(self):
        self.lua.globals().turret(self.table(self.action['position']),9999)
        self.assertEqual(execute_bastion_deployment(self.game,self.action)['reason'],'unmarked_existing_bastion_not_adopted')
        self.assertEqual(self.lua.globals().main[1].count,2);self.assertIsNone(self.lua.globals().d.defense_deployments)

    def test_managed_known_loss_allows_stale_owned_binding_and_marker_replay_after_watch_bind(self):
        spec={'name':'gun-turret','position':self.post['position']}
        from factorio_agent.defense_deployment import _key
        self.c.state['bindings'][_key(spec)]=900;self.c._save()
        post=next(p for p in self.perimeter.state['posts']if p['name']==self.post['name'])
        post.update(lost_unit_number=900,lost_unit_numbers=[900],lost_unit_origin=900);post.pop('unit_number',None)
        self.perimeter._save();self.action=bastion_deployment_action(self.game,self.obs,self.post['name'])
        before=self.c.path.read_bytes();first=execute_bastion_deployment(self.game,self.action)
        self.assertTrue(first['ok'],first);self.assertEqual(self.c.path.read_bytes(),before)
        post['unit_number']=first['unit_number'];self.perimeter._save()
        replay=execute_bastion_deployment(self.game,self.action);self.assertTrue(replay['reused_receipt'],replay)
        self.assertEqual(self.lua.globals().built_calls,1)

    def test_unproven_stale_binding_cannot_authorize_another_generation(self):
        from factorio_agent.defense_deployment import _key
        self.c.state['bindings'][_key({'name':'gun-turret','position':self.post['position']})]=899;self.c._save()
        post=next(p for p in self.perimeter.state['posts']if p['name']==self.post['name'])
        post.update(lost_unit_number=900,lost_unit_numbers=[900],lost_unit_origin=900);self.perimeter._save()
        action=bastion_deployment_action(self.game,self.obs,self.post['name'])
        result=execute_bastion_deployment(self.game,action)
        self.assertEqual(result['reason'],'bastion_known_loss_must_be_recovered_before_build')
        self.assertEqual(self.lua.globals().built_calls,0);self.assertEqual(self.lua.globals().main[1].count,2)

    def test_partial_first_magazine_is_preserved_and_auto_sort_cannot_break_source_slots(self):
        self.lua.execute('main[1].count=1;main[2]=stack("firearm-magazine",6);main[3]=stack("firearm-magazine",15,3);auto_sort=true')
        result=execute_bastion_deployment(self.game,self.action);self.assertTrue(result['ok'],result)
        self.assertEqual((result['loaded_magazines'],result['loaded_rounds']),(20,200))
        self.assertEqual(self.lua.globals().main[1].ammo,3);self.assertEqual(self.lua.globals().main[1].count,1)
        self.assertEqual(result['rounds_before'],result['rounds_after'])

    def test_funding_actor_world_resource_tile_worm_and_metadata_guards_precede_payment(self):
        cases=(('a.unit_number=12','a.unit_number=11','bastion_deployment_actor_changed'),
            ('d.world_id="other"','d.world_id="w"','bastion_deployment_world_changed'),
            ('ore=true','ore=false','bastion_site_changed_or_resource_overlap'),
            ('water=true','water=false','bastion_site_changed_or_resource_overlap'),
            ('main[1].health=.5','main[1].health=1','bastion_actor_ammo_metadata_unsupported'),
            ('main[2].is_item_with_tags=true','main[2].is_item_with_tags=false','bastion_actor_ammo_metadata_unsupported'),
            ('main[2].count=1;main[3].count=1','main[2].count=40;main[3].count=5','bastion_paid_plain_turret_and_twenty_main_magazines_required'))
        for change,restore,reason in cases:
            self.lua.execute(change);self.assertEqual(execute_bastion_deployment(self.game,self.action)['reason'],reason);self.lua.execute(restore)
            self.assertEqual(self.lua.globals().main[1].count,2)
        p=self.action['position'];self.lua.globals().worms=self.table([dict(position=p,prototype=dict(attack_parameters=dict(range=25)))])
        self.assertEqual(execute_bastion_deployment(self.game,self.action)['reason'],'bastion_site_inside_enemy_turret_range')
        self.assertEqual(self.lua.globals().built_calls,0);self.assertIsNone(self.lua.globals().d.defense_deployments)

    def test_failed_create_refunds_exact_paid_turret_and_records_known_failed_intent(self):
        self.lua.execute('failed_create=true')
        result=execute_bastion_deployment(self.game,self.action)
        self.assertEqual(result['reason'],'bastion_build_failed_paid_turret_refunded')
        self.assertEqual(self.lua.globals().main[1].count,2);self.assertEqual(self.marker().phase,'failed_refunded')
        self.assertEqual(execute_bastion_deployment(self.game,self.action)['reason'],'bastion_deployment_incomplete_intent_requires_recovery')

    def test_interrupted_creation_keeps_intent_and_never_adopts_unknown_existing_gun(self):
        self.lua.execute('throw_after_create=true')
        with self.assertRaises(Exception):execute_bastion_deployment(self.game,self.action)
        self.assertEqual(self.marker().phase,'intent');self.assertIsNone(self.marker().unit_number)
        self.assertEqual(self.lua.globals().main[1].count,1);self.assertEqual(self.lua.globals().built_calls,1)
        self.assertEqual(execute_bastion_deployment(self.game,self.action)['reason'],'bastion_deployment_incomplete_intent_requires_recovery')
        self.assertEqual(self.lua.globals().built_calls,1)

    def test_flattened_script_has_no_health_ammo_research_or_enemy_damage_setters(self):
        self.assertNotIn('--',DEPLOY_BASTION_LUA)
        self.assertIsNone(re.search(r'\.(?:health|ammo)\s*=(?!=)',DEPLOY_BASTION_LUA))
        for word in('.destroy(','.damage(','set_research','insert_fluid','set_fluid'):self.assertNotIn(word,DEPLOY_BASTION_LUA)


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeAssaultDeploymentTests(DeploymentFixture):
    def setUp(self):
        DeploymentFixture.setUp(self);NativeDeploymentTests.native_scene(self)
        from factorio_agent.defense_deployment import _hash
        stage=deepcopy(self.c.state);old_scope=stage['scope_id'];old_block=stage['block']
        stage.update(scope_id='nest:operation-1:stage0',block='nest_assault_stage0',phase='deploy')
        for post in stage['posts']:post.update(coverage_scope=stage['scope_id'],ammo_item='firearm-magazine')
        for post in self.perimeter.state['posts']:
            if post.get('coverage_scope')==old_scope:post['coverage_scope']=stage['scope_id']
        self.perimeter.state['coverage_scopes'][stage['scope_id']]=self.perimeter.state['coverage_scopes'].pop(old_scope)
        self.perimeter._save()
        board=json.loads((self.root/'pcb-board.json').read_text());board['blocks'][stage['block']]=board['blocks'].pop(old_block)
        (self.root/'pcb-board.json').write_text(json.dumps(board))
        target=dict(name='small-worm-turret',type='turret',unit_number=200,position={'x':0,'y':14},force_index=2,
            surface_index=1,world_id='w',combat=dict(range=25),range=25,reviewed_range=25)
        profile=dict(ammo_damage=0,turret_damage=0,gun_speed=0)
        backs=[dict(name='gun-turret',unit_number=301,position={'x':-8,'y':4}),
               dict(name='gun-turret',unit_number=302,position={'x':-4,'y':0})]
        policy=dict(mode='controlled_projectile_assault_v1',operation_id='operation-1',target_set_hash=_hash([target]),
            stage_index=0,started_tick=100,deadline_tick=10900,max_post_losses=2,max_enemy_range=25,min_rear_count=2,
            coverage_margin=2,backstop_specs=backs,targets=[target],force_profile=profile,
            min_backstop_health_fraction=.5,min_backstop_full_mags=5)
        policy['ammo_item']='firearm-magazine';stage['ammo_item']='firearm-magazine';stage['underfire_policy']=policy
        stage['perimeter']=dict(world_id='w',catalog_fingerprint='c',posts=deepcopy(stage['posts']),
            coverage_scopes={stage['scope_id']:deepcopy(stage['scope'])})
        self.perimeter.state['posts']=[p for p in self.perimeter.state['posts']if p.get('coverage_scope')!=stage['scope_id']]
        self.perimeter.state['coverage_scopes'].pop(stage['scope_id']);self.perimeter._save()
        self.document=dict(world_id='w',catalog_fingerprint='c',id='operation-1',phase='deploy',active_stage=0,stages=[stage],
            targets=[target],started_tick=100,deadline_tick=10900,target_set_hash=policy['target_set_hash'],force_profile=profile)
        self.assault_path=self.root/'nest-assault.json';self.assault_path.write_text(json.dumps(self.document))
        self.lua.execute('''f.index=1;s.index=1;s.name="nauvis";enemy.index=2;game.forces={enemy=enemy}
f.get_ammo_damage_modifier=function()return 0 end;f.get_turret_attack_modifier=function()return 0 end;f.get_gun_speed_modifier=function()return 0 end
f.get_friend=function()return false end;f.get_cease_fire=function()return false end
defines.events={on_entity_died=42};_G.__factorio_nest_death_handler=function()end
script={get_event_handler=function()return _G.__factorio_nest_death_handler end}
prototypes.entity["small-worm-turret"]={type="turret",attack_parameters={range=25}}
prototypes.entity["medium-worm-turret"]={type="turret",attack_parameters={range=30}}
local original=turret;turret=function(p,unit)local e=original(p,unit);e.type="ammo-turret";e.priorities={}
 e.set_priority_target=function(index,name)e.priorities[index]=name end;return e end
''')
        for back in backs:
            gun=self.lua.globals().turret(self.table(back['position']),back['unit_number'])
            gun.inv[1]=self.lua.globals().stack('firearm-magazine',5)
        self.foe=self.table(dict(target,valid=True,health=200,max_health=200,prototype=dict(attack_parameters=dict(range=25))))
        self.foe.force=self.lua.globals().enemy;self.foe.surface=self.lua.globals().s
        self.lua.globals().world['small-worm-turret:0:14']=self.foe;self.lua.globals().worms=self.lua.table_from([self.foe])
        operation=dict(world_id='w',catalog_fingerprint='c',actor_unit=11,target_set_hash=policy['target_set_hash'],
            deadline_tick=10900,started_tick=100,loss_count=0,posts={},deaths={},targets={'200':target})
        self.lua.globals().d.nest_assaults=self.table({'operation-1':operation})
        self.action=bastion_deployment_action(self.game,self.obs,self.post['name'],owner_kind='nest_assault')

    def test_reviewed_assault_can_build_under_listed_worm_with_two_real_rears_and_normal_ui(self):
        result=execute_bastion_deployment(self.game,self.action);self.assertTrue(result['ok'],result)
        self.assertEqual((result['owner_kind'],result['paid_turrets'],result['loaded_magazines']),('nest_assault',1,20))
        p=self.action['position'];e=self.lua.globals().world[f"gun-turret:{p['x']}:{p['y']}"]
        self.assertEqual(e.priorities[1],'small-worm-turret');self.assertFalse(e.ignore_unprioritised_targets)
        self.assertIsNotNone(self.lua.globals().d.nest_assaults['operation-1'].posts[str(result['unit_number'])])
        self.assertEqual(self.foe.health,200)

    def test_deadline_loss_force_and_current_rear_stock_guards_precede_payment(self):
        cases=(('game.tick=10901','game.tick=100','assault_launch_budget_or_native_authority_changed'),
               ('d.nest_assaults["operation-1"].loss_count=2','d.nest_assaults["operation-1"].loss_count=0','assault_launch_budget_or_native_authority_changed'),
               ('f.get_gun_speed_modifier=function()return -.1 end','f.get_gun_speed_modifier=function()return 0 end','assault_force_profile_weakened'),
               ('world["gun-turret:-8:4"].inv[1].count=4','world["gun-turret:-8:4"].inv[1].count=5','assault_requires_two_current_armed_rear_posts'))
        for change,restore,reason in cases:
            self.lua.execute(change);result=execute_bastion_deployment(self.game,self.action)
            self.assertEqual(result['reason'],reason);self.assertEqual(self.lua.globals().main[1].count,2)
            self.assertIsNone(self.lua.globals().d.defense_deployments);self.lua.execute(restore)

    def test_unknown_native_journal_and_duplicate_backstop_cannot_enable_underfire(self):
        original=self.lua.globals().d.nest_assaults;self.lua.globals().d.nest_assaults=None
        self.assertEqual(execute_bastion_deployment(self.game,self.action)['reason'],'assault_launch_budget_or_native_authority_changed')
        self.lua.globals().d.nest_assaults=original
        policy=self.document['stages'][0]['underfire_policy'];policy['backstop_specs'][1]=deepcopy(policy['backstop_specs'][0])
        self.assault_path.write_text(json.dumps(self.document));self.game.query.reset_mock()
        with self.assertRaises(ValueError):bastion_deployment_action(self.game,self.obs,self.post['name'],owner_kind='nest_assault')
        self.game.query.assert_not_called()

    def test_new_unlisted_covering_worm_or_changed_reviewed_range_cannot_be_waived(self):
        extra=self.table(dict(name='small-worm-turret',type='turret',unit_number=201,position={'x':2,'y':14},
            valid=True,prototype=dict(attack_parameters=dict(range=25))))
        extra.force=self.lua.globals().enemy;extra.surface=self.lua.globals().s
        self.lua.globals().worms=self.lua.table_from([self.foe,extra])
        self.assertFalse(execute_bastion_deployment(self.game,self.action)['ok'])
        self.assertEqual(self.lua.globals().built_calls,0)
        self.lua.globals().worms=self.lua.table_from([self.foe]);self.foe.prototype.attack_parameters.range=24
        self.assertFalse(execute_bastion_deployment(self.game,self.action)['ok'])
        self.assertEqual(self.lua.globals().built_calls,0)

    def test_unlisted_longer_range_worm_outside_small_worm_scan_still_blocks(self):
        p=self.action['position'];extra=self.table(dict(name='medium-worm-turret',type='turret',unit_number=201,
            position={'x':p['x']+29,'y':p['y']},valid=True,prototype=dict(attack_parameters=dict(range=30))))
        extra.force=self.lua.globals().enemy;extra.surface=self.lua.globals().s
        self.lua.globals().worms=self.lua.table_from([self.foe,extra])
        result=execute_bastion_deployment(self.game,self.action)
        self.assertEqual(result['reason'],'assault_new_or_changed_covering_worm');self.assertEqual(self.lua.globals().built_calls,0)


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeGroupDeploymentTests(DeploymentFixture):
    def setUp(self):
        NativeAssaultDeploymentTests.setUp(self)
        stage=self.document['stages'][0];stage['ammo_item']='piercing-rounds-magazine'
        stage['underfire_policy']['ammo_item']='piercing-rounds-magazine'
        for post in stage['posts']+stage['perimeter']['posts']:post['ammo_item']='piercing-rounds-magazine'
        self.assault_path.write_text(json.dumps(self.document))
        self.lua.execute('main=inventory({stack("gun-turret",3),stack("piercing-rounds-magazine",61,3),stack("firearm-magazine",7,2),stack(nil,0),stack(nil,0),stack(nil,0)})')
        self.group=bastion_group_deployment_action(self.game,self.obs,owner_kind='nest_assault');self.game.query.reset_mock()

    def test_three_red_posts_pay_and_arm_in_one_query_tick_without_touching_yellow_or_partial(self):
        before=self.assault_path.read_bytes();result=execute_bastion_group(self.game,self.group)
        self.assertTrue(result['ok'],result);self.game.query.assert_called_once()
        self.assertEqual((result['paid_turrets'],result['loaded_magazines'],result['loaded_rounds']),(3,60,600))
        self.assertEqual((result['ammo_item'],result['magazine_size'],result['ammo_category']),('piercing-rounds-magazine',10,'bullet'))
        self.assertTrue(result['same_tick']);self.assertEqual(result['start_tick'],result['armed_tick'])
        self.assertEqual(len(result['deployments']),3);self.assertEqual(self.lua.globals().built_calls,3)
        self.assertEqual({r['armed_tick']for r in result['deployments']},{100})
        self.assertEqual((self.lua.globals().main[2].count,self.lua.globals().main[2].ammo),(1,3))
        self.assertEqual((self.lua.globals().main[3].count,self.lua.globals().main[3].ammo),(7,2))
        self.assertEqual((result['before']['guns'],result['after']['guns']),(3,0));self.assertEqual(self.foe.health,200)
        self.assertEqual(self.assault_path.read_bytes(),before)
        global_registry=json.loads((self.root/'defense-perimeter.json').read_text())
        self.assertFalse(any(p.get('coverage_scope')==result['scope_id']for p in global_registry['posts']))

    def test_aggregate_funding_is_checked_before_any_gun_even_if_each_individual_can_be_funded(self):
        for change,restore in(('main[1].count=2','main[1].count=3'),('main[2].count=60','main[2].count=61')):
            self.lua.execute(change);result=execute_bastion_group(self.game,self.group)
            self.assertEqual(result['reason'],'bastion_group_three_guns_and_sixty_full_main_magazines_required')
            self.assertEqual(self.lua.globals().built_calls,0);self.assertIsNone(self.lua.globals().d.defense_deployments)
            self.assertIsNone(self.lua.globals().d.defense_deployment_groups);self.lua.execute(restore)

    def test_all_three_native_positions_are_guarded_before_first_payment(self):
        p=self.group['deployments'][2]['position'];self.lua.globals().block_x=p['x'];self.lua.globals().block_y=p['y']
        self.lua.execute('local old=s.can_place_entity;s.can_place_entity=function(q)return not(q.position.x==block_x and q.position.y==block_y)and old(q)end')
        result=execute_bastion_group(self.game,self.group)
        self.assertEqual((result['reason'],result['failed_index']),('bastion_site_changed_or_resource_overlap',3))
        self.assertTrue(result['preflight_failed']);self.assertEqual(self.lua.globals().main[1].count,3)
        self.assertEqual(self.lua.globals().built_calls,0);self.assertIsNone(self.lua.globals().d.defense_deployment_groups)

    def test_malformed_or_changed_stage_context_never_reaches_native_query(self):
        from factorio_agent.defense_deployment import _group_id,_hash,_generation
        for alter in('duplicate','partial','mixed_ammo','extra'):
            changed=deepcopy(self.group)
            if alter=='duplicate':changed['deployments'][2]=deepcopy(changed['deployments'][0])
            elif alter=='partial':changed['deployments'].pop()
            elif alter=='mixed_ammo':
                changed['deployments'][2]['ammo_item']='firearm-magazine'
                changed['deployments'][2]['deployment_id']=_hash(_generation(changed['deployments'][2]))
            else:changed['extra']=True
            changed['group_id']=_group_id(changed['deployments'])
            with self.subTest(alter=alter),self.assertRaises(ValueError):execute_bastion_group(self.game,changed)
        self.document['stages'][0]['underfire_policy']['deadline_tick']-=1;self.assault_path.write_text(json.dumps(self.document))
        with self.assertRaises(ValueError):execute_bastion_group(self.game,self.group)
        self.game.query.assert_not_called()

    def test_piercing_unlock_and_native_ammo_category_and_metadata_are_checked_before_payment(self):
        cases=(('f.recipes["piercing-rounds-magazine"].enabled=false','f.recipes["piercing-rounds-magazine"].enabled=true','bastion_piercing_recipe_not_unlocked'),
            ('prototypes.item["piercing-rounds-magazine"].ammo_category.name="rocket"','prototypes.item["piercing-rounds-magazine"].ammo_category.name="bullet"','bastion_native_prototypes_changed'),
            ('main[2].is_item_with_tags=true','main[2].is_item_with_tags=false','bastion_actor_ammo_metadata_unsupported'))
        for change,restore,reason in cases:
            self.lua.execute(change);result=execute_bastion_group(self.game,self.group)
            self.assertEqual(result['reason'],reason);self.assertEqual(self.lua.globals().built_calls,0)
            self.assertIsNone(self.lua.globals().d.defense_deployment_groups);self.lua.execute(restore)

    def test_completed_group_retry_uses_exact_paid_units_and_survives_binding_publication(self):
        result=execute_bastion_group(self.game,self.group);self.assertTrue(result['ok'],result)
        from factorio_agent.defense_deployment import _key
        stage=self.document['stages'][0]
        for q in result['deployments']:
            stage['bindings'][_key(dict(name='gun-turret',position=q['position']))]=q['unit_number']
            next(p for p in stage['perimeter']['posts']if p['name']==q['post_name'])['unit_number']=q['unit_number']
        self.assault_path.write_text(json.dumps(self.document));replay=execute_bastion_group(self.game,self.group)
        self.assertTrue(replay['reused_receipt'],replay);self.assertEqual((replay['paid_this_call'],replay['loaded_this_call']),(0,0))
        self.assertEqual(self.lua.globals().built_calls,3)
        self.lua.globals().turret(self.table(result['deployments'][1]['position']),9999)
        self.assertEqual(execute_bastion_group(self.game,self.group)['reason'],'paid_bastion_group_missing_or_replaced')
        self.assertEqual(self.lua.globals().built_calls,3)

    def test_interrupted_second_build_records_bounded_prefix_and_never_replays_payment(self):
        self.lua.execute('local old=s.create_entity;s.create_entity=function(q)local e=old(q);if built_calls==2 then error("injected second native build")end;return e end')
        result=execute_bastion_group(self.game,self.group)
        self.assertFalse(result['ok']);self.assertEqual(result['failed_index'],2)
        self.assertEqual(len(result['completed_prefix']),1);self.assertEqual(result['completed_prefix'][0]['loaded_magazines'],20)
        self.assertEqual(self.lua.globals().built_calls,2);self.assertEqual(self.lua.globals().main[1].count,1)
        marker=self.lua.globals().d.defense_deployment_groups[self.group['group_id']];self.assertEqual(marker.phase,'incomplete')
        self.assertEqual(execute_bastion_group(self.game,self.group)['reason'],'bastion_group_incomplete_intent_requires_recovery')
        self.assertEqual(self.lua.globals().built_calls,2)

    def test_group_native_source_rescan_survives_normal_inventory_sorting(self):
        self.lua.execute('auto_sort=true');result=execute_bastion_group(self.game,self.group)
        self.assertTrue(result['ok'],result);self.assertEqual(result['loaded_rounds'],600)
        self.assertEqual(self.lua.globals().main[1].name,'piercing-rounds-magazine')
        self.assertEqual((self.lua.globals().main[1].count,self.lua.globals().main[1].ammo),(1,3))
        self.assertNotIn('--',GROUP_DEPLOY_LUA)


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeWaveDeploymentTests(DeploymentFixture):
    def setUp(self):
        NativeAssaultDeploymentTests.setUp(self);self.install_wave(10)

    def install_wave(self,count):
        from factorio_agent.defense_deployment import _hash
        from factorio_ai.deterministic_builder import _plan
        old=self.document['stages'][0];posts=[dict(name='wave_'+str(i),position={'x':4*(i//2),'y':4*(i%2)},
            coverage_scope='sweep:one-wave',local_walk_access=True,ammo_item='piercing-rounds-magazine')for i in range(count)]
        entities=[dict(name='gun-turret',position=p['position'],direction=0)for p in posts]
        plan=_plan(entities,[],reserved_area=[[-1,-1],[4*((count-1)//2)+1,5]],clear_obstacles=[],clearance_world_id='w')
        scope=dict(old['scope'],new_posts=[p['name']for p in posts],covering_posts=[p['name']for p in posts])
        worm=dict(self.document['targets'][0],position={'x':14,'y':10})
        spawner=dict(worm,name='biter-spawner',type='unit-spawner',unit_number=201,position={'x':19,'y':2},reviewed_range=None)
        targets=[worm,spawner];target_hash=_hash(targets)
        wave=dict(world_id='w',catalog_fingerprint='c',phase='deploy',scope_id='sweep:one-wave',block='nest_sweep',
            plan=plan,posts=posts,scope=scope,hotspot={'x':8,'y':2},bindings={},ammo_item='piercing-rounds-magazine')
        policy=dict(mode='committed_projectile_wave_v2',operation_id='sweep-op',post_count=count,ammo_item='piercing-rounds-magazine',
            targets=targets,target_set_hash=target_hash,started_tick=100,deadline_tick=10900,max_enemy_range=25,coverage_margin=2,
            min_planned_neighbors=2,min_target_coverage=2,min_backstop_count=2,min_backstop_health_fraction=.5,min_backstop_full_mags=5,
            backstop_specs=deepcopy(old['underfire_policy']['backstop_specs']),planned_posts=deepcopy(posts),
            force_profile=dict(ammo_damage=0,turret_damage=0,gun_speed=0))
        wave['underfire_policy']=policy;wave['perimeter']=dict(world_id='w',catalog_fingerprint='c',posts=deepcopy(posts),
            coverage_scopes={wave['scope_id']:deepcopy(scope)})
        self.sweep=dict(world_id='w',catalog_fingerprint='c',actor_unit=11,id='sweep-op',phase='deploy',targets=targets,
            target_set_hash=target_hash,started_tick=100,deadline_tick=10900,wave=wave)
        self.sweep_path=self.root/'nest-sweep.json';self.sweep_path.write_text(json.dumps(self.sweep))
        board=json.loads((self.root/'pcb-board.json').read_text());board['blocks'].pop(old['block'],None);board['blocks']['nest_sweep']=plan
        (self.root/'pcb-board.json').write_text(json.dumps(board))
        self.lua.globals().world['small-worm-turret:0:14']=None;self.foe.position=self.table(worm['position'])
        self.lua.globals().world['small-worm-turret:14:10']=self.foe
        foe=self.table(dict(spawner,valid=True,health=350,max_health=350,prototype={}))
        foe.force=self.lua.globals().enemy;foe.surface=self.lua.globals().s;self.spawner=foe
        self.lua.globals().world['biter-spawner:19:2']=foe
        operation=dict(world_id='w',catalog_fingerprint='c',actor_unit=11,target_set_hash=target_hash,started_tick=100,deadline_tick=10900,
            posts={},deaths={},lost_posts={},loss_count=0,targets={str(q['unit_number']):q for q in targets})
        self.lua.globals().d.nest_assaults=self.table({'sweep-op':operation})
        self.lua.execute('main=inventory({stack("gun-turret",12),stack("piercing-rounds-magazine",100),stack("piercing-rounds-magazine",100),stack("piercing-rounds-magazine",41,3),stack("firearm-magazine",7,2),stack(nil,0)})')
        self.group=bastion_group_deployment_action(self.game,self.obs,owner_kind='nest_sweep');self.game.query.reset_mock()

    def assert_wave_paid(self,count):
        result=execute_bastion_group(self.game,self.group);self.assertTrue(result['ok'],result)
        self.game.query.assert_called_once();self.assertEqual(self.lua.globals().built_calls,count)
        self.assertEqual((result['paid_turrets'],result['loaded_magazines'],result['loaded_rounds']),(count,20*count,200*count))
        self.assertEqual(result['start_tick'],result['armed_tick']);self.assertEqual({q['armed_tick']for q in result['deployments']},{100})
        self.assertEqual(self.lua.globals().d.nest_assaults['sweep-op'].wave_group_id,self.group['group_id'])
        self.assertEqual((self.foe.health,self.spawner.health),(200,350))
        return result

    def test_ten_prepaid_guns_and_two_hundred_red_magazines_deploy_in_one_native_tick(self):self.assert_wave_paid(10)

    def test_six_post_lower_bound_keeps_same_tick_and_exact_costs(self):
        self.install_wave(6);self.assert_wave_paid(6)

    def test_twelve_post_upper_bound_keeps_partial_magazine_and_yellow_stock(self):
        self.install_wave(12);self.assert_wave_paid(12)
        self.assertEqual((self.lua.globals().main[4].count,self.lua.globals().main[4].ammo),(1,3))
        self.assertEqual((self.lua.globals().main[5].count,self.lua.globals().main[5].ammo),(7,2))

    def test_wave_cannot_use_external_single_or_unregistered_partial_group(self):
        self.game.query.reset_mock()
        with self.assertRaises(ValueError):execute_bastion_deployment(self.game,self.group['deployments'][0])
        partial=deepcopy(self.group);partial['deployments']=partial['deployments'][:6]
        from factorio_agent.defense_deployment import _group_id
        partial['group_id']=_group_id(partial['deployments'])
        with self.assertRaises(ValueError):execute_bastion_group(self.game,partial)
        self.game.query.assert_not_called()

    def test_last_wave_site_and_whole_stock_are_checked_before_any_debit(self):
        self.lua.execute('main[1].count=9');r=execute_bastion_group(self.game,self.group)
        self.assertEqual(r['reason'],'bastion_wave_all_guns_and_full_main_magazines_required');self.assertEqual(self.lua.globals().built_calls,0)
        self.lua.execute('main[1].count=12');p=self.group['deployments'][-1]['position']
        self.lua.globals().block_x=p['x'];self.lua.globals().block_y=p['y']
        self.lua.execute('local old=s.can_place_entity;s.can_place_entity=function(q)return not(q.position.x==block_x and q.position.y==block_y)and old(q)end')
        r=execute_bastion_group(self.game,self.group);self.assertEqual(r['failed_index'],10)
        self.assertEqual(self.lua.globals().built_calls,0);self.assertIsNone(self.lua.globals().d.defense_deployment_groups)
        self.assertIsNone(self.lua.globals().d.nest_assaults['sweep-op'].wave_group_id)

    def test_incomplete_wave_preserves_prefix_and_cannot_pay_or_launch_again(self):
        self.lua.execute('local old=s.create_entity;s.create_entity=function(q)local e=old(q);if built_calls==7 then error("seventh creation interruption")end;return e end')
        r=execute_bastion_group(self.game,self.group);self.assertFalse(r['ok']);self.assertEqual(r['failed_index'],7)
        self.assertEqual(len(r['completed_prefix']),6);self.assertEqual(self.lua.globals().built_calls,7)
        self.assertEqual(self.lua.globals().d.nest_assaults['sweep-op'].wave_group_id,self.group['group_id'])
        self.assertEqual(execute_bastion_group(self.game,self.group)['reason'],'bastion_group_incomplete_intent_requires_recovery')
        self.assertEqual(self.lua.globals().built_calls,7)

    def regroup(self):
        self.sweep_path.write_text(json.dumps(self.sweep));board=json.loads((self.root/'pcb-board.json').read_text())
        board['blocks']['nest_sweep']=self.sweep['wave']['plan'];(self.root/'pcb-board.json').write_text(json.dumps(board))
        self.group=bastion_group_deployment_action(self.game,self.obs,owner_kind='nest_sweep');self.game.query.reset_mock()

    def test_uncovered_spawner_blocks_entire_wave_before_first_material_cost(self):
        from factorio_agent.defense_deployment import _hash
        q=self.sweep['targets'][1];q['position']={'x':100,'y':2}
        self.sweep['target_set_hash']=_hash(self.sweep['targets']);policy=self.sweep['wave']['underfire_policy']
        policy['target_set_hash']=self.sweep['target_set_hash'];policy['targets']=deepcopy(self.sweep['targets'])
        self.lua.globals().world['biter-spawner:19:2']=None;self.spawner.position=self.table(q['position'])
        self.lua.globals().world['biter-spawner:100:2']=self.spawner
        self.lua.globals().d.nest_assaults['sweep-op'].target_set_hash=self.sweep['target_set_hash'];self.regroup()
        r=execute_bastion_group(self.game,self.group);self.assertEqual(r['reason'],'sweep_target_not_covered_by_two_planned_guns')
        self.assertEqual(self.lua.globals().built_calls,0);self.assertEqual(self.lua.globals().main[1].count,12)

    def test_real_rear_anchors_require_whole_wave_connectivity_before_payment(self):
        policy=self.sweep['wave']['underfire_policy']
        for i,q in enumerate(policy['backstop_specs']):
            old=q['position'];self.lua.globals().world[f"gun-turret:{old['x']}:{old['y']}"]=None
            q['position']={'x':-30,'y':4*i};e=self.lua.globals().turret(self.table(q['position']),q['unit_number'])
            e.inv[1]=self.lua.globals().stack('firearm-magazine',5)
        self.regroup();r=execute_bastion_group(self.game,self.group)
        self.assertEqual(r['reason'],'sweep_whole_wave_lacks_two_robust_rear_paths');self.assertEqual(self.lua.globals().built_calls,0)
        self.assertIsNone(self.lua.globals().d.defense_deployment_groups)

    def test_second_wave_lock_or_missing_native_listener_never_spends_stock(self):
        op=self.lua.globals().d.nest_assaults['sweep-op'];op.wave_group_id='another-paid-wave'
        r=execute_bastion_group(self.game,self.group);self.assertEqual(r['reason'],'sweep_operation_already_committed_to_another_wave')
        self.assertEqual(self.lua.globals().built_calls,0);op.wave_group_id=None
        self.lua.execute('_G.__factorio_nest_death_handler=nil')
        self.assertFalse(execute_bastion_group(self.game,self.group)['ok']);self.assertEqual(self.lua.globals().built_calls,0)

    def test_whole_wave_is_not_rejected_by_legacy_two_loss_stage_limit(self):
        self.lua.globals().d.nest_assaults['sweep-op'].loss_count=3;self.assert_wave_paid(10)

    def test_199_full_red_magazines_cannot_fund_ten_posts_even_with_partial_stock(self):
        self.lua.execute('main[3].count=59')
        r=execute_bastion_group(self.game,self.group)
        self.assertEqual(r['reason'],'bastion_wave_all_guns_and_full_main_magazines_required');self.assertEqual(self.lua.globals().built_calls,0)
        self.assertIsNone(self.lua.globals().d.nest_assaults['sweep-op'].wave_group_id)

    def test_forged_registered_count_actor_or_foreign_future_slot_cannot_reach_io(self):
        for change in('count','actor'):
            original=deepcopy(self.sweep)
            if change=='count':self.sweep['wave']['underfire_policy']['post_count']=13
            else:self.sweep['actor_unit']=12
            self.sweep_path.write_text(json.dumps(self.sweep))
            with self.subTest(change=change),self.assertRaises(ValueError):execute_bastion_group(self.game,self.group)
            self.sweep=original;self.sweep_path.write_text(json.dumps(original))
        board=json.loads((self.root/'pcb-board.json').read_text());p=self.group['deployments'][-1]['position']
        board['blocks']['reserved_future_factory']={'entities':[],'ports':[],'reserved_area':[[p['x']-1,p['y']-1],[p['x']+1,p['y']+1]]}
        (self.root/'pcb-board.json').write_text(json.dumps(board))
        with self.assertRaises(ValueError):execute_bastion_group(self.game,self.group)
        self.game.query.assert_not_called()


if __name__=='__main__':unittest.main()
