from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_ai.deterministic_state import _atomic_json
from factorio_agent.capacity_profiles import refresh_bus_capacity
from factorio_agent.main_bus import BusMaterial, ConsumerTap, plan_main_bus


class CapacityProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root))
        self.catalog=SimpleNamespace(fingerprint='f',entities={
            'stone-furnace':{'crafting_speed':1},'steel-furnace':{'crafting_speed':2}},
            recipes={'iron-plate':{'energy':3.2,'products':[{'name':'iron-plate','amount':1}]}})
        self.bus=plan_main_bus([BusMaterial('iron-plate',-11.5,(225,),2)],
            [ConsumerTap('science','iron-plate',-12.5,'critical',demand_per_minute=264.5)],
            north_y=-20.5,south_y=10.5,port_x=12.5)
        self.assertTrue(self.bus['ok'])
        furnaces=[{'name':'steel-furnace' if i<8 else 'stone-furnace',
                   'position':{'x':i*4,'y':0}} for i in range(12)]
        self.source={'world_id':'w','catalog_fingerprint':'f','banks':[
            {'resource':'iron-ore','product':'iron-plate','entities':furnaces}]}
        self.factory={'world_id':'w','catalog_fingerprint':'f','bus':deepcopy(self.bus),'unrelated':[1,2]}
        self.board={'world_id':'w','catalog_fingerprint':'f','shared_main_bus':deepcopy(self.bus),'blocks':{}}
        for name,value in [('smelting-district.json',self.source),('bus-factory.json',self.factory),('pcb-board.json',self.board)]:
            _atomic_json(self.root/name,value)
        self.component=SimpleNamespace(path=self.root/'bus-factory.json',state=deepcopy(self.factory))

    def test_verified_type_budget_refresh_changes_only_derived_profiles_and_is_idempotent(self):
        before=(self.root/'smelting-district.json').read_bytes()
        result=refresh_bus_capacity(self.game,self.catalog,self.component)
        self.assertTrue(result['ok']);self.assertTrue(result['changed'])
        line=result['capacity']['materials']['iron-plate']['lines'][0]
        self.assertEqual(line['declared_source_per_minute'],375)
        self.assertEqual(line['producer_deficit_per_minute'],0)
        factory=json.loads(self.component.path.read_text());board=json.loads((self.root/'pcb-board.json').read_text())
        self.assertEqual(factory['bus']['entities'],self.bus['entities'])
        self.assertEqual(factory['bus']['ports'],self.bus['ports'])
        self.assertEqual(factory['unrelated'],[1,2])
        self.assertEqual(factory['bus'],board['shared_main_bus'])
        self.assertEqual((self.root/'smelting-district.json').read_bytes(),before)
        with patch('factorio_agent.capacity_profiles._atomic_json') as write:
            self.assertFalse(refresh_bus_capacity(self.game,self.catalog,self.component)['changed'])
            write.assert_not_called()

    def test_interrupted_profile_only_write_recovers_without_replacing_layout(self):
        def interrupted(path,data):
            if Path(path).name=='pcb-board.json':raise OSError('interrupted metadata publication')
            _atomic_json(path,data)
        with patch('factorio_agent.capacity_profiles._atomic_json',side_effect=interrupted):
            with self.assertRaises(OSError):refresh_bus_capacity(self.game,self.catalog,self.component)
        result=refresh_bus_capacity(self.game,self.catalog,self.component)
        self.assertTrue(result['ok'])
        factory=json.loads(self.component.path.read_text());board=json.loads((self.root/'pcb-board.json').read_text())
        self.assertEqual(factory['bus'],board['shared_main_bus'])

    def test_different_geometry_or_world_blocks_without_any_write(self):
        self.board['shared_main_bus']['entities'][0]['direction']=4
        _atomic_json(self.root/'pcb-board.json',self.board)
        with patch('factorio_agent.capacity_profiles._atomic_json') as write:
            self.assertEqual(refresh_bus_capacity(self.game,self.catalog,self.component)['reason'],
                             'capacity_profile_bus_contracts_differ')
            write.assert_not_called()
        self.board['world_id']='another-world';_atomic_json(self.root/'pcb-board.json',self.board)
        with patch('factorio_agent.capacity_profiles._atomic_json') as write:
            self.assertEqual(refresh_bus_capacity(self.game,self.catalog,self.component)['reason'],
                             'capacity_profile_world_or_catalog_changed')
            write.assert_not_called()
