from copy import deepcopy
import hashlib
import json
from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import zipfile

from factorio_agent.lifecycle import Lifecycle,Processes,matches,command_argv,STATE,PROCESS,STOP,PAUSE,LOCAL_ACK


class NativeInspectionTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows process inspection')
    def test_exit_during_cim_inspection_is_normal_and_live_missing_identity_is_preserved(self):
        empty=SimpleNamespace(stdout='')
        with patch('factorio_agent.lifecycle._identity',side_effect=[123,None]),\
             patch('factorio_agent.lifecycle.subprocess.run',return_value=empty):
            self.assertIsNone(Processes().inspect(100))
        with patch('factorio_agent.lifecycle._identity',side_effect=[123,123]),\
             patch('factorio_agent.lifecycle.subprocess.run',return_value=empty):
            with self.assertRaisesRegex(RuntimeError,'process preserved'):Processes().inspect(100)


class FakeProcesses:
    def __init__(self):self.rows={};self.spawned=[];self.closed=[];self.next=100
    def inspect(self,pid):return deepcopy(self.rows.get(pid))
    def spawn(self,command,**kw):
        self.next+=1;pid=self.next;self.spawned.append((command,kw))
        self.rows[pid]=dict(pid=pid,creation_time=1000+pid,executable=sys.executable,argv=command)
        return pid
    def terminate(self,record):
        if self.rows.get(record['pid'],{}).get('creation_time')!=record['creation_time']:raise RuntimeError('PID reused')
        self.closed.append(record['pid']);self.rows.pop(record['pid'])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.repo=Path(self.tmp.name).resolve()
        self.root=self.repo/'runtime/v2/eval-1053994892';self.proc=FakeProcesses();self.now=0.;self.paused=False;self.calls=[];self.ack=True
        self.make_runtime(self.root,1053994892);self.control=Lifecycle(self.repo,processes=self.proc,sleep=self.sleep,clock=lambda:self.now)
        patches=[
            patch('factorio_agent.lifecycle.runtime.configuration',side_effect=self.config),
            patch('factorio_agent.lifecycle.runtime.game_for',return_value=SimpleNamespace(query=self.query)),
            patch('factorio_agent.lifecycle.runtime.start',side_effect=self.server),
            patch('factorio_agent.lifecycle.runtime.connect_player',side_effect=self.client),
            patch('factorio_agent.lifecycle.runtime.create',side_effect=self.create),
            patch('factorio_agent.lifecycle.set_client_visibility',return_value={'status':'ready'}),
            patch('factorio_agent.lifecycle._identity',return_value=None),
            patch.object(self.control,'_cli',side_effect=self.cli),
        ]
        for p in patches:p.start();self.addCleanup(p.stop)

    def write(self,path,value):Path(path).parent.mkdir(parents=True,exist_ok=True);Path(path).write_text(json.dumps(value))
    def make_runtime(self,root,seed):
        self.write(root/'run.json',dict(schema_version=2,root=str(root),seed=seed,server_port=34261,rcon_port=27061,rcon_password='secret'))
        self.write(root/'world-settings.json',dict(world_id='w'+str(seed)))
        self.write(root/'jobs.json',dict(jobs={}))
        (root/'logs').mkdir(exist_ok=True);self.save(root)

    def save(self,root):
        p=root/'vanilla/saves/no-mod-rcon.zip';p.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(p,'w')as z:z.writestr('level.dat',b'saved world')
        stamp=p.stat().st_mtime_ns+1000;os.utime(p,ns=(stamp,stamp))

    def config(self,root):
        return SimpleNamespace(runtime_dir=Path(root),factorio_exe=self.repo/'factorio.exe',rcon_host='127.0.0.1',rcon_port=27061,rcon_password='secret')

    def server(self,root):
        self.calls.append(('server',str(root)));pid=10
        self.proc.rows[pid]=dict(pid=pid,creation_time=100,executable=str(self.repo/'factorio.exe'),argv=[str(self.repo/'factorio.exe'),'--config',str(root/'vanilla/server-config.ini'),'--start-server',str(root/'vanilla/saves/no-mod-rcon.zip')])
        self.write(root/'server.json',dict(pid=pid,creation_time=100));return {'status':'running'}

    def client(self,root):
        self.calls.append(('client',str(root)));pid=11
        self.proc.rows[pid]=dict(pid=pid,creation_time=110,executable=str(self.repo/'factorio.exe'),argv=[str(self.repo/'factorio.exe'),'--config',str(root/'agent-client/client-config.ini'),'--mp-connect','127.0.0.1:34261'])
        self.write(root/'agent-client/client-process.json',dict(pid=pid,creation_time=110));return dict(status='ready',pid=pid)

    def create(self,root,**kw):
        self.calls.append(('create',str(root),kw));self.make_runtime(root,kw['seed']);self.paused=False;return {'status':'created'}

    def query(self,script):
        encoded=script.split('helpers.json_to_table(',1)[1].split(');',1)[0];x=json.loads(json.loads(encoded))
        if not x['paused']and self.paused and not x['owned']:return dict(ok=False,reason='world_paused_by_other_operator')
        self.paused=x['paused'];self.calls.append(('pause',self.paused))
        return dict(ok=True,world_id=x['world_id'],tick=100,tick_paused=self.paused)

    def cli(self,root,*args):
        self.calls.append(('cli',args))
        if args[0]=='stop-worker':
            if not(root/'stop.json').exists():self.write(root/'stop.json',{'source':'gameplay-worker-cli','requested_at':1})
        elif args[0]=='clear-worker-stop':(root/'stop.json').unlink()
        elif args[0]=='checkpoint':self.save(root)
        elif args[0]=='pause':
            value=json.loads((root/'jobs.json').read_text());value['jobs'][args[2]]['status']='paused';self.write(root/'jobs.json',value)
        return dict(ok=True,status='succeeded')

    def sleep(self,seconds):
        self.now+=seconds
        if self.ack and(self.root/STOP).exists():
            request=json.loads((self.root/STOP).read_text())
            self.cli(self.root,'stop-worker');digest=hashlib.sha256((self.root/'stop.json').read_bytes()).hexdigest()
            self.write(self.root/STATE,dict(status='safe_stopped',state='stopped',world_id=request['world_id'],stop_request_id=request['request_id'],
                saved=True,worker_stop_owned=True,worker_stop_sha256=digest))
            for pid,row in list(self.proc.rows.items()):
                if '-m'in row['argv']and 'factorio_agent.supervisor'in row['argv']:self.proc.rows.pop(pid)

    def launch(self):
        result=self.control.start();self.assertEqual(result['status'],'running');return result

    def test_start_resumes_only_active_world_and_launches_one_hidden_supervisor(self):
        a=self.launch();b=self.launch();self.assertEqual(a['supervisor_pid'],b['supervisor_pid']);self.assertTrue(b['reused']);self.assertEqual(len(self.proc.spawned),1)
        cmd,options=self.proc.spawned[0];self.assertEqual(cmd[:4],[sys.executable,'-B','-m','factorio_agent.supervisor'])
        self.assertEqual(options['env']['PYTHONPATH'],str(self.repo/'src'));self.assertIn(str(self.root),cmd)
        self.assertNotIn('create',[q[0]for q in self.calls]);self.assertEqual(json.loads(self.control.pointer.read_text())['runtime'],str(self.root))

    def test_stop_waits_exact_ack_pauses_and_saves_without_closing_gui(self):
        self.launch();result=self.control.stop(timeout=2)
        self.assertTrue(result['game_paused']);self.assertTrue(self.paused);self.assertEqual(self.proc.closed,[]);self.assertIn(11,self.proc.rows)
        self.assertEqual(json.loads((self.root/PAUSE).read_text())['phase'],'paused')
        self.assertTrue(any(q==('cli',('checkpoint',))for q in self.calls))

    def test_start_after_stop_clears_only_its_acknowledged_markers_and_owned_pause(self):
        self.launch();self.control.stop(timeout=2);self.assertTrue((self.root/'stop.json').exists())
        self.control.start();self.assertFalse(self.paused);self.assertFalse((self.root/'stop.json').exists());self.assertFalse((self.root/STOP).exists());self.assertFalse((self.root/PAUSE).exists())

    def test_mismatched_or_missing_ack_does_not_kill_or_pause_game(self):
        self.launch();self.ack=False
        with self.assertRaises(TimeoutError):self.control.stop(timeout=.5)
        self.assertEqual(self.proc.closed,[]);self.assertFalse(self.paused);self.assertIn(11,self.proc.rows)

    def test_no_supervisor_stop_uses_standard_job_pause_checkpoint_and_reusable_ack(self):
        self.server(self.root);self.client(self.root);self.write(self.root/'jobs.json',{'jobs':{'job':{'status':'waiting'}}})
        self.control.stop();self.assertTrue(self.paused);self.assertEqual(json.loads((self.root/'jobs.json').read_text())['jobs']['job']['status'],'paused')
        self.assertTrue(json.loads((self.root/LOCAL_ACK).read_text())['saved']);self.control.start();self.assertFalse(self.paused)

    def test_foreign_stop_and_unowned_pause_are_not_silently_cleared(self):
        self.server(self.root);self.write(self.root/'stop.json',{'source':'another-operator'})
        with self.assertRaises(RuntimeError):self.control.start()
        self.assertEqual(json.loads((self.root/'stop.json').read_text())['source'],'another-operator');self.assertEqual(len(self.proc.spawned),0)
        (self.root/'stop.json').unlink();self.paused=True
        with self.assertRaises(RuntimeError):self.control.start()
        self.assertTrue(self.paused);self.assertEqual(len(self.proc.spawned),0)

    def test_pid_reuse_wrong_runtime_and_foreign_commands_are_preserved(self):
        self.server(self.root);record=json.loads((self.root/'server.json').read_text());row=self.proc.rows[10]
        self.assertTrue(matches(row,record,self.root,'server',self.repo/'factorio.exe'))
        row['creation_time']=999
        with self.assertRaises(RuntimeError):self.control.shutdown()
        self.assertEqual(self.proc.closed,[]);row['creation_time']=100;row['argv'][2]=str(self.repo/'foreign/server-config.ini')
        with self.assertRaises(RuntimeError):self.control.shutdown()
        self.assertEqual(self.proc.closed,[])

    def test_active_pointer_rejects_external_or_relocated_runtime(self):
        self.write(self.control.pointer,dict(runtime=str(self.repo.parent/'foreign'),seed=1))
        with self.assertRaises(ValueError):self.control.status()
        self.write(self.control.pointer,dict(runtime=str(self.root),seed=999))
        with self.assertRaises(ValueError):self.control.start()

    def test_selection_preserves_model_source_and_mode_metadata(self):
        self.write(self.control.pointer,dict(runtime=str(self.root),seed=1053994892,source_root=str(self.repo),model='gpt-6-luna',mode='assisted_development'))
        self.launch();value=json.loads(self.control.pointer.read_text());self.assertEqual(value['model'],'gpt-6-luna')
        self.assertEqual(value['source_root'],str(self.repo));self.assertEqual(value['mode'],'assisted_development')

    def test_shutdown_saves_then_closes_only_verified_registered_processes(self):
        self.server(self.root);self.client(self.root)
        self.proc.rows[999]=dict(pid=999,creation_time=99,argv=['other'],executable='other')
        rcon=Mock();rcon.__enter__=Mock(return_value=rcon);rcon.__exit__=Mock(return_value=None)
        rcon.execute.side_effect=lambda *a,**k:self.proc.rows.pop(10,None)
        with patch('factorio_agent.lifecycle.FactorioRconClient',return_value=rcon):
            result=self.control.shutdown()
        self.assertEqual(result['status'],'shutdown');self.assertEqual(self.proc.closed,[11]);self.assertIn(999,self.proc.rows);self.assertTrue(self.paused)

    def test_pending_clean_server_exit_never_force_kills_a_possible_save_writer(self):
        self.server(self.root);self.client(self.root)
        with patch('factorio_agent.lifecycle.FactorioRconClient'):
            with self.assertRaises(TimeoutError):self.control.shutdown()
        self.assertEqual(self.proc.closed,[]);self.assertIn(10,self.proc.rows);self.assertIn(11,self.proc.rows)

    def test_unfinished_checkpoint_blocks_shutdown_before_any_process_close(self):
        self.server(self.root);self.client(self.root)
        with patch.object(self.control,'_checkpoint',side_effect=TimeoutError('save still writing')):
            with self.assertRaises(TimeoutError):self.control.shutdown()
        self.assertEqual(self.proc.closed,[]);self.assertIn(10,self.proc.rows)

    def test_new_map_keeps_old_runtime_and_backs_up_before_unique_paid_start(self):
        self.server(self.root);self.client(self.root);original=(self.root/'run.json').read_bytes()
        with patch.object(self.control,'_ports',return_value=(34262,27062)),patch('factorio_agent.lifecycle.secrets.randbits',return_value=123),patch('factorio_agent.lifecycle.FactorioRconClient')as rcon:
            rcon.return_value.__enter__.return_value.execute.side_effect=lambda *a,**k:self.proc.rows.pop(10,None)
            result=self.control.new_map()
        self.assertEqual((self.root/'run.json').read_bytes(),original);self.assertTrue(self.root.exists())
        backup=Path(result['backup']);self.assertTrue((backup/'no-mod-rcon.zip').exists());self.assertTrue((backup/'backup-manifest.json').exists())
        self.assertEqual(json.loads(self.control.pointer.read_text())['seed'],123);self.assertEqual(len(self.proc.spawned),1)
        created=[q for q in self.calls if q[0]=='create'][0][2]
        self.assertEqual({k:v for k,v in created.items()if k!='automation_config'},dict(seed=123,server_port=34262,rcon_port=27062))
        self.assertEqual(created['automation_config'].world.starting_area_percent,200)

    def test_configured_start_changes_controller_only_on_actual_restart(self):
        before=(self.root/'world-settings.json').read_bytes()
        self.write(self.repo/'automation_config.json',{'controller':{'decision_interval_seconds':120,'execution_slice_seconds':15}})
        self.launch();command=self.proc.spawned[-1][0]
        self.assertEqual(command[command.index('--decision-interval')+1],'120')
        self.assertEqual(command[command.index('--slice-seconds')+1],'15')
        path=self.root/'controller-config.json';snapshot=path.read_bytes()
        self.assertEqual(json.loads(snapshot)['controller']['execution_slice_seconds'],15)
        self.write(self.repo/'automation_config.json',{'world':{'starting_area_percent':100},'controller':{'decision_interval_seconds':180}})
        self.assertTrue(self.control.start()['reused']);self.assertEqual(path.read_bytes(),snapshot)
        self.control.stop(timeout=2);self.control.start()
        self.assertEqual(self.proc.spawned[-1][0][-3],'180')
        self.assertEqual((self.root/'world-settings.json').read_bytes(),before)
        self.assertFalse((self.root/'world-generation-config.json').exists())

    def test_invalid_config_start_or_new_map_has_no_process_or_save_effect(self):
        for value in ({'controller':{'model':'other'}},{'world':{'starting_area_percent':True}},{'world':None}):
            with self.subTest(value=value):
                self.write(self.repo/'automation_config.json',value)
                before=(self.root/'vanilla/saves/no-mod-rcon.zip').stat().st_mtime_ns
                with self.assertRaises(ValueError):self.control.new_map()
                with self.assertRaises(ValueError):self.control.start()
                self.assertEqual(self.calls,[]);self.assertEqual(self.proc.spawned,[])
                self.assertEqual((self.root/'vanilla/saves/no-mod-rcon.zip').stat().st_mtime_ns,before)

    def test_configured_seed_collision_preserves_running_old_world(self):
        self.launch();self.write(self.repo/'automation_config.json',{'world':{'seed':1053994892}})
        before=list(self.calls)
        with self.assertRaises(FileExistsError):self.control.new_map()
        self.assertEqual(self.calls,before);self.assertFalse(self.paused);self.assertEqual(self.proc.closed,[])

    def test_new_map_uses_prevalidated_seed_and_options_after_old_world_stops(self):
        self.write(self.repo/'automation_config.json',{'world':{'seed':321},'controller':{'execution_slice_seconds':12}})
        original_stop=self.control._stop
        def stop(root):
            result=original_stop(root)
            (self.repo/'automation_config.json').write_text('{invalid-after-safe-stop')
            return result
        with patch.object(self.control,'_stop',side_effect=stop),patch.object(self.control,'_shutdown',return_value={}),\
             patch.object(self.control,'_ports',return_value=(34262,27062)),\
             patch('factorio_agent.lifecycle.secrets.randbits')as random:
            result=self.control.new_map()
        self.assertEqual(result['seed'],321);random.assert_not_called()
        options=[q for q in self.calls if q[0]=='create'][0][2]['automation_config']
        self.assertEqual(options.world.seed,321);self.assertEqual(options.controller.execution_slice_seconds,12)
        self.assertEqual(self.proc.spawned[-1][0][-1],'12')

    def test_failed_backup_prevents_new_world_or_process_close(self):
        self.server(self.root);(self.root/'vanilla/saves/no-mod-rcon.zip').write_bytes(b'broken')
        with patch.object(self.control,'_checkpoint',return_value={}):
            with self.assertRaises(zipfile.BadZipFile):self.control.new_map()
        self.assertEqual(self.proc.closed,[]);self.assertNotIn('create',[q[0]for q in self.calls])

    def test_stop_reads_final_ack_after_process_exit_inspection(self):
        self.launch();original=self.control._owned;calls=[0]
        def owned(root,kind):
            if kind=='supervisor':
                calls[0]+=1
                if calls[0]==2:
                    marker=json.loads((root/STOP).read_text())
                    record=json.loads((root/PROCESS).read_text())
                    self.write(root/STATE,dict(status='safe_stopped',state='stopped',saved=True,
                        world_id=marker['world_id'],stop_request_id=marker['request_id']))
                    self.proc.rows.pop(record['pid'])
            return original(root,kind)
        with patch.object(self.control,'_owned',side_effect=owned):
            result=self.control.stop()
        self.assertEqual(result['status'],'stopped');self.assertTrue(self.paused)

    def test_status_and_logs_are_bounded_and_do_not_expose_server_password(self):
        self.launch();value=self.control.status();self.assertNotIn('secret',json.dumps(value));self.assertNotIn('argv',json.dumps(value))
        (self.root/'logs/supervisor-process.log').write_text('\n'.join(str(i)for i in range(100)))
        self.assertEqual(self.control.logs(lines=3)['logs']['supervisor-process.log'],['97','98','99'])
        with self.assertRaises(ValueError):self.control.logs(lines=201)

    def test_all_batch_files_delegate_with_absolute_repo_source_and_no_process_kills(self):
        repo=Path(__file__).resolve().parents[1]
        for name,command in [('start','start'),('stop','stop'),('new_map','new-map'),('gui','gui'),('status','status'),('logs','logs'),('shutdown','shutdown')]:
            content=(repo/(name+'.bat')).read_text(encoding='utf-8')
            self.assertIn('set "PYTHONPATH=%~dp0src"',content);self.assertIn('factorio_agent.lifecycle --repo "%~dp0." '+command,content)
            self.assertNotIn('taskkill',content.lower());self.assertNotIn('del ',content.lower())

    @unittest.skipUnless(os.name=='nt','native Windows argument parser')
    def test_native_windows_argument_parser_preserves_quoted_runtime_paths(self):
        import subprocess
        argv=[r'C:\Program Files\Python310\python.exe','-B','-m','factorio_agent.supervisor','--runtime',str(self.repo/'space path')]
        self.assertEqual(command_argv(subprocess.list2cmdline(argv)),argv)


if __name__=='__main__':unittest.main()
