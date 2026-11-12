from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest

from factorio_agent.__main__ import main


class ActorRecoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.game=SimpleNamespace(observe=Mock(side_effect=AssertionError('dead actor cannot be observed')),
            _record_action=Mock())

    def call(self,*args):
        out=io.StringIO()
        with redirect_stdout(out):main(['--runtime',str(self.root),*args])
        return json.loads(out.getvalue())

    def test_status_remains_available_without_an_observable_actor_or_game_effect(self):
        with patch('factorio_agent.runtime.game_for',return_value=self.game),patch(
                'factorio_agent.actor_recovery.inspect_actor_recovery',return_value={'ok':True,'new_unit':42})as inspect:
            self.assertEqual(self.call('actor-status')['new_unit'],42)
            inspect.assert_called_once_with(self.game)
        self.game.observe.assert_not_called();self.game._record_action.assert_not_called()

    def test_exact_successor_recovery_is_serialized_and_audited_without_job_replay(self):
        def execute(game,world,old,new):
            self.assertTrue((self.root/'writer.lock').exists())
            self.assertEqual((game,world,old,new),(self.game,'w',11,42))
            return {'ok':True,'tick':100,'old_unit':11,'new_unit':42}
        with patch('factorio_agent.runtime.game_for',return_value=self.game),patch(
                'factorio_agent.actor_recovery.execute_actor_recovery',side_effect=execute)as recover:
            result=self.call('recover-actor','--world-id','w','--expected-old-unit','11','--expected-new-unit','42')
        self.assertTrue(result['ok']);recover.assert_called_once();self.game.observe.assert_not_called()
        self.game._record_action.assert_called_once()
        events=(self.root/'job-events.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(events),1);self.assertEqual(json.loads(events[0])['event'],'actor_recovery')

    def test_belongings_recovery_serializes_real_transfers_and_preserves_one_tool_limit(self):
        def execute(game,world,actor,death,*,keep_single_repair_tool):
            self.assertTrue((self.root/'writer.lock').exists())
            self.assertEqual((game,world,actor,death),(self.game,'w',42,90))
            self.assertTrue(keep_single_repair_tool)
            return {'ok':True,'tick':100,'transferred_items':4103,'conservation':{'full':'recorded'}}
        with patch('factorio_agent.runtime.game_for',return_value=self.game),patch(
                'factorio_agent.corpse_recovery.execute_corpse_recovery',side_effect=execute):
            result=self.call('recover-belongings','--world-id','w','--expected-actor-unit','42','--death-tick','90')
        self.assertEqual(result['transferred_items'],4103);self.assertNotIn('conservation',result)
        self.game.observe.assert_not_called();self.game._record_action.assert_called_once()

    def test_safe_return_is_an_ordinary_move_and_does_not_execute_a_blocked_emission(self):
        self.game.act=Mock(return_value={'ok':True,'position':{'x':0,'y':0}})
        with patch('factorio_agent.runtime.game_for',return_value=self.game),patch(
                'factorio_agent.defense_repairs.return_to_spawn_action',return_value={'type':'move','position':{'x':0,'y':0}})as emit:
            self.assertTrue(self.call('return-to-base','--world-id','w','--expected-actor-unit','42')['ok'])
            emit.assert_called_once_with(self.game,'w',42)
            self.game.act.assert_called_once()
            emit.return_value={'status':'blocked','ok':False};self.game.act.reset_mock()
            self.assertFalse(self.call('return-to-base','--world-id','w','--expected-actor-unit','42')['ok'])
            self.game.act.assert_not_called()


if __name__=='__main__':unittest.main()
