from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.construction_supply import V2Bootstrap
from factorio_agent.power_layout import V2FactoryBuilder


class DirectCellCleanupTests(unittest.TestCase):
    def setUp(self):
        self.bootstrap=V2Bootstrap.__new__(V2Bootstrap)
        self.obs={'world_id':'w'}
        self.build={'type':'build','name':'wooden-chest','position':{'x':5.5,'y':6.5},'direction':0}

    def test_direct_cell_uses_existing_conserved_floor_pickup_before_build(self):
        take=dict(type='take',name='item-on-ground',position={'x':5.4,'y':6.6},item='coal',quality='normal',count=1)
        self.bootstrap.cell_site_cleanup=Mock(return_value=take)
        with patch('factorio_agent.core.deterministic_bootstrap.DeterministicBootstrap._ensure_cell',return_value=self.build):
            result=self.bootstrap._ensure_cell(self.obs,'coal','wooden-chest')
        self.assertEqual(result,take)
        self.bootstrap.cell_site_cleanup.assert_called_once_with(self.obs,{'entities':[]},self.build)

    def test_unobstructed_build_and_nonbuild_results_are_preserved(self):
        self.bootstrap.cell_site_cleanup=Mock(return_value=None)
        for result in [None,{'type':'craft','recipe':'wooden-chest','count':1},self.build]:
            with self.subTest(result=result),patch('factorio_agent.core.deterministic_bootstrap.DeterministicBootstrap._ensure_cell',return_value=result):
                self.assertEqual(self.bootstrap._ensure_cell(self.obs,'coal','wooden-chest'),result)
        self.bootstrap.cell_site_cleanup.assert_called_once()

    def test_builder_binds_the_same_native_cleanup_for_direct_cells_and_saved_plans(self):
        with patch('factorio_agent.core.deterministic_builder.FactoryBuilder.__init__'):
            builder=V2FactoryBuilder(SimpleNamespace(),self.bootstrap,SimpleNamespace())
        self.assertEqual(self.bootstrap.cell_site_cleanup,builder._recover_resource_cell_obstruction)
