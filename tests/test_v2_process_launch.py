import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock,patch

from factorio_agent.core import process_launch as module


@unittest.skipUnless(os.name=='nt','Windows console ownership')
class IndependentLaunchTests(unittest.TestCase):
    def test_game_arguments_use_private_pipe_and_never_broker_command_line(self):
        broker=Mock(returncode=0);broker.communicate.return_value=(json.dumps({'pid':123}),'')
        with patch.object(module.subprocess,'Popen',return_value=broker)as spawn,patch.object(module,'DetachedChild')as child:
            result=module.spawn_console_independent(['factorio.exe','--rcon-password','private-value'],log_path=Path('runtime/test.log'))
        self.assertIs(result,child.return_value);child.assert_called_once_with(123)
        self.assertNotIn('private-value',str(spawn.call_args.args))
        payload=json.loads(broker.communicate.call_args.args[0])
        self.assertEqual(payload['command'][-1],'private-value')
        self.assertTrue(spawn.call_args.kwargs['creationflags']&subprocess.DETACHED_PROCESS)
        self.assertEqual(spawn.call_args.kwargs['stdin'],subprocess.PIPE)

    def test_uncertain_launch_never_kills_or_starts_a_duplicate(self):
        broker=Mock();broker.communicate.side_effect=subprocess.TimeoutExpired('broker',15)
        with patch.object(module.subprocess,'Popen',return_value=broker)as spawn:
            with self.assertRaisesRegex(RuntimeError,'outcome unknown'):
                module.spawn_console_independent(['factorio.exe'],log_path=Path('runtime/test.log'))
        self.assertEqual(spawn.call_count,1);broker.kill.assert_not_called();broker.terminate.assert_not_called()

    def test_malformed_launch_receipt_is_not_retried(self):
        broker=Mock(returncode=0);broker.communicate.return_value=('{}','')
        with patch.object(module.subprocess,'Popen',return_value=broker)as spawn:
            with self.assertRaisesRegex(RuntimeError,'outcome unknown'):
                module.spawn_console_independent(['factorio.exe'],log_path=Path('runtime/test.log'))
        self.assertEqual(spawn.call_count,1)
