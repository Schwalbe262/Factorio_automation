from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.defense_perimeter import DefensePerimeter
from factorio_agent.defense_hardening import (DefenseHardening,wall_bastion,mutual_coverage,_key,
    HARDENING_PROOF_LUA,SERVICE_ACCESS_LUA,BASTION_SURVEY_LUA)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


class BastionGeometryTests(unittest.TestCase):
    def test_three_sides_have_eight_walls_and_open_rear_in_every_direction(self):
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        for facing in(0,4,8,12):
            bastion=wall_bastion({'x':0,'y':0},facing);es=bastion['entities']
            self.assertEqual(sum(e['name']=='stone-wall'for e in es),8)
            boxes=[geometry._occupied_by_plan([e])for e in es]
            self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
            self.assertTrue(all((p['x'],p['y'])not in set().union(*boxes)for p in bastion['service_points']))

    def test_wall_corners_and_hotspot_require_two_independent_mutually_covering_guns(self):
        group=[wall_bastion({'x':x,'y':4})for x in(-4,0,4)]
        self.assertTrue(mutual_coverage(group,{'x':0,'y':0},16))
        self.assertFalse(mutual_coverage(group[:1],{'x':0,'y':0},16))
        self.assertFalse(mutual_coverage([group[0],wall_bastion({'x':20,'y':4})],{'x':0,'y':0},16))
        self.assertFalse(mutual_coverage([group[0],deepcopy(group[0])],{'x':0,'y':0},16))
        for p in({'x':float('nan'),'y':0},{'x':.5,'y':0},{'x':True,'y':0}):
            with self.assertRaises(ValueError):wall_bastion(p)


class HardeningControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.catalog=SimpleNamespace(fingerprint='c',recipes={'stone-wall':dict(ingredients=[dict(name='stone-brick',amount=5,type='item')],
            products=[dict(name='stone-wall',amount=1,type='item')])})
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),query=Mock(return_value={'ok':True,'count':0,'tick':4}))
        self.bootstrap=SimpleNamespace(ensure_item=Mock(side_effect=lambda obs,item,count:dict(type='craft',recipe=item,count=count)))
        self.builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
            ensure_plan=Mock(return_value={'type':'craft','recipe':'gun-turret','count':1}))
        self.perimeter=DefensePerimeter.__new__(DefensePerimeter);self.perimeter.game=self.game;self.perimeter.catalog=self.catalog
        self.perimeter.path=self.root/'defense-perimeter.json';self.perimeter._ensure_magazines=Mock(return_value={'type':'take_full_magazines'})
        self.original=dict(name='plastic',position={'x':0,'y':0},commissioned=False,coverage_scope='plastic',
            lost_unit_number=11,lost_unit_numbers=[10,11])
        self.perimeter.state=dict(world_id='w',catalog_fingerprint='c',posts=[deepcopy(self.original)],coverage_scopes={},range=18)
        self.perimeter._save();self.full={};self.actor_full=60;self.clock=1;self.obs=dict(ok=True,world_id='w',tick=4,inventory={'gun-turret':3},
            enabled_recipes={n:True for n in('gun-turret','firearm-magazine','stone-wall')},entities=[])
        def inspect(obs):
            actual={tuple(e['position'].values()):e for e in obs['entities']};rows=[]
            for p in self.perimeter.state['posts']:
                e=actual.get(tuple(p['position'].values()));unit=e['unit_number']if e else None
                rows.append(dict(name=p['name'],position=p['position'],unit_number=unit,missing=not bool(e),identity_changed=False,
                    ammo=self.full.get(unit,0),full_magazines=self.full.get(unit,0),nearby_enemies=9 if p['name']=='plastic'else 0))
            return dict(ok=True,tick=4,posts=rows,changed=[],actor_full_magazines=self.actor_full)
        self.perimeter.inspect=Mock(side_effect=inspect)
        self.board=dict(world_id='w',catalog_fingerprint='c',blocks={});(self.root/'pcb-board.json').write_text(json.dumps(self.board))
        self.reserved=patch('factorio_agent.defense_hardening.protected_factory_areas',return_value=[]).start()
        patch('factorio_agent.defense_hardening.native_candidate_survey',return_value=dict(ok=True,range=18,
            candidates=[dict(position={'x':x,'y':4})for x in(-4,0,4)],magazine_size=10)).start()
        self.preflight=patch('factorio_agent.defense_hardening.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        def query(body):
            if 'native_bastion_footprints_changed'in body:return dict(ok=True,candidates=[wall_bastion({'x':x,'y':4})for x in(-4,0,4)])
            if 'hardening_owned_asset_missing_or_changed'in body:return dict(ok=True,tick=4,
                posts=[dict(unit_number=p['unit_number'],full_magazines=self.full.get(p['unit_number'],0))
                       for p in self.perimeter.state['posts']if p.get('coverage_scope','').startswith('hardening:')and p.get('unit_number')])
            return dict(ok=True,count=0,tick=4)
        self.game.query.side_effect=query;self.addCleanup(patch.stopall)
        self.c=DefenseHardening(self.game,self.bootstrap,self.builder,self.catalog,self.perimeter)

    def install(self):
        ready=self.c.prepare(self.obs);self.assertTrue(ready['ok'],ready);self.c.state=ready;self.c._save();return ready

    def receipt(self,action,result,key='hardening'):
        self.clock+=1
        rows=[dict(key=key,event='action_intent',tick=self.clock,action=action),
              dict(key=key,event='action_result',tick=self.clock,result=dict(ok=True,**result))]
        with(self.root/'job-events.jsonl').open('a')as f:
            for row in rows:f.write(json.dumps(row)+'\n')

    def test_prepare_is_readonly_additive_and_strength_follows_observed_losses(self):
        before=self.perimeter.path.read_bytes();board=(self.root/'pcb-board.json').read_bytes()
        with patch('factorio_agent.defense_hardening._atomic_json')as write:ready=self.c.prepare(self.obs);write.assert_not_called()
        self.assertTrue(ready['ok'],ready);self.assertEqual(ready['requirements'],
            {'gun-turret':3,'firearm-magazine':60,'stone-wall':24,'stone-brick':120})
        self.assertEqual(self.perimeter.path.read_bytes(),before);self.assertEqual((self.root/'pcb-board.json').read_bytes(),board)
        self.assertEqual(ready['scope']['new_posts'],ready['scope']['covering_posts'])
        self.assertNotIn('plastic',ready['scope']['covering_posts']);self.assertEqual(self.c.state,{})

    def test_research_and_unproven_hotspots_block_without_construction(self):
        self.obs['enabled_recipes']['stone-wall']=False
        self.assertEqual(self.c.prepare(self.obs)['reason'],'native_turret_magazine_and_stone_wall_research_required')
        self.obs['enabled_recipes']['stone-wall']=True;self.perimeter.state['posts'][0].pop('lost_unit_number')
        self.perimeter.state['posts'][0]['lost_unit_numbers']=[]
        self.assertEqual(self.c.prepare(self.obs)['reason'],'no_observed_defense_loss_or_damage_hotspot')
        self.builder.ensure_plan.assert_not_called()

    def test_all_missing_turrets_and_sixty_full_magazines_are_prepaid_before_first_build(self):
        self.install();self.obs['inventory']['gun-turret']=0
        action=self.c.step(self.obs);self.assertEqual((action['type'],action['recipe'],action['count']),('craft','gun-turret',3))
        self.builder.ensure_plan.assert_not_called()
        self.obs['inventory']['gun-turret']=3;self.actor_full=0
        self.assertEqual(self.c.step(self.obs)['type'],'take_full_magazines')
        self.assertEqual(self.perimeter._ensure_magazines.call_args.args[2],60)
        self.builder.ensure_plan.assert_not_called();self.actor_full=60
        self.c.step(self.obs);self.builder.ensure_plan.assert_called_once()
        self.assertEqual(self.c.state['deployment_prepayment']['full_magazines'],60)

    def test_factory_reservations_and_native_resource_collision_fail_closed(self):
        self.reserved.return_value=[[[-100,-100],[100,100]]]
        self.game.query.side_effect=lambda body:dict(ok=True,candidates=[],count=0)
        self.assertEqual(self.c.prepare(self.obs)['reason'],'no_finite_ore_free_mutual_bastions')
        self.builder.ensure_plan.assert_not_called()

    def test_posts_build_and_arm_before_any_wall_while_old_hotspot_is_missing(self):
        ready=self.install();self.c.step(self.obs)
        first=self.builder.ensure_plan.call_args.args[1]
        self.assertTrue(all(e['name']=='gun-turret'for e in first['entities']))
        self.assertEqual(len(first['entities']),1)
        self.assertEqual(self.perimeter.state['posts'][0],self.original)
        for i in range(3):
            if i:
                self.c.step(self.obs);first=self.builder.ensure_plan.call_args.args[1]
                self.assertEqual(len(first['entities']),1);self.assertEqual(first['entities'][0]['name'],'gun-turret')
            self.obs['entities'].append(dict(first['entities'][0],unit_number=100+i))
            self.receipt(dict(type='build',**first['entities'][0]),dict(unit_number=100+i))
            self.builder.ensure_plan.reset_mock();action=self.c.step(self.obs)
            self.assertEqual(action['type'],'insert');self.assertEqual(action['count'],20)
            self.builder.ensure_plan.assert_not_called();self.full[100+i]=10
            self.receipt(action,dict(moved=20));self.full[100+i]=19
        self.c.step(self.obs)
        self.assertTrue(any(e['name']=='stone-wall'for e in self.builder.ensure_plan.call_args.args[1]['entities']))
        self.assertTrue(self.perimeter.state['coverage_scopes'][ready['scope_id']]['commissioned'])
        self.assertEqual(self.perimeter.state['posts'][0],self.original)
        for unit in self.full:self.full[unit]=12
        self.builder.ensure_plan.reset_mock();self.c.step(self.obs)
        self.assertTrue(any(e['name']=='stone-wall'for e in self.builder.ensure_plan.call_args.args[1]['entities']))

    def test_bound_asset_replacement_and_board_revision_never_silently_adopt(self):
        self.install();self.c.step(self.obs);gun=next(e for e in self.c.state['plan']['entities']if e['name']=='gun-turret')
        self.c.state['bindings'][_key(gun)]=100;self.c._save()
        self.receipt(dict(type='build',**gun),dict(unit_number=100))
        self.obs['entities']=[dict(gun,unit_number=101)]
        self.assertEqual(self.c.step(self.obs)['reason'],'hardening_replacement_requires_exact_registered_paid_repair')
        board=json.loads((self.root/'pcb-board.json').read_text());board['blocks'][self.c.state['block']]['layout_policy']='foreign'
        (self.root/'pcb-board.json').write_text(json.dumps(board))
        self.assertEqual(self.c.step(self.obs)['reason'],'hardening_registered_plan_changed')

    def test_reused_build_receipts_are_not_payment_evidence_fresh_or_cached(self):
        self.install();gun=next(e for e in self.c.state['plan']['entities']if e['name']=='gun-turret')
        self.receipt(dict(type='build',**gun),dict(unit_number=100,reused=True))
        rows=self.c._receipts();self.assertIsNone(self.c._paid_build(100,gun,rows))
        self.c.state['unit_build_receipts']={'100':rows[0]}
        self.assertIsNone(self.c._paid_build(100,gun,[]))
        self.receipt(dict(type='build',**gun),dict(unit_number=101))
        paid=self.c._paid_build(101,gun,self.c._receipts());self.assertEqual(paid['result']['unit_number'],101)

    def atomic_birth(self,post,unit,actor=11):
        from factorio_agent.defense_deployment import bastion_deployment_action,_hash,SCOPE_FIELDS
        self.game.expected_world='w';self.game.backend='assisted';obs=dict(self.obs,actor_unit_number=actor)
        action=bastion_deployment_action(self.game,obs,post['name'])
        result=dict(ok=True,unit_number=unit,world_id='w',catalog_fingerprint='c',actor_unit=actor,
            position=post['position'],paid_turrets=1,loaded_magazines=20,loaded_rounds=200,paid_this_call=1,loaded_this_call=20,
            ammo_item='firearm-magazine',magazine_size=10,ammo_category='bullet',quality='normal',start_tick=4,armed_tick=4,
            scope_revision=_hash({k:self.c.state['scope'][k]for k in SCOPE_FIELDS}))
        result.update({k:action[k]for k in('deployment_id','owner_kind','authority_revision','plan_revision','scope_id','post_name')})
        return action,result

    def test_atomic_paid_birth_and_consumed_delivery_preserve_original_actor_and_deduplicate(self):
        self.install();self.c.step(self.obs);post=self.c.state['posts'][0];action,result=self.atomic_birth(post,100)
        self.receipt(action,{k:v for k,v in result.items()if k!='ok'})
        rows=self.c._receipts();spec=dict(post,name='gun-turret');paid=self.c._paid_build(100,spec,rows)
        self.assertTrue(paid['atomic_marker_verified']);self.assertEqual(paid['result']['actor_unit'],11)
        self.c.state.setdefault('unit_build_receipts',{})['100']=paid;self.obs['actor_unit_number']=10915
        self.game.query.reset_mock();self.assertEqual(self.c._paid_build(100,spec,[])['result']['actor_unit'],11)
        self.game.query.assert_not_called()
        self.c._observe_delivery(post,100,0,rows+rows)
        self.assertEqual(sum(q['moved']for q in self.c.state['stock_delivery_receipts']['100'].values()),20)
        self.assertEqual(self.c.state['stocked_units'][post['name']],100)

    def test_atomic_receipt_requires_original_payment_and_native_marker(self):
        self.install();self.c.step(self.obs);post=self.c.state['posts'][0];action,result=self.atomic_birth(post,100)
        row=dict(action=action,result=result,key='watch',tick=4,intent_sha256='a',result_sha256='b')
        for change in({'reused':True},{'reused_receipt':True},{'paid_this_call':0},{'actor_unit':12},{'loaded_magazines':19},{'unit_number':101},{'owner_kind':'nest_assault'}):
            altered=dict(row,result=dict(result,**change))
            self.assertIsNone(self.c._paid_build(100,dict(post,name='gun-turret'),[altered]))
        self.game.query.return_value={'ok':False};self.game.query.side_effect=None
        self.assertIsNone(self.c._paid_build(100,dict(post,name='gun-turret'),[row]))

    def test_atomic_group_children_retain_paired_evidence_and_each_get_twenty_delivery(self):
        self.install();self.c.step(self.obs)
        pairs=[self.atomic_birth(p,100+i)for i,p in enumerate(self.c.state['posts'])]
        self.receipt(dict(type='build_and_arm_bastion_group',group_id='g',deployments=[a for a,r in pairs]),
            dict(paid_this_call=3,deployments=[r for a,r in pairs]))
        rows=self.c._receipts()
        for i,post in enumerate(self.c.state['posts']):
            paid=self.c._paid_build(100+i,dict(post,name='gun-turret'),rows)
            self.assertEqual(paid['atomic_group_id'],'g')
            self.c._observe_delivery(post,100+i,0,rows)
            self.assertEqual(sum(q['moved']for q in self.c.state['stock_delivery_receipts'][str(100+i)].values()),20)

    def test_known_paid_watch_replacement_records_lost_unit_before_binding_new_unit(self):
        self.install();self.c.step(self.obs);gun=next(e for e in self.c.state['plan']['entities']if e['name']=='gun-turret')
        self.c.state['bindings'][_key(gun)]=100;self.c._save()
        self.receipt(dict(type='build',**gun),dict(unit_number=100))
        self.receipt(dict(type='build',**gun),dict(unit_number=200),key='watch')
        post=next(p for p in self.perimeter.state['posts']if p['position']==gun['position']);post.update(unit_number=200,commissioned=True)
        self.perimeter._save();self.obs['entities']=[dict(gun,unit_number=200)];self.full[200]=15
        before=self.c.path.read_bytes();preview=self.c.prepare_recovery(self.obs)
        self.assertTrue(preview['ok'],preview);self.assertEqual(self.c.path.read_bytes(),before)
        self.assertEqual(preview['recoveries'][0]['old_unit'],100)
        self.c.step(self.obs)
        self.assertEqual(self.c.state['bindings'][_key(gun)],200)
        self.assertEqual(self.c.state['loss_history'][-1]['old_unit'],100)
        disk=json.loads(self.perimeter.path.read_text());updated=next(p for p in disk['posts']if p['name']==post['name'])
        self.assertIn(100,updated['lost_unit_numbers']);self.assertEqual(updated['lost_unit_origin'],100)

    def test_native_confirmed_known_loss_is_recorded_before_paid_rebuild(self):
        self.install();self.c.step(self.obs);gun=next(e for e in self.c.state['plan']['entities']if e['name']=='gun-turret')
        self.c.state['bindings'][_key(gun)]=100;self.c._save();self.receipt(dict(type='build',**gun),dict(unit_number=100))
        post=next(p for p in self.perimeter.state['posts']if p['position']==gun['position']);post['unit_number']=100;self.perimeter._save()
        self.builder.ensure_plan.reset_mock();self.c.step(self.obs)
        self.assertNotIn(_key(gun),self.c.state['bindings'])
        self.assertEqual(self.c.state['loss_history'][-1]['old_unit'],100)
        post=next(p for p in self.perimeter.state['posts']if p['position']==gun['position'])
        self.assertNotIn('unit_number',post);self.assertIn(100,post['lost_unit_numbers'])
        self.assertEqual(self.builder.ensure_plan.call_args.args[1]['entities'],[gun])

    def canonical_missing_guns(self,prove_all=True):
        self.install();self.c.step(self.obs)
        guns=[e for e in self.c.state['plan']['entities']if e['name']=='gun-turret']
        self.c.state['bindings'][_key(guns[0])]=100;self.c._save()
        self.receipt(dict(type='build',**guns[0]),dict(unit_number=100))
        for i,gun in enumerate(guns):
            post=next(p for p in self.perimeter.state['posts']if p['position']==gun['position'])
            post.update(unit_number=200+i,commissioned=True)
            if prove_all or i!=1:self.receipt(dict(type='build',**gun),dict(unit_number=200+i),key='watch')
        self.perimeter._save();return guns

    def test_dead_paid_intermediate_and_unbound_canonical_units_clear_before_paid_rebuild(self):
        guns=self.canonical_missing_guns();self.builder.ensure_plan.reset_mock()
        self.c.step(self.obs)
        self.assertFalse(any(_key(gun)in self.c.state['bindings']for gun in guns))
        losses=self.c.state['loss_history']
        self.assertTrue({100,200,201,202}<={p['old_unit']for p in losses})
        disk=json.loads(self.perimeter.path.read_text())
        for i,gun in enumerate(guns):
            post=next(p for p in disk['posts']if p['position']==gun['position'])
            self.assertNotIn('unit_number',post);self.assertEqual(post['lost_unit_number'],200+i)
            self.assertIn(200+i,post['lost_unit_numbers']);self.assertFalse(post['commissioned'])
        self.assertEqual(self.builder.ensure_plan.call_args.args[1]['entities'],[guns[0]])

    def test_every_missing_canonical_receipt_and_native_absence_precede_any_metadata(self):
        self.canonical_missing_guns(prove_all=False)
        before=self.c.path.read_bytes();perimeter=self.perimeter.path.read_bytes();self.game.query.reset_mock()
        result=self.c.step(self.obs)
        self.assertEqual(result['reason'],'hardening_missing_canonical_paid_build_proof_required')
        self.assertEqual(self.c.path.read_bytes(),before);self.assertEqual(self.perimeter.path.read_bytes(),perimeter)
        self.game.query.assert_not_called()

    def existing_guns(self,stocks=(7,5,1),walls=False):
        self.install();self.c.step(self.obs)
        entities=self.c.state['plan']['entities']if walls else[e for e in self.c.state['plan']['entities']if e['name']=='gun-turret']
        for i,e in enumerate(entities):
            unit=100+i;self.obs['entities'].append(dict(e,unit_number=unit));self.receipt(dict(type='build',**e),dict(unit_number=unit))
            if e['name']=='gun-turret':
                post=next(p for p in self.perimeter.state['posts']if p['position']==e['position'])
                post.update(unit_number=unit,commissioned=True);self.full[unit]=stocks[len(self.full)]
            if walls:self.c.state['bindings'][_key(e)]=unit
        self.perimeter._save();self.c._save()

    def test_functional_guns_allow_paid_walls_before_reserve_but_never_false_commission(self):
        self.existing_guns();self.builder.ensure_plan.reset_mock()
        action=self.c.step(self.obs)
        self.assertTrue(any(e['name']=='stone-wall'for e in self.builder.ensure_plan.call_args.args[1]['entities']))
        self.assertNotEqual(action.get('status'),'succeeded')
        self.assertFalse(self.perimeter.state['coverage_scopes'][self.c.state['scope_id']]['commissioned'])
        self.assertFalse(self.c.state.get('stocked_posts'))

    def test_verified_wall_upkeep_records_known_loss_and_requires_paid_replacement(self):
        self.existing_guns(stocks=(12,12,12),walls=True);self.c.state['phase']='verified';self.c._save()
        wall=next(e for e in self.obs['entities']if e['name']=='stone-wall');old=wall['unit_number']
        self.obs['entities'].remove(wall);self.builder.ensure_plan.reset_mock();self.c.upkeep(self.obs)
        self.assertEqual(self.c.state['wall_rebuild_intent']['old_unit'],old)
        self.assertNotIn(_key(wall),self.c.state['bindings'])
        replacement=dict(wall,unit_number=9000);self.obs['entities'].append(replacement)
        self.assertEqual(self.c.upkeep(self.obs)['reason'],'hardening_wall_rebuild_paid_receipt_missing')
        spec=next(e for e in self.c.state['plan']['entities']if _key(e)==_key(wall))
        self.receipt(dict(type='build',**spec),dict(unit_number=9000))
        self.assertEqual(self.c.upkeep(self.obs)['reason'],'normally_paid_wall_rebuild_bound_reobserve')
        self.assertEqual(self.c.state['bindings'][_key(wall)],9000)

    def test_damaged_known_wall_delegates_only_to_parent_normal_repair_callback(self):
        self.existing_guns(stocks=(12,12,12),walls=True);self.c.state['phase']='verified';self.c._save()
        wall=next(e for e in self.obs['entities']if e['name']=='stone-wall');wall.update(health=50,max_health=100)
        self.c.wall_repair=Mock(return_value={'type':'repair'})
        self.assertEqual(self.c.upkeep(self.obs),{'type':'repair'})
        self.assertEqual(self.c.wall_repair.call_args.args[2],wall['unit_number'])


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeHardeningTests(unittest.TestCase):
    def setUp(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(a)for a in v])
            return v
        self.table=table
        self.lua.execute('''game={tick=1};f={};s={};enemy={};defines={inventory={turret_ammo=1}}
prototypes={item={["firearm-magazine"]={magazine_size=10}}};world={}
function target(p,name)return world[name..":"..p.x..":"..p.y]end
function make(q)
 local e={valid=true,name=q.name,position=q.position,unit_number=q.unit_number,quality={name="normal"},force=f,surface=s,health=400,max_health=400}
 local st={valid_for_read=true,name="firearm-magazine",count=10,ammo=10,quality={name="normal"},health=1,spoil_tick=0,
 is_item_with_tags=false,is_item_with_inventory=false,is_item_with_entity_data=false};e.inv={st};e.get_inventory=function()return e.inv end
 world[q.name..":"..q.position.x..":"..q.position.y]=e;return e end
''')
        self.entities=[dict(name='gun-turret',position={'x':0,'y':0},unit_number=1),
            dict(name='stone-wall',position={'x':1.5,'y':.5},unit_number=2)]
        for e in self.entities:self.lua.globals().make(table(e))

    def run_proof(self):
        self.lua.globals().x=self.table(dict(entities=self.entities,minimum_full=10,initial=True))
        return dict(self.lua.execute('return(function() '+' '.join(HARDENING_PROOF_LUA.splitlines())+' end)()').items())

    def test_native_full_normal_ammo_and_exact_foreign_wall_identity(self):
        self.assertTrue(self.run_proof()['ok'])
        wall=self.lua.globals().world['stone-wall:1.5:0.5'];wall.force=self.lua.globals().enemy
        self.assertEqual(self.run_proof()['reason'],'hardening_owned_asset_missing_or_changed');wall.force=self.lua.globals().f
        gun=self.lua.globals().world['gun-turret:0:0'];gun.inv[1].ammo=5
        self.assertEqual(self.run_proof()['reason'],'hardening_post_full_normal_magazines_or_health_missing')
        gun.inv[1].ammo=10;gun.inv[1].is_item_with_tags=True
        self.assertEqual(self.run_proof()['reason'],'hardening_magazine_metadata_or_type_changed')

    def test_native_damaged_functional_gun_can_commission_but_dead_gun_cannot(self):
        gun=self.lua.globals().world['gun-turret:0:0'];gun.health=15
        result=self.run_proof();self.assertTrue(result['ok'],result)
        row=result['posts'][1];self.assertEqual(row.health,15);self.assertTrue(row.damaged)
        gun.health=0
        self.assertEqual(self.run_proof()['reason'],'hardening_owned_asset_missing_or_changed')

    def test_native_scripts_flatten_without_comments_or_health_ammo_enemy_setters(self):
        for body in(HARDENING_PROOF_LUA,SERVICE_ACCESS_LUA,BASTION_SURVEY_LUA):
            self.assertNotIn('--',body)
            self.assertNotIn('destroy(',body);self.assertNotIn('.damage(',body)
            self.assertNotIn('.health=',body);self.assertNotIn('.ammo=',body)


if __name__=='__main__':unittest.main()
