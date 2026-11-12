import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from factorio_agent.luna_client import LunaDecisionClient,normalize_decision


def response(**changes):
    return dict(operation='wait',capability=None,parameters_json='{}',queue=[],reason='No action needed',**changes)


class LunaDecisionTests(unittest.TestCase):
    def test_queue_wire_parameters_are_data_not_shell_commands(self):
        value=response();value.update(operation='plan',queue=[dict(id='power',capability='power',
            parameters_json='{}',priority=5,depends_on=[],reason='Native power first')])
        result=normalize_decision(value)
        self.assertEqual(result['queue'][0]['parameters'],{})
        for raw in ('[]','null','echo hello'):
            value['queue'][0]['parameters_json']=raw
            with self.assertRaises(ValueError):normalize_decision(value)

    def test_unsupported_operation_or_arbitrary_command_fails_closed(self):
        for value in ({'operation':'shell'},dict(response(),command='anything'),
                      dict(response(),operation='request',capability='invalid;command')):
            with self.assertRaises(ValueError):normalize_decision(value)
        with self.assertRaises(ValueError):LunaDecisionClient(model='another-model')

    def test_target_task_id_normalizes_controls_and_keeps_old_missing_field_compatible(self):
        self.assertNotIn('target_task_id',normalize_decision(response()))
        self.assertIsNone(normalize_decision(dict(response(),target_task_id=None))['target_task_id'])
        for operation in ('continue','retry','pause'):
            value=dict(response(),operation=operation,target_task_id='funded_electric',parameters_json='{"technology":"electric-mining-drill"}')
            normalized=normalize_decision(value)
            self.assertEqual(normalized['target_task_id'],'funded_electric')
            self.assertEqual(normalized['parameters'],{'technology':'electric-mining-drill'})

    def test_target_task_id_rejects_invalid_ids_wrong_operations_and_raw_job_keys(self):
        for target in ('','a'*65,'contains spaces',{'position':{'x':1,'y':2}},True):
            with self.assertRaises(ValueError):normalize_decision(dict(response(),operation='continue',target_task_id=target))
        for operation in ('plan','request','wait'):
            with self.assertRaises(ValueError):normalize_decision(dict(response(),operation=operation,capability='starter',target_task_id='existing'))
        with self.assertRaises(ValueError):normalize_decision(dict(response(),operation='continue',job_key='lunaq_raw'))

    def test_native_cli_uses_saved_auth_strict_model_and_readonly_planner(self):
        client=LunaDecisionClient()
        def run(command,**kwargs):
            self.assertEqual(command[command.index('--model')+1],'gpt-6-luna')
            self.assertEqual(command[command.index('--sandbox')+1],'read-only')
            self.assertNotIn('OPENAI_API_KEY',kwargs['env']);self.assertNotIn('CODEX_API_KEY',kwargs['env'])
            self.assertNotIn('do-not-send',kwargs['input'].decode())
            schema=json.loads(Path(command[command.index('--output-schema')+1]).read_text(encoding='utf-8'))
            self.assertIn('target_task_id',schema['required']);self.assertEqual(schema['properties']['target_task_id']['type'],['string','null'])
            prompt=kwargs['input'].decode('utf-8')
            self.assertIn('Reuse the SAME ID for retries of the SAME effect',prompt)
            self.assertIn('FULL output buffers is not evidence of a shortage',prompt)
            self.assertIn('Finish or repair an existing funded science batch',prompt)
            Path(command[command.index('--output-last-message')+1]).write_text(json.dumps(response()),encoding='utf-8')
            return SimpleNamespace(returncode=0,stdout=b'{"type":"turn.completed","usage":{"input_tokens":20,"output_tokens":5}}\n',stderr=b'')
        with patch('factorio_agent.luna_client._codex_command',return_value=['codex.exe']),\
             patch('factorio_agent.luna_client.LunaDecisionClient._invoke',side_effect=run),\
             patch.dict('os.environ',{'OPENAI_API_KEY':'do-not-send','CODEX_API_KEY':'do-not-send'}):
            result=client.decide({'world_id':'w','rcon_password':'do-not-send'})
        self.assertEqual(result['operation'],'wait');self.assertEqual(client.last_usage['input_tokens'],20)

    def test_tool_attempt_or_model_failure_does_not_authorize_a_game_decision(self):
        def run(command,**kwargs):
            Path(command[command.index('--output-last-message')+1]).write_text(json.dumps(response()),encoding='utf-8')
            return SimpleNamespace(returncode=0,stdout=b'{"type":"item.started","item":{"type":"command_execution"}}',stderr=b'')
        with patch('factorio_agent.luna_client._codex_command',return_value=['codex.exe']),\
             patch('factorio_agent.luna_client.LunaDecisionClient._invoke',side_effect=run):
            with self.assertRaisesRegex(RuntimeError,'attempted a tool'):LunaDecisionClient().decide({'world_id':'w'})

    def test_stop_terminates_only_its_owned_model_process_and_prevents_another_call(self):
        client=LunaDecisionClient();process=SimpleNamespace(poll=lambda:None,terminate=unittest.mock.Mock())
        client._process=process;client.cancel();process.terminate.assert_called_once()
        with patch('factorio_agent.luna_client.subprocess.Popen')as spawn:
            with self.assertRaisesRegex(RuntimeError,'stopped'):
                client._invoke(['codex.exe'],input=b'{}',timeout=1)
            spawn.assert_not_called()
