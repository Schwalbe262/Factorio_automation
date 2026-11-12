"""Windows launch controls for one explicitly selected, resumable v2 world."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

from factorio_ai.deterministic_state import RunLock,_atomic_json
from factorio_ai.deterministic_character import _identity,set_client_visibility
from factorio_ai.factorio import no_mod_save_path
from factorio_ai.rcon import FactorioRconClient
from . import runtime
from .automation_config import load_automation_config

DEFAULT_RUNTIME=Path('runtime/v2/eval-1053994892')
STATE=Path('logs/controller-state.json')
STOP='supervisor-stop.json'
PAUSE='lifecycle-pause.json'
PROCESS='supervisor-process.json'
LOCAL_ACK='lifecycle-stop-ack.json'


def read(path,default=None):
    if not Path(path).exists()and default is not None:return default
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _inside(path,parent):
    try:Path(path).resolve().relative_to(Path(parent).resolve());return True
    except ValueError:return False


def command_argv(raw):
    if os.name!='nt':
        import shlex
        return shlex.split(raw)
    shell=ctypes.WinDLL('shell32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    shell.CommandLineToArgvW.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_int)];shell.CommandLineToArgvW.restype=ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes=[wintypes.HLOCAL];kernel.LocalFree.restype=wintypes.HLOCAL
    count=ctypes.c_int();values=shell.CommandLineToArgvW(raw,ctypes.byref(count))
    if not values:raise OSError('cannot inspect process arguments')
    try:return [values[i]for i in range(count.value)]
    finally:kernel.LocalFree(ctypes.cast(values,wintypes.HLOCAL))


class Processes:
    """Inspect command lines internally; never print credentials from server argv."""
    def inspect(self,pid):
        if type(pid)is not int or pid<1:return None
        created=_identity(pid)
        if created is None:return None
        if os.name!='nt':raise RuntimeError('Windows process inspection required')
        script='[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);Get-CimInstance Win32_Process -Filter "ProcessId = '+str(pid)+'" | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress'
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=12,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=True)
        if _identity(pid)!=created:return None
        if not result.stdout.strip():raise RuntimeError('live process inspection returned no identity; process preserved')
        row=json.loads(result.stdout)
        if not row:return None
        return dict(pid=pid,creation_time=created,executable=row['ExecutablePath'],argv=command_argv(row['CommandLine']))

    def spawn(self,command,*,cwd,env,log):
        with Path(log).open('ab')as output:
            child=subprocess.Popen(command,cwd=str(cwd),env=env,stdout=output,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
        return child.pid

    def terminate(self,record):
        """Close an exact process handle only, after its creation time is checked again."""
        if os.name!='nt':raise RuntimeError('Windows process close required')
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1001,False,record['pid'])
        if not handle:raise OSError('cannot open owned process')
        try:
            times=[wintypes.FILETIME()for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*(ctypes.byref(q)for q in times)):raise OSError('cannot verify owned process time')
            created=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
            if created!=record['creation_time']:raise RuntimeError('process identity changed before close')
            if not kernel.TerminateProcess(handle,0):raise OSError('owned process close failed')
        finally:kernel.CloseHandle(handle)


def _arg(argv,key):
    return argv[argv.index(key)+1]if key in argv and argv.index(key)+1<len(argv)else None


def matches(snapshot,record,root,kind,exe=None):
    if(not snapshot or type(record.get('pid'))is not int or type(record.get('creation_time'))is not int
            or snapshot.get('pid')!=record['pid']or snapshot.get('creation_time')!=record['creation_time']):return False
    argv=snapshot.get('argv',[])
    def path(value,wanted):return isinstance(value,str)and Path(value).resolve()==Path(wanted).resolve()
    if kind=='supervisor':
        return(_arg(argv,'-m')=='factorio_agent.supervisor'and path(_arg(argv,'--runtime'),root)
            and path(snapshot.get('executable'),sys.executable))
    config=root/('vanilla/server-config.ini'if kind=='server'else'agent-client/client-config.ini')
    return(path(snapshot.get('executable'),exe)and path(_arg(argv,'--config'),config)
        and(kind!='server'or _arg(argv,'--start-server')is not None and _inside(_arg(argv,'--start-server'),root/'vanilla'))
        and(kind!='client'or _arg(argv,'--mp-connect')is not None))


class Lifecycle:
    def __init__(self,repo=None,*,processes=None,sleep=time.sleep,clock=time.monotonic):
        self.repo=Path(repo or Path(__file__).resolve().parents[2]).resolve();self.base=self.repo/'runtime'
        self.pointer=self.base/'active-runtime.json';self.processes=processes or Processes();self.sleep,self.clock=sleep,clock

    def _root(self):
        pointer=read(self.pointer,{})
        value=pointer.get('runtime',str(self.repo/DEFAULT_RUNTIME))
        root=Path(value)
        if not root.is_absolute()or not _inside(root,self.base):raise ValueError('active runtime must be an absolute repository runtime directory')
        root=root.resolve();run=read(root/'run.json')
        if run.get('schema_version')!=2 or run.get('root')!=str(root)or type(run.get('seed'))is not int:raise ValueError('invalid or relocated active runtime')
        if pointer and pointer.get('seed')!=run['seed']:raise ValueError('active seed differs from registered runtime')
        if pointer.get('world_id')and pointer['world_id']!=read(root/'world-settings.json',{}).get('world_id'):raise ValueError('active world identity changed')
        return root

    def _select(self,root):
        cfg=read(root/'run.json');world=read(root/'world-settings.json',{}).get('world_id')
        pointer=read(self.pointer,{})
        pointer.update(schema_version=1,runtime=str(root.resolve()),seed=cfg['seed'],world_id=world,
            server_port=cfg['server_port'],rcon_port=cfg['rcon_port'],selected_at=time.time())
        _atomic_json(self.pointer,pointer)

    def _record(self,root,kind):
        name={'server':'server.json','client':'agent-client/client-process.json','supervisor':PROCESS}[kind]
        value=read(root/name,{})
        if kind=='supervisor'and not value:
            state=read(root/STATE,{})
            if(state.get('runtime')==str(root)and state.get('world_id')==read(root/'world-settings.json',{}).get('world_id')):
                return dict(pid=state.get('pid'),creation_time=state.get('native_creation_time'))
        return value

    def _owned(self,root,kind):
        record=self._record(root,kind)
        if not record:return None
        if record.get('world_id')and record['world_id']!=read(root/'world-settings.json',{}).get('world_id'):raise RuntimeError(kind+' registered world identity changed')
        snapshot=self.processes.inspect(record.get('pid'))
        if not snapshot:return None
        cfg=runtime.configuration(root)
        if not matches(snapshot,record,root,kind,cfg.factorio_exe):raise RuntimeError(kind+' process registry identity/command line mismatch; preserved')
        return record

    def _cli(self,root,*args):
        env=os.environ.copy();env['PYTHONPATH']=str(self.repo/'src')
        answer=subprocess.run([sys.executable,'-B','-m','factorio_agent','--runtime',str(root),*args],cwd=self.repo,env=env,
            capture_output=True,text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=150)
        if answer.returncode:raise RuntimeError('standard runtime command failed: '+args[0]+'; inspect runtime logs')
        try:result=json.loads(answer.stdout)
        except ValueError:raise RuntimeError('standard runtime command returned invalid JSON: '+args[0])
        if result.get('ok')is False or result.get('status')in('blocked','failed','error'):raise RuntimeError('standard runtime command blocked: '+str(result.get('reason',args[0])))
        return result

    def _ensure_server(self,root):
        if not self._owned(root,'server'):runtime.start(root)
        if not self._owned(root,'server'):raise RuntimeError('started server identity cannot be verified')
        return read(root/'world-settings.json')['world_id']

    def _client(self,root,*,visible):
        existing=self._owned(root,'client')
        if existing and visible:set_client_visibility(runtime.configuration(root),visible=True)
        result=runtime.connect_player(root)
        if result.get('status')!='ready':raise RuntimeError('client/actor connection not ready: '+str(result.get('reason')))
        if not self._owned(root,'client'):raise RuntimeError('client process identity cannot be verified')
        if visible:
            shown=set_client_visibility(runtime.configuration(root),visible=True)
            if shown.get('status')=='blocked':raise RuntimeError('owned GUI could not be shown: '+str(shown.get('reason')))
        return result

    def _game_pause(self,root,paused,request_id=None):
        world=read(root/'world-settings.json')['world_id'];old=read(root/PAUSE,{})
        if not paused and old:
            if old.get('kind')!='lifecycle-operator-pause-v1'or old.get('world_id')!=world or old.get('phase')!='paused':
                raise RuntimeError('operator pause marker does not authorize resume')
        if paused:
            marker=dict(kind='lifecycle-operator-pause-v1',world_id=world,request_id=request_id or str(uuid.uuid4()),phase='intent',requested_at=time.time())
            _atomic_json(root/PAUSE,marker)
        payload=json.dumps(dict(paused=paused,owned=bool(old),world_id=world))
        result=runtime.game_for(root).query('local x=helpers.json_to_table('+json.dumps(payload)+');'
            'if d.world_id~=x.world_id then return {ok=false,reason="lifecycle_world_changed"}end;'
            'if game.speed~=1 then return {ok=false,reason="normal_game_speed_required"}end;'
            'if not x.paused and game.tick_paused and not x.owned then return {ok=false,reason="world_paused_by_other_operator"}end;'
            'game.tick_paused=x.paused;return {ok=true,world_id=d.world_id,tick=game.tick,tick_paused=game.tick_paused}')
        if not result.get('ok')or result.get('world_id')!=world or result.get('tick_paused')is not paused:raise RuntimeError('world pause transition was not verified: '+str(result.get('reason')))
        if paused:
            marker.update(phase='paused',paused_tick=result['tick']);_atomic_json(root/PAUSE,marker)
        elif old:
            old.update(phase='resumed',resumed_tick=result['tick']);_atomic_json(root/'lifecycle-last-resume.json',old);(root/PAUSE).unlink()
        return result

    def _clear_stops(self,root):
        state=read(root/STATE,{});stop=root/'stop.json';local=read(root/LOCAL_ACK,{})
        if stop.exists():
            digest=hashlib.sha256(stop.read_bytes()).hexdigest()
            valid=lambda q:q.get('status')=='safe_stopped'and q.get('saved')is True and q.get('world_id')==read(root/'world-settings.json')['world_id']and q.get('worker_stop_owned')is True and q.get('worker_stop_sha256')==digest
            if not valid(state)and not valid(local):
                raise RuntimeError('worker stop marker is not covered by the last safe supervisor acknowledgement')
            self._cli(root,'clear-worker-stop')
        marker=root/STOP
        if marker.exists():
            value=read(marker)
            if(value.get('source')!='lifecycle'or state.get('status')!='safe_stopped'or state.get('stop_request_id')!=value.get('request_id')
                    or state.get('world_id')!=value.get('world_id')or state.get('saved')is not True):raise RuntimeError('supervisor stop marker has no exact saved acknowledgement')
            marker.unlink()

    def _start(self,root,*,visible=True,automation_config=None):
        options=automation_config if automation_config is not None else load_automation_config(self.repo/'automation_config.json')
        options.validate()
        self._ensure_server(root);existing=self._owned(root,'supervisor')
        if existing:
            if (root/STOP).exists():raise RuntimeError('supervisor safe stop is still pending')
            if visible and self._owned(root,'client'):set_client_visibility(runtime.configuration(root),visible=True)
            return dict(status='running',runtime=str(root),seed=read(root/'run.json')['seed'],supervisor_pid=existing['pid'],reused=True)
        with RunLock(root/'controller.lock'):pass
        self._client(root,visible=visible)
        with RunLock(root/'writer.lock'):self._clear_stops(root);self._game_pause(root,False)
        env=os.environ.copy();env['PYTHONPATH']=str(self.repo/'src')
        interval=options.controller.decision_interval_seconds;slice_seconds=options.controller.execution_slice_seconds
        number=lambda n:str(int(n))if float(n).is_integer()else str(n)
        command=[sys.executable,'-B','-m','factorio_agent.supervisor','--runtime',str(root),'--decision-interval',number(interval),'--slice-seconds',number(slice_seconds)]
        (root/'logs').mkdir(exist_ok=True);pid=self.processes.spawn(command,cwd=self.repo,env=env,log=root/'logs/supervisor-process.log')
        record=dict(pid=pid,creation_time=_identity(pid),runtime=str(root),world_id=read(root/'world-settings.json')['world_id'],started_at=time.time())
        _atomic_json(root/PROCESS,record)
        snapshot=self.processes.inspect(pid)
        if not snapshot:raise RuntimeError('supervisor started but its identity is unavailable; inspect logs before retry')
        record=dict(pid=pid,creation_time=snapshot['creation_time'],runtime=str(root),world_id=read(root/'world-settings.json')['world_id'],started_at=time.time())
        if not matches(snapshot,record,root,'supervisor'):raise RuntimeError('launched supervisor command line does not match its runtime')
        _atomic_json(root/PROCESS,record);self._select(root)
        _atomic_json(root/'controller-config.json',dict(options.controller_snapshot(),runtime=str(root),world_id=record['world_id'],
            supervisor_pid=pid,native_creation_time=record['creation_time']))
        return dict(status='running',runtime=str(root),seed=read(root/'run.json')['seed'],supervisor_pid=pid,reused=False)

    def start(self,*,visible=True):
        with RunLock(self.base/'lifecycle.lock'):return self._start(self._root(),visible=visible)

    def _stop(self,root,*,timeout=90):
        world=read(root/'world-settings.json')['world_id'];record=self._owned(root,'supervisor');request=str(uuid.uuid4())
        if record:
            _atomic_json(root/STOP,dict(request_id=request,world_id=world,source='lifecycle',requested_at=time.time()))
            deadline=self.clock()+timeout
            while True:
                running=self._owned(root,'supervisor')
                state=read(root/STATE,{})
                ack=(state.get('status')=='safe_stopped'and state.get('state')=='stopped'and state.get('world_id')==world
                     and state.get('stop_request_id')==request and state.get('saved')is True)
                if ack and not running:break
                if not running and not ack:raise RuntimeError('supervisor exited without the exact saved stop acknowledgement')
                if self.clock()>=deadline:raise TimeoutError('safe stop acknowledgement pending; no process was killed')
                self.sleep(.25)
        online=bool(self._owned(root,'server'))
        local=None
        if not record and online:
            with RunLock(root/'controller.lock'):pass
            stop=root/'stop.json';owned=False
            prior=read(root/LOCAL_ACK,{})or read(root/STATE,{})
            if stop.exists()and prior.get('saved')is True and prior.get('worker_stop_owned')is True:
                owned=prior.get('worker_stop_sha256')==hashlib.sha256(stop.read_bytes()).hexdigest()
            try:
                fd=os.open(stop,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            except FileExistsError:pass
            else:
                with os.fdopen(fd,'w',encoding='utf-8')as marker:
                    json.dump(dict(source='gameplay-worker-cli',lifecycle_request_id=request,requested_at=time.time()),marker);marker.flush();os.fsync(marker.fileno())
                owned=True
            self._cli(root,'stop-worker')
            deadline=self.clock()+timeout
            while True:
                try:
                    with RunLock(root/'writer.lock'):pass
                    break
                except RuntimeError:
                    if self.clock()>=deadline:raise TimeoutError('ordinary worker action is still running; no process was killed')
                    self.sleep(.25)
            jobs=read(root/'jobs.json',{}).get('jobs',{})
            active=[key for key,q in jobs.items()if q.get('status')in('pending','running','waiting')]
            if len(active)>1:raise RuntimeError('multiple unfinished jobs require inspection; no checkpoint was reset')
            for key in active:self._cli(root,'pause','--key',key)
            local=dict(status='safe_stopped',state='stopped',world_id=world,stop_request_id=request,worker_stop_owned=owned,
                worker_stop_sha256=hashlib.sha256(stop.read_bytes()).hexdigest()if stop.exists()else None,saved=False)
        if online:
            with RunLock(root/'writer.lock'):
                self._game_pause(root,True,request)
            self._checkpoint(root)
            if local:local['saved']=True;_atomic_json(root/LOCAL_ACK,local)
        return dict(status='stopped',runtime=str(root),world_id=world,gui_left_open=True,game_paused=True if online else None,stop_request_id=request)

    def _checkpoint(self,root):
        save=no_mod_save_path(runtime.configuration(root));before=save.stat().st_mtime_ns if save.exists()else None
        result=self._cli(root,'checkpoint');deadline=self.clock()+30;stable=None
        while self.clock()<deadline:
            if save.exists():
                stat=save.stat();signature=(stat.st_mtime_ns,stat.st_size)
                if stat.st_mtime_ns!=before and signature==stable:
                    try:
                        with zipfile.ZipFile(save)as archive:
                            if archive.testzip()is None:return result
                    except (OSError,zipfile.BadZipFile):pass
                stable=signature
            self.sleep(.2)
        raise TimeoutError('checkpoint save file did not finish; world/processes preserved')

    def stop(self,*,timeout=90):
        with RunLock(self.base/'lifecycle.lock'):return self._stop(self._root(),timeout=timeout)

    def gui(self):
        with RunLock(self.base/'lifecycle.lock'):
            root=self._root();self._ensure_server(root);result=self._client(root,visible=True);self._select(root)
            return dict(status='ready',runtime=str(root),client_pid=result.get('pid'),visible=True)

    def _shutdown(self,root,stopped=None):
        stopped=stopped or self._stop(root)
        server=self._owned(root,'server');client=self._owned(root,'client')
        if server:
            cfg=runtime.configuration(root)
            try:
                with FactorioRconClient(cfg.rcon_host,cfg.rcon_port,cfg.rcon_password)as rcon:rcon.execute('/quit',drain_seconds=.4)
            except (OSError,RuntimeError):pass
            deadline=self.clock()+8
            while self._owned(root,'server')and self.clock()<deadline:self.sleep(.2)
            if self._owned(root,'server'):raise TimeoutError('clean server shutdown is pending; server and GUI were preserved')
        record=self._owned(root,'client')
        if record:self.processes.terminate(record)
        return dict(stopped,status='shutdown',gui_left_open=False)

    def shutdown(self):
        with RunLock(self.base/'lifecycle.lock'):return self._shutdown(self._root())

    def _backup(self,root):
        cfg=runtime.configuration(root);save=no_mod_save_path(cfg)
        with zipfile.ZipFile(save)as archive:
            if archive.testzip()is not None:raise RuntimeError('old save ZIP integrity failed; nothing replaced')
        dest=self.base/'backups'/(root.name+'-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
        if not _inside(dest,self.base/'backups')or _inside(dest,root):raise ValueError('invalid backup destination')
        dest.mkdir(parents=True,exist_ok=False);shutil.copy2(save,dest/save.name)
        for file in root.glob('*.json'):shutil.copy2(file,dest/file.name)
        _atomic_json(dest/'backup-manifest.json',dict(source_runtime=str(root),seed=read(root/'run.json')['seed'],save_sha256=hashlib.sha256(save.read_bytes()).hexdigest(),
            scope='saved world ZIP and root JSON checkpoints; original complete runtime retained in place'))
        return dest

    def _ports(self):
        import socket
        for offset in range(100):
            udp,tcp=34261+offset,27061+offset
            try:
                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM)as one,socket.socket(socket.AF_INET,socket.SOCK_STREAM)as two:
                    if os.name=='nt':
                        one.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1);two.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                    one.bind(('127.0.0.1',udp));two.bind(('127.0.0.1',tcp))
                return udp,tcp
            except OSError:continue
        raise RuntimeError('no free bounded v2 server/RCON port pair')

    def new_map(self,*,visible=True):
        options=load_automation_config(self.repo/'automation_config.json')
        with RunLock(self.base/'lifecycle.lock'):
            old=self._root();seed=options.world.seed
            if seed is not None:
                root=self.base/'v2'/('eval-'+str(seed))
                if root.exists():raise FileExistsError('configured new-map seed already has a runtime; nothing stopped or overwritten')
            else:
                for _ in range(16):
                    seed=secrets.randbits(32);root=self.base/'v2'/('eval-'+str(seed))
                    if not root.exists():break
                else:raise FileExistsError('could not choose an unused random runtime; old world preserved')
            stopped=self._stop(old);backup=self._backup(old);self._shutdown(old,stopped)
            server,rcon=self._ports();runtime.create(root,seed=seed,server_port=server,rcon_port=rcon,automation_config=options)
            self._select(root)
            result=self._start(root,visible=visible,automation_config=options)
            return dict(result,backup=str(backup),previous_runtime=str(old))

    def status(self):
        root=self._root();cfg=read(root/'run.json');state=read(root/STATE,{})
        work=read(root/'work_queue.json',{})
        if work and work.get('world_id')!=read(root/'world-settings.json',{}).get('world_id'):
            raise ValueError('task queue belongs to a different world')
        nodes=work.get('nodes',[])
        tasks=[{k:node.get(k)for k in('id','capability','status','priority','depends_on','last_reason')}
               for node in nodes if node.get('status')!='completed'][:32]
        from .supervisor import blocker_summary,_diagnostic_text
        ledger=read(root/'jobs.json',{})
        world=read(root/'world-settings.json',{}).get('world_id')
        if(ledger.get('jobs')or ledger.get('world_id'))and ledger.get('world_id')!=world:
            raise ValueError('job diagnostics belong to a different world')
        key=state.get('active_job_key');job=ledger.get('jobs',{}).get(key)
        current=None
        if job is not None:
            node=next((n for n in nodes if n.get('job_key')==key),{})
            current={k:job.get(k)for k in('capability','status','actions','failures','last_tick')}
            current.update(task_id=node.get('id'),diagnosis=blocker_summary(job))
            since=job.get('progress_at_time')
            if type(since)in(int,float)and since>0:
                until=job.get('paused_at_time')if job.get('status')=='paused'else self.clock()
                if type(until)in(int,float):current['seconds_without_progress']=round(max(0,until-since),1)
        processes={}
        for kind in('server','client','supervisor'):
            try:
                record=self._owned(root,kind);processes[kind]=dict(pid=record['pid'],running=True)if record else dict(running=False)
            except (RuntimeError,OSError)as exc:processes[kind]=dict(running=False,reason=str(exc))
        return dict(runtime=str(root),seed=cfg['seed'],world_id=read(root/'world-settings.json',{}).get('world_id'),processes=processes,
            controller_status=state.get('status'),active_job=state.get('active_job_key'),game_pause=read(root/PAUSE,{}).get('phase'),
            current_task=current,controller_error=_diagnostic_text(state.get('last_error')),
            queue_completed=sum(node.get('status')=='completed'for node in nodes),queue_tasks=tasks)

    def logs(self,*,lines=60,open_folder=False):
        root=self._root();folder=root/'logs'
        if open_folder:
            if os.name!='nt':raise RuntimeError('log folder opening requires Windows')
            os.startfile(str(folder));return dict(log_folder=str(folder))
        if not 1<=lines<=200:raise ValueError('bounded log tail requires 1..200 lines')
        rows={}
        for path in(folder/'supervisor-process.log',folder/'controller-events.jsonl',folder/'controller.log'):
            if path.exists():
                with path.open('rb')as stream:
                    stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-65536));data=stream.read()
                rows[path.name]=data.decode('utf-8',errors='replace').splitlines()[-lines:]
        return dict(runtime=str(root),logs=rows)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--repo',type=Path)
    parser.add_argument('command',choices=('start','stop','new-map','gui','status','logs','shutdown'))
    parser.add_argument('--no-gui',action='store_true');parser.add_argument('--open',action='store_true',dest='open_folder');parser.add_argument('--lines',type=int,default=60)
    args=parser.parse_args(argv);control=Lifecycle(args.repo)
    try:
        if args.command in('start','new-map'):result=getattr(control,args.command.replace('-','_'))(visible=not args.no_gui)
        elif args.command=='logs':result=control.logs(lines=args.lines,open_folder=args.open_folder)
        else:result=getattr(control,args.command)()
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Exception as exc:
        print(json.dumps(dict(status='blocked',reason=str(exc)[:350]),ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
