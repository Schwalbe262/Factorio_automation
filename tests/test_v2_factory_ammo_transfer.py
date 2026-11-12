from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.world_catalog import WorldCatalog
from factorio_agent.factory_ammo_transfer import (factory_magazine_action,execute_take_factory_magazines,
    TAKE_FACTORY_MAGAZINES_LUA)
import test_v2_ammo_transfer as fixtures

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


class FactoryTransferFixture(unittest.TestCase):
    def context(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        catalog=WorldCatalog({'version':'2.1.20'});self.fingerprint=catalog.fingerprint
        (self.root/'catalog.json').write_text(json.dumps(catalog.to_dict()))
        self.chest={'name':'iron-chest','position':{'x':.5,'y':.5},'direction':0}
        self.producer={'world_id':'w','catalog_fingerprint':self.fingerprint,'phase':'verified',
            'core':{'chest':deepcopy(self.chest)},'plan':{'entities':[deepcopy(self.chest)]},'bindings':{'iron-chest:0.5:0.5':7}}
        self.board={'world_id':'w','catalog_fingerprint':self.fingerprint,'blocks':{'piercing_ammo':deepcopy(self.producer['plan'])}}
        self.save();self.game.cfg=SimpleNamespace(runtime_dir=self.root)
        self.obs={'ok':True,'world_id':'w','actor_unit_number':11}
        self.action=factory_magazine_action(self.game,self.obs,5,[1])

    def save(self):
        (self.root/'piercing-ammo.json').write_text(json.dumps(self.producer))
        (self.root/'pcb-board.json').write_text(json.dumps(self.board))


class FactoryTransferEnvelopeTests(FactoryTransferFixture):
    def setUp(self):
        self.game=SimpleNamespace(expected_world='w',backend='assisted',query=Mock());self.context()

    def test_emitter_is_readonly_and_transfer_requires_exact_factory_frame(self):
        before={p.name:p.read_bytes()for p in self.root.glob('*.json')}
        self.assertEqual(factory_magazine_action(self.game,self.obs,5,[1]),self.action)
        self.assertEqual(before,{p.name:p.read_bytes()for p in self.root.glob('*.json')});self.game.query.assert_not_called()
        for field,value in(('phase','build'),('catalog_fingerprint','other'),('world_id','other')):
            old=deepcopy(self.producer);self.producer[field]=value;self.save()
            with self.assertRaises(ValueError):execute_take_factory_magazines(self.game,self.action)
            self.producer=old;self.save()
        self.producer['bindings']['iron-chest:0.5:0.5']=8;self.save()
        with self.assertRaises(ValueError):execute_take_factory_magazines(self.game,self.action)
        self.game.query.assert_not_called()

    def test_malformed_or_arbitrary_chest_envelope_is_rejected_before_native_io(self):
        for change in({'count':200},{'count':True},{'count':0},{'source_slots':[1,1]}, {'source_slots':[49]},
                       {'item':'firearm-magazine'},{'type':'craft'},{'position':{'x':float('nan'),'y':.5}},
                       {'expected_catalog_fingerprint':'other'},{'expected_actor_unit':False},
                       {'expected_entity_world_id':'other'},{'expected_entity_unit':8},{'extra':'field'}):
            with self.subTest(change=change),self.assertRaises(ValueError):execute_take_factory_magazines(self.game,{**self.action,**change})
        self.game.query.assert_not_called()

    def test_changed_plan_or_pinned_catalog_never_adopts_a_same_named_chest(self):
        self.board['blocks']['piercing_ammo']['foreign_revision']=True;self.save()
        with self.assertRaises(ValueError):execute_take_factory_magazines(self.game,self.action)
        self.board['blocks']['piercing_ammo']=deepcopy(self.producer['plan']);self.save()
        (self.root/'catalog.json').write_text(json.dumps(WorldCatalog({'version':'other'}).to_dict()))
        with self.assertRaises(ValueError):execute_take_factory_magazines(self.game,self.action)
        self.game.query.assert_not_called()


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeFactoryTransferTests(FactoryTransferFixture):
    def setUp(self):
        fixtures.NativeAmmoTransferTests.setUp(self)
        self.lua.execute('''prototypes.item["piercing-rounds-magazine"]={type="ammo",magazine_size=10}
local original=magazine;magazine=function(count,ammo,quality)local st=original(count,ammo,quality)
 st.name="piercing-rounds-magazine";st.prototype=prototypes.item[st.name];return st end
source[1]=magazine(10,10);chest.name="iron-chest"
''')
        self.context()

    def test_full_red_tails_transfer_without_refilling_either_partial_magazine(self):
        self.lua.execute('source[1]=magazine(3,7);dest[1]=magazine(2,4)')
        r=execute_take_factory_magazines(self.game,self.action)
        self.assertTrue(r['ok'],r);self.assertEqual(r['moved'],2);self.assertEqual(r['item'],'piercing-rounds-magazine')
        self.assertEqual(r['catalog_fingerprint'],self.fingerprint);self.assertEqual(r['rounds_before'],r['rounds_after'])
        self.assertEqual((self.lua.eval('source[1].count'),self.lua.eval('source[1].ammo')),(1,7))
        self.assertEqual((self.lua.eval('dest[1].count'),self.lua.eval('dest[1].ammo')),(4,4))

    def test_whole_selected_output_validates_before_any_material_moves(self):
        for change in('source[2]=magazine(1,10,"rare")','source[2]=magazine(1,10);source[2].name="firearm-magazine"',
                       'source[1].health=.5','a.unit_number=12','chest.unit_number=8','d.world_id="other"'):
            with self.subTest(change=change):
                self.setUp();self.lua.execute(change);r=execute_take_factory_magazines(self.game,self.action)
                self.assertFalse(r['ok']);self.assertEqual(self.lua.eval('source[1].count'),10)
                self.assertFalse(self.lua.eval('dest[1].valid_for_read'))

    def test_aggregate_transfer_is_limited_by_request_and_actual_actor_capacity(self):
        self.lua.execute('source[1]=magazine(100,10);capacity=2')
        r=execute_take_factory_magazines(self.game,{**self.action,'count':50})
        self.assertEqual(r['moved'],2);self.assertEqual(self.lua.eval('source[1].count'),98)
        self.assertEqual(execute_take_factory_magazines(self.game,self.action)['moved'],0)
        self.assertEqual(self.lua.eval('source[1].count'),98)

    def test_one_partial_red_and_unsupported_actor_metadata_never_get_normalized(self):
        self.lua.execute('source[1]=magazine(1,1)');r=execute_take_factory_magazines(self.game,self.action)
        self.assertEqual(r['moved'],0);self.assertEqual(self.lua.eval('source[1].ammo'),1)
        self.lua.execute('source[1]=magazine(10,10);dest[1]=magazine(1,3);dest[1].health=.5')
        self.assertFalse(execute_take_factory_magazines(self.game,self.action)['ok'])
        self.assertEqual((self.lua.eval('dest[1].count'),self.lua.eval('dest[1].ammo')),(1,3))
        self.assertNotIn('--',TAKE_FACTORY_MAGAZINES_LUA)


if __name__=='__main__':unittest.main()
