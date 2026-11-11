from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest

from factorio_agent.diagnostics import production_rates,construction_budget


class RateEvidenceTests(unittest.TestCase):
    def test_lifetime_counter_requires_two_timed_samples_and_rollback_resets(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            o={'world_id':'w','tick':10000,'production':{'iron-plate':{'produced':5000,'consumed':4000}}}
            self.assertEqual(production_rates(root,o)['status'],'sampling')
            o={'world_id':'w','tick':13600,'production':{'iron-plate':{'produced':5060,'consumed':4030}}}
            r=production_rates(root,o)
            self.assertEqual(r['rates_per_game_minute']['iron-plate'],{'produced':60,'consumed':30})
            o['tick']=9000
            r=production_rates(root,o)
            self.assertEqual(r['status'],'sampling')
            self.assertEqual(r['reset_reason'],'world_rollback')

    def test_pending_mining_plan_is_included_once_in_known_project_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            drill={'name':'electric-mining-drill','position':{'x':.5,'y':.5}}
            plan={'entities':[drill]}
            (root/'mining-outposts.json').write_text(json.dumps({'world_id':'w','banks':[{'plan':plan},{'plan':plan}]}))
            catalog=SimpleNamespace(recipe_for_product=lambda _: {'products':[{'amount':1}],
                'ingredients':[{'name':'iron-plate','amount':23}]})
            obs={'world_id':'w','inventory':{},'entities':[]}
            result=construction_budget(root,obs,catalog)
            self.assertEqual(result['remaining_placement_items'],{'electric-mining-drill':1})
            self.assertEqual(result['base_material_upper_bound'],{'iron-plate':23})


if __name__=='__main__':unittest.main()
