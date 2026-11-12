from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
from factorio_agent.core.deterministic_game import run_config
from factorio_agent.runtime import WorldBoundGame


class GuardedSplitterSettingsTests(unittest.TestCase):
    def action(self):
        return {'type':'configure_splitter','name':'splitter','position':{'x':.5,'y':.5},
                'expected_splitter_unit':7,'expected_splitter_world_id':'world',
                'input_priority':'left','output_priority':'right'}

    def test_invalid_settings_and_missing_identity_are_rejected_before_game_io(self):
        game=WorldBoundGame(run_config(),'world')
        for change in ({'input_priority':'all'},{'expected_splitter_unit':True},{'position':{'x':float('nan'),'y':.5}},
                       {'expected_splitter_world_id':''},{'inventory':{'iron-plate':100}}):
            with patch.object(game,'query') as query:
                with self.assertRaises(ValueError):game.act(dict(self.action(),**change))
                query.assert_not_called()

    def test_setting_uses_same_command_world_unit_and_force_checks(self):
        with TemporaryDirectory() as directory:
            game=WorldBoundGame(run_config(runtime=Path(directory)),'world')
            receipt={'ok':True,'status':'succeeded','input_priority':'left','output_priority':'right'}
            with patch.object(game,'query',return_value=receipt) as query:
                self.assertEqual(game.act(self.action()),receipt)
            script=query.call_args.args[0]
            for guard in ('d.world_id~=x.expected_splitter_world_id','e.unit_number~=x.expected_splitter_unit','e.force~=f'):
                self.assertIn(guard,script)
            self.assertNotIn('inv.insert',script)
            self.assertNotIn('create_entity',script)

    def test_entire_recovery_batch_is_validated_before_first_effect(self):
        game=WorldBoundGame(run_config(),'world')
        mine={'type':'mine','name':'transport-belt','position':{'x':.5,'y':.5},
              'expected_entity_unit':7,'expected_entity_world_id':'world','count':1}
        for bad in ({'expected_entity_unit':True},{'expected_entity_world_id':'other'},{'type':'craft'}):
            with patch.object(game,'query') as query:
                with self.assertRaises(ValueError):game.act({'type':'mine_many','actions':[mine,dict(mine,**bad)]})
                query.assert_not_called()

    def test_uncertain_recovery_batch_preserves_prefix_and_never_retries_native_command(self):
        with TemporaryDirectory() as directory:
            game=WorldBoundGame(run_config(runtime=Path(directory)),'world')
            rows=[{'type':'mine','name':'transport-belt','position':{'x':i+.5,'y':.5},
                   'expected_entity_unit':i+1,'expected_entity_world_id':'world','count':1} for i in range(3)]
            uncertain={'ok':False,'status':'blocked','reason':'mine_batch_outcome_unknown','completed':1,
                       'results':[{'ok':True,'status':'succeeded','unit_number':1}],'uncertain_index':1}
            with patch.object(game,'query',return_value=uncertain) as query:
                self.assertEqual(game.act({'type':'mine_many','actions':rows}),uncertain)
                query.assert_called_once()
            with patch.object(game,'query',side_effect=TimeoutError('lost response')) as query:
                result=game.act({'type':'mine_many','actions':rows})
                self.assertEqual(result['reason'],'mine_batch_outcome_unknown')
                self.assertEqual(result['completed'],0)
                query.assert_called_once()


if __name__=='__main__':unittest.main()
