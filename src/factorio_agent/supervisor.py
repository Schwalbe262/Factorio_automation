"""Periodic Luna task decisions; typed fresh-process execution and durable DAG."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from factorio_ai.deterministic_character import _identity
from factorio_ai.deterministic_state import RunLock,_atomic_json


STATE_FILE=Path('logs/controller-state.json')
STOP_FILE='supervisor-stop.json'
QUEUE_FILE='work_queue.json'
SOURCE_ROOT=Path(__file__).resolve().parents[1]
REPO_ROOT=SOURCE_ROOT.parent
ACTIVE={'pending','running','waiting'}
TERMINAL={'succeeded','blocked','failed'}
OPERATIONS={'plan','request','continue','retry','pause','wait'}
FORBIDDEN={'x','y','position','positions','coordinates','area','entities','entity','lua','rcon','shell','command','cmd','script','code'}
BLOCKER_FIELDS={'reason','last_failure','failure_category','stage','phase','item','resource','product','technology','recipe',
    'required_technology','missing_technologies','required_technologies','required_recipe','missing_recipes',
    'required_recipes','have','need','available','required','missing','count','capacity','supply_per_minute',
    'required_per_minute','available_per_minute','missing_items','required_items','requirements'}
PRIVATE_FIELD=re.compile(r'password|secret|credential|token|auth|api.?key|(?:^|_)(?:path|file|env|position|coordinates|area|entities|entity|lua|rcon|shell|command|cmd|script|code)(?:_|$)',re.I)


def _diagnostic_text(value):
    if not isinstance(value,str):return None
    # Diagnostic strings are descriptions, never a channel for native payloads,
    # coordinates, file paths or credentials embedded in an otherwise safe key.
    if re.search(r'password|secret|credential|authorization|bearer|api.?key|\btoken\b|[A-Za-z]:[\\/]|(?:^|\s)/[A-Za-z]|[{}\[\]]|(?:\bx|\by)\s*[:=]|[-+]?\d+(?:\.\d+)?\s*,\s*[-+]?\d|\b(?:rcon|powershell|shell|lua|cmd)\b',value,re.I):return '<redacted>'
    return value[:160]


def blocker_summary(job):
    """Small semantic diagnosis; arbitrary executor payloads stay out of prompts."""
    remaining=[20]
    def compact(value,depth=0,named_counts=False):
        if depth>3 or remaining[0]<=0:return None
        if isinstance(value,dict):
            result={};details=[]
            for key,item in list(value.items())[:48]:
                if not isinstance(key,str)or key.lower()in FORBIDDEN or PRIVATE_FIELD.search(key):continue
                key=key.lower()
                if key in BLOCKER_FIELDS:
                    clean=compact(item,depth+1,key in {'missing_items','required_items','requirements'})
                    if clean is not None:result[key]=clean
                elif named_counts and re.fullmatch(r'[a-z][a-z0-9-]{0,79}',key)and type(item)in(int,float)and math.isfinite(item):
                    remaining[0]-=1;result[key]=item
                elif isinstance(item,(dict,list))and len(details)<3:
                    clean=compact(item,depth+1)
                    if clean:details.append(clean)
                if remaining[0]<=0 or len(result)>=8:break
            if details:result['details']=details
            return result or None
        if isinstance(value,list):
            rows=[compact(v,depth+1)for v in value[:4]]
            return [v for v in rows if v is not None]or None
        if isinstance(value,str):
            remaining[0]-=1;return _diagnostic_text(value)
        if type(value)in(int,float,bool)and math.isfinite(value):
            remaining[0]-=1;return value
        return None
    return {key:clean for key in ('reason','last_failure','evidence')
        if(clean:=compact(job.get(key)))is not None}


def _terminal_signature(status):
    return {j['key']:(j['status'],j.get('reason','')if j['status']in {'blocked','failed'}else'')
        for j in status.get('jobs',{}).values()if j['status']in TERMINAL}


def _hash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def _raw_hash(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _positive(value):return type(value)in(int,float)and math.isfinite(value)and value>0
def _same_actor(a,b):return {k:v for k,v in a.items()if k!='native_repair_pending'}=={k:v for k,v in b.items()if k!='native_repair_pending'}


def _no_raw(value):
    if isinstance(value,dict):
        if any(not isinstance(k,str)or k.lower()in FORBIDDEN for k in value):raise ValueError('raw coordinates or commands are forbidden')
        for q in value.values():_no_raw(q)
    elif isinstance(value,list):
        for q in value:_no_raw(q)
    elif type(value)not in(str,int,float,bool,type(None))or(type(value)is float and not math.isfinite(value)):
        raise ValueError('finite JSON parameters required')


def validate_decision(value):
    if not isinstance(value,dict)or set(value)-{'operation','capability','parameters','reason','queue'}:
        raise ValueError('decision must contain only the structured task schema')
    if value.get('operation')not in OPERATIONS or not isinstance(value.get('parameters'),dict)or not isinstance(value.get('reason'),str)or len(value['reason'])>2000:
        raise ValueError('invalid operation, parameters or reason')
    cap=value.get('capability')
    if cap is not None and(not isinstance(cap,str)or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}',cap)):raise ValueError('invalid capability name')
    if value['operation']=='request'and cap is None:raise ValueError('request needs a capability')
    nodes=value.get('queue',[])
    if not isinstance(nodes,list)or len(nodes)>64:raise ValueError('queue decision accepts at most64 nodes')
    _no_raw(value['parameters'])
    return dict(value,queue=nodes)


def stable_job_key(world,node):return 'lunaq_'+_hash([world,node['id'],node['capability'],node['parameters']])[:48]


class WorkQueue:
    """World-bound append/merge queue; submitted task identity never changes."""
    def __init__(self,path,world):
        self.path,self.world=Path(path),world
        self.data=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else dict(schema_version=1,world_id=world,nodes=[])
        if self.data.get('schema_version')!=1 or self.data.get('world_id')!=world:raise ValueError('work queue world or format changed')
        if not isinstance(self.data.get('nodes'),list):raise ValueError('invalid persistent work queue')
        if len(self.data['nodes'])>512 or len({q['id']for q in self.data['nodes']})!=len(self.data['nodes']):raise ValueError('invalid unique queue history')
        for node in self.data['nodes']:
            if node.get('job_key')!=stable_job_key(world,node):raise ValueError('persistent queue job identity changed')
            _no_raw(node['parameters'])

    def save(self):_atomic_json(self.path,self.data)

    def merge(self,nodes,capabilities,validator,tick):
        result=deepcopy(self.data);index={q['id']:q for q in result['nodes']};incoming=set()
        for node in nodes:
            required={'id','capability','parameters','priority','depends_on','reason'}
            if not isinstance(node,dict)or set(node)!=required:raise ValueError('queue node requires exactly id/capability/parameters/priority/depends_on/reason')
            name=node['id'];cap=node['capability'];deps=node['depends_on']
            if(not isinstance(name,str)or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',name)or name in incoming
                    or cap not in capabilities or not isinstance(node['parameters'],dict)
                    or type(node['priority'])is not int or not -10000<=node['priority']<=10000
                    or not isinstance(deps,list)or any(not isinstance(d,str)for d in deps)or name in deps
                    or not isinstance(node['reason'],str)or len(node['reason'])>2000):raise ValueError('invalid unique typed queue node')
            # A DAG edge is a set membership, not a sequence or a multiset.
            # Repeated/reordered edges cannot change execution semantics.
            deps=sorted(set(deps))
            _no_raw(node['parameters']);incoming.add(name)
            old=index.get(name)
            if old:
                if old['capability']!=cap or old['parameters']!=node['parameters']:raise ValueError('queue task identity is immutable; use a new node id')
                if old['status']in('running','completed'):
                    added=set(deps)-set(old['depends_on']);removed=set(old['depends_on'])-set(deps)
                    if added or removed:
                        changes='; added='+(' '.join(sorted(added)[:4])or'none')+'; removed='+(' '.join(sorted(removed)[:4])or'none')
                        raise ValueError('cannot change dependencies after execution starts: '+name+changes)
                    deps=list(old['depends_on'])
                old.update(priority=node['priority'],depends_on=list(deps),reason=node['reason'])
                if old['status']not in('running','completed'):old.pop('dispatch_blocked',None)
            else:
                row=dict(deepcopy(node),depends_on=list(deps),status='pending',job_key=stable_job_key(self.world,node),created_at=time.time(),paused=False)
                result['nodes'].append(row);index[name]=row
        if len(result['nodes'])>512:raise ValueError('persistent queue exceeds512 nodes; explicit archival needed')
        visiting=set();done=set()
        def visit(name):
            if name not in index:raise ValueError('unknown queue dependency: '+name)
            if name in visiting:raise ValueError('queue dependency cycle')
            if name in done:return
            visiting.add(name)
            for d in index[name]['depends_on']:visit(d)
            visiting.remove(name);done.add(name)
        for name in index:visit(name)
        validator([dict(key=q['job_key'],world_id=self.world,observed_tick=tick,capability=q['capability'],parameters=q['parameters'])for q in result['nodes']if q['id']in incoming])
        self.data=result;self.save()

    def refresh(self,jobs,capabilities):
        by_key={q['key']:q for q in jobs};completed={q['capability']for q in jobs if q['status']=='succeeded'}
        nodes={q['id']:q for q in self.data['nodes']}
        # First reconcile receipts, then calculate dependency readiness so the
        # insertion order of a valid DAG does not add an unnecessary poll delay.
        for q in nodes.values():
            job=by_key.get(q['job_key'])
            if job:
                if(job.get('world_id')!=self.world or job.get('capability')!=q['capability']or job.get('parameters')!=q['parameters']):
                    raise ValueError('queue/native job identity differs')
                q['submitted']=True
                q['last_job_status']=job['status'];q['last_reason']=job.get('reason','')
                q['last_blocker']=blocker_summary(job)if job['status']in {'waiting','blocked','failed'}else{}
                q['status']='completed'if job['status']=='succeeded'else'running'if job['status']in ACTIVE else'blocked'
            elif q.get('submitted'):q.update(status='blocked',last_reason='submitted_job_receipt_missing')
            elif q.get('status')not in('completed','blocked'):q['status']='pending'
        for q in nodes.values():
            if q['job_key']in by_key or q['status']=='completed':continue
            if q.get('dispatch_blocked'):q['status']='blocked';continue
            if q['capability']not in capabilities:q.update(status='blocked',last_reason='capability_no_longer_available');continue
            missing=[d for d in q['depends_on']if nodes[d]['status']!='completed']
            prerequisites=[p for p in capabilities[q['capability']].get('requires',[])if p not in completed]
            q['missing_dependencies']=missing;q['missing_prerequisites']=prerequisites
            q['status']='ready'if not missing and not prerequisites and not q.get('paused')else'pending'
        self.save()

    def ready(self):
        rows=[q for q in self.data['nodes']if q['status']=='ready']
        return min(rows,key=lambda q:(-q['priority'],q['created_at'],q['id']))if rows else None

    def node_for_job(self,key):return next((q for q in self.data['nodes']if q['job_key']==key),None)

    def compact(self):
        return [{k:q.get(k)for k in('id','capability','parameters','priority','depends_on','status','job_key','missing_dependencies','missing_prerequisites')}
            |{'last_reason':_diagnostic_text(q.get('last_reason','')),'blocker':blocker_summary(q.get('last_blocker',{}))}
            for q in self.data['nodes']if q['status']!='completed'][:64]

    def compact_completed(self):
        return [{k:q.get(k)for k in ('id','capability','parameters','status','depends_on')}
            for q in self.data['nodes']if q['status']=='completed'][-16:]


class CLIError(RuntimeError):
    pass


class CLIBridge:
    """Fixed argv, never a shell; each CLI call imports current source anew."""
    def __init__(self,runtime,*,on_spawn=None,on_exit=None):
        self.runtime=Path(runtime).resolve();self.on_spawn=on_spawn;self.on_exit=on_exit

    def _execute(self,args,*,payload=None,stop_requested=None,on_stop=None):
        env=dict(os.environ,PYTHONPATH=str(SOURCE_ROOT));flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        process=subprocess.Popen([str(Path(sys.executable).resolve()),'-B','-m',*args],cwd=str(REPO_ROOT),env=env,
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,encoding='utf-8',errors='replace',creationflags=flags)
        if self.on_spawn:self.on_spawn(process,args)
        sent=False;stopped=False
        while True:
            if stop_requested and stop_requested()and not stopped:
                stopped=True
                if on_stop:
                    try:on_stop()
                    except Exception:
                        # Never abandon a possibly mutating child at this
                        # boundary. Let it finish its safe slice, then the
                        # supervisor will pause/save or report a failed ACK.
                        pass
            try:
                stdout,stderr=process.communicate(input=payload if not sent else None,timeout=.25);break
            except subprocess.TimeoutExpired:sent=True
        if self.on_exit:self.on_exit(process,args,stdout,stderr)
        # A CLI receipt is exactly one JSON object. Worker streaming is never
        # used; run is one bounded slice with a final durable job receipt.
        try:result=json.loads(stdout.strip())
        except ValueError as exc:raise CLIError('invalid typed CLI receipt: '+stderr[-800:])from exc
        if process.returncode or not isinstance(result,dict)or result.get('status')=='error':
            raise CLIError(str(result.get('reason','typed CLI failure'))if isinstance(result,dict)else'invalid CLI object')
        return result

    def invoke(self,args,*,stop_requested=None,on_stop=None):
        return self._execute(['factorio_agent','--runtime',str(self.runtime),*args],stop_requested=stop_requested,on_stop=on_stop)

    def validate_requests(self,rows):
        if not rows:return
        result=self._execute(['factorio_agent.supervisor','--validate-requests'],payload=json.dumps(rows,allow_nan=False))
        if result.get('ok')is not True:raise ValueError('typed request batch rejected')


def actor_binding(value,world):
    if value.get('ok')is not True or value.get('world_id')!=world:raise ValueError('native actor/world inspection failed')
    old=value.get('old_pointer',{});successor=value.get('successor',{});owner=value.get('owner',{})
    if(not old.get('valid')or type(old.get('unit_number'))is not int or old['unit_number']<1
            or value.get('old_unit')!=old['unit_number']or not successor.get('valid')or successor.get('unit_number')!=old['unit_number']
            or owner.get('index')!=value.get('pinned_owner_index')or not owner.get('name')
            or old.get('surface_index')!=value.get('surface_index')or successor.get('surface_index')!=old.get('surface_index')
            or successor.get('force_index')!=old.get('force_index')or not old.get('force_index')):
        raise ValueError('current framework actor is not the exact native pinned owner character')
    return dict(unit_number=old['unit_number'],owner_index=owner['index'],owner_name=owner['name'],surface_index=old['surface_index'],
        force_index=old['force_index'],native_repair_pending=bool(value.get('native_repair_pending')))


def compact_snapshot(observation,status,capabilities,work,actor,planner_error=None):
    def compact(value,depth=0):
        if depth>5:return '<depth limit>'
        if isinstance(value,dict):return {k:compact(v,depth+1)for k,v in list(value.items())[:64]if k.lower()not in FORBIDDEN}
        if isinstance(value,list):return [compact(v,depth+1)for v in value[:48]]
        if isinstance(value,str):return value[:600]
        return value
    jobs=list(status.get('jobs',{}).values());recent=sorted(jobs,key=lambda q:q.get('created_at',0),reverse=True)
    return dict(schema_version=1,world_id=observation['world_id'],observed_tick=observation['tick'],actor=actor,
        objective='Progress to a real Space Age rocket using typed capabilities, ordinary costs and current native prerequisites.',
        native_researched_technologies=observation.get('technologies',[]),current_research=observation.get('research'),
        native_enabled_recipes=observation.get('enabled_recipes',[]),
        research_progress=observation.get('research_progress'),available_research=compact(observation.get('available_research')),
        inventory=compact(observation.get('inventory',{})),entity_counts=compact(observation.get('entities',{})),
        electricity=compact(observation.get('electricity')),factory_constraints=compact(observation.get('factory_constraints')),
        construction_budget=compact(observation.get('known_project_construction_budget')),crafting_queue=compact(observation.get('crafting_queue')),
        production_rate_measurement=compact(observation.get('production_rate_measurement')),
        capabilities={k:{'parameters':v.get('parameters',{}),'requires':v.get('requires',[]),'effect':v.get('effect','')[:160]}for k,v in capabilities.items()},
        verified_capabilities=sorted({q['capability']for q in jobs if q['status']=='succeeded'}),
        jobs=[{k:q.get(k)for k in('key','capability','parameters','status','actions','last_tick',
            'created_tick','planning_seconds','execution_seconds')}
            |blocker_summary(q)|{'uncertain_action':bool(q.get('pending_action'))}for q in recent[:12]],
        work_queue=work.compact(),recent_completed_queue=work.compact_completed(),last_planner_error=_diagnostic_text(planner_error),
        policy='Only structured task/DAG decisions; no shell, coordinates, RCON, research setters or model fallback. Verification and actual execution remain in the framework.')


class Supervisor:
    def __init__(self,runtime,client,*,cli=None,decision_interval=60,slice_seconds=25,max_seconds=None,
                 minimum_decision_interval=10,clock=time.time,sleep=time.sleep):
        self.root=Path(runtime)
        if not self.root.is_absolute()or not self.root.is_dir():raise ValueError('supervisor requires an existing absolute runtime')
        if not _positive(decision_interval)or not _positive(slice_seconds)or slice_seconds>50 or(max_seconds is not None and not _positive(max_seconds)):
            raise ValueError('positive decision interval, slice<=50 and optional duration required')
        self.root=self.root.resolve();self.clock,self.sleep=clock,sleep;self.client=client
        self.decision_interval=decision_interval;self.slice_seconds=slice_seconds;self.max_seconds=max_seconds
        self.minimum_decision_interval=min(minimum_decision_interval,decision_interval)
        self.world=json.loads((self.root/'world-settings.json').read_text(encoding='utf-8'))['world_id']
        if not isinstance(self.world,str)or not self.world:raise ValueError('installed world identity required')
        self.work=WorkQueue(self.root/QUEUE_FILE,self.world)
        self.state_path=self.root/STATE_FILE;self.event_path=self.root/'logs/controller-events.jsonl'
        prior=json.loads(self.state_path.read_text(encoding='utf-8'))if self.state_path.exists()else{}
        self.resume_job_key=prior.get('pause_receipt',{}).get('key')if(prior.get('world_id')==self.world and prior.get('saved')is True and prior.get('status')=='safe_stopped'and isinstance(prior.get('pause_receipt'),dict))else None
        self.controller_id=uuid.uuid4().hex;created=_identity(os.getpid());now=self.clock()
        self.state=dict(schema_version=1,controller_id=self.controller_id,pid=os.getpid(),creation_time=created,native_creation_time=created,
            process_creation_time=created/10000000-11644473600 if created else now,started_at=now,runtime=str(self.root),source_root=str(SOURCE_ROOT),
            world_id=self.world,status='starting',state='running',active_job_key=None,decision_interval=decision_interval,slice_seconds=slice_seconds,
            model='gpt-6-luna',reasoning_effort='medium',last_decision_at=None,next_decision_at=now,decision_sequence=int(prior.get('decision_sequence',0)),worker_stop_owned=False,saved=False,resume_job_key=self.resume_job_key)
        self.cli=cli or CLIBridge(self.root,on_spawn=self._child_spawn,on_exit=self._child_exit)
        self.stop_marker=None;self.stopping=False;self.last_job_signature=None;self.last_snapshot=None;self.wait_until=0
        self.errors=0;self.backoff_until=0;self.last_decision_time=None;self.force_decision=True;self.pending_decision=None

    def _save(self,**updates):
        self.state.update(updates,updated_at=self.clock());_atomic_json(self.state_path,self.state)

    def _event(self,kind,**data):
        self.event_path.parent.mkdir(parents=True,exist_ok=True)
        with self.event_path.open('a',encoding='utf-8')as out:out.write(json.dumps(dict(time=self.clock(),controller_id=self.controller_id,event=kind,**data),ensure_ascii=False,allow_nan=False)+'\n')

    def _child_spawn(self,process,args):
        self._save(child=dict(pid=process.pid,native_creation_time=_identity(process.pid),command=args[-3:]if '--validate-requests'not in args else['validate-requests']))

    def _child_exit(self,process,args,stdout,stderr):
        # Full job ledgers/observations are parsed in memory, never copied into
        # the model/event log. Keep bounded stderr for failed command diagnosis.
        self._event('cli_exit',pid=process.pid,returncode=process.returncode,command='validate-requests'if '--validate-requests'in args else next((a for a in args if a in{'observe','status','capabilities','actor-status','request','run','pause','resume','retry','checkpoint','stop-worker'}),'typed_cli'),stderr=stderr[-1600:])
        self._save(child=None)

    def _world_guard(self):
        if json.loads((self.root/'world-settings.json').read_text(encoding='utf-8')).get('world_id')!=self.world:raise ValueError('installed world changed; supervisor will not retarget')

    def _stop_requested(self):
        if self.stopping:return True
        path=self.root/STOP_FILE
        if path.exists():
            try:marker=json.loads(path.read_text(encoding='utf-8'))
            except(ValueError,OSError):marker={'request_id':'invalid-marker','world_id':self.world,'invalid':True}
            self.stop_marker=marker;self.stopping=True;self._save(status='stopping',stop_request_id=marker.get('request_id'),stop_requested_at=self.clock())
            return True
        if self.max_seconds is not None and self.clock()-self.state['started_at']>=self.max_seconds:
            self.stop_marker={'request_id':'duration-'+self.controller_id,'world_id':self.world,'source':'max_seconds'}
            self.stopping=True;self._save(status='stopping',stop_request_id=self.stop_marker['request_id']);return True
        return False

    def _worker_stop(self):
        self._world_guard();path=self.root/'stop.json'
        payload=json.dumps(dict(source='gameplay-worker-cli',controller_id=self.controller_id,stop_request_id=(self.stop_marker or{}).get('request_id'),requested_at=self.clock()),separators=(',',':')).encode()
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:pass
        else:
            with os.fdopen(fd,'wb')as out:out.write(payload);out.flush();os.fsync(out.fileno())
            self._save(worker_stop_owned=True,worker_stop_sha256=hashlib.sha256(payload).hexdigest())
        self.cli.invoke(['stop-worker'])

    def _snapshot(self):
        self._world_guard()
        observation=self.cli.invoke(['observe']);status=self.cli.invoke(['status']);capabilities=self.cli.invoke(['capabilities'])
        actor=actor_binding(self.cli.invoke(['actor-status']),self.world)
        if observation.get('world_id')!=self.world or status.get('world_id')not in(None,self.world):raise ValueError('native observation/jobs disagree with installed world')
        tick=observation.get('tick')
        if type(tick)is not int or tick<0 or tick<self.state.get('last_observed_tick',0)or 'rollback_tick'in status:raise ValueError('world rollback requires explicit reconciliation')
        jobs=list(status.get('jobs',{}).values());active=[j for j in jobs if j['status']in ACTIVE]
        if len(active)>1:raise ValueError('multiple native jobs active in one world')
        self.work.refresh(jobs,capabilities)
        self._save(last_observed_tick=tick,actor=actor,active_job_key=active[0]['key']if active else None)
        snapshot=compact_snapshot(observation,status,capabilities,self.work,actor,self.state.get('last_error'))
        self.last_snapshot=snapshot
        return observation,status,capabilities,actor,snapshot

    def _decision_due(self,status):
        now=self.clock()
        if self.pending_decision is not None:return False
        if now<self.backoff_until:return False
        jobs=list(status.get('jobs',{}).values())
        if now<self.wait_until and not any(j['status']in ACTIVE for j in jobs)and self.work.ready()is None:return False
        if self.last_decision_time is None:return True
        if now-self.last_decision_time<self.minimum_decision_interval:return False
        signature=_terminal_signature(status)
        event=any((self.last_job_signature or{}).get(key)!=value for key,value in signature.items())
        low=len([n for n in self.work.data['nodes']if n['status']in('pending','ready')])<2
        return self.force_decision or event or now-self.last_decision_time>=self.decision_interval or(low and now-self.last_decision_time>=max(self.minimum_decision_interval,self.decision_interval/2))

    def _start_decision(self,context):
        snapshot=context[4]
        result=queue.Queue(maxsize=1)
        def call():
            try:result.put((True,self.client.decide(snapshot)))
            except BaseException as exc:result.put((False,exc))
        self.last_decision_time=self.clock();self.state['decision_sequence']+=1
        self.pending_decision=dict(result=result,context=context,started_at=self.last_decision_time)
        self._save(status='deciding',luna_pending=True,last_decision_started_at=self.last_decision_time)
        threading.Thread(target=call,daemon=True,name='luna-structured-decision').start()

    def _poll_decision(self):
        if self.pending_decision is None:return False
        if self._stop_requested():return False
        pending=self.pending_decision
        try:ok,value=pending['result'].get_nowait()
        except queue.Empty:return False
        self.pending_decision=None;self._save(luna_pending=False)
        if not ok:raise RuntimeError(str(value))from value
        decision=validate_decision(value);self._apply(decision,pending['context'])
        # A terminal change that happened while Luna was thinking was not in
        # its prompt and must remain visible as the next bounded event.
        self.last_job_signature=_terminal_signature(pending['context'][1])
        self.force_decision=False;self.errors=0;self.backoff_until=0
        self._save(last_decision_at=self.clock(),next_decision_at=self.last_decision_time+self.decision_interval,last_decision=decision,status='running',last_error=None,
            last_luna_usage=getattr(self.client,'last_usage',None),last_luna_session_id=getattr(self.client,'last_session_id',None))
        return True

    def _target(self,decision,status,allowed,*,missing_ok=False):
        rows=[j for j in status.get('jobs',{}).values()if j['status']in allowed]
        cap=decision.get('capability');params=decision.get('parameters',{})
        if cap is not None:rows=[j for j in rows if j['capability']==cap]
        if params:rows=[j for j in rows if j.get('parameters')==params]
        active=[j for j in rows if j['status']in ACTIVE]
        if active:rows=active
        if not rows and missing_ok:return None
        if len(rows)!=1:raise ValueError('operation requires one unambiguous existing job')
        if rows[0].get('world_id')!=self.world:raise ValueError('job world identity changed')
        return rows[0]

    def _resume_known(self,context):
        if not self.resume_job_key:return False
        job=context[1].get('jobs',{}).get(self.resume_job_key)
        if not job or job.get('world_id')!=self.world:raise ValueError('saved paused job identity missing; no unrelated replacement')
        if job.get('pending_action')or job.get('reason')=='action_outcome_unknown_reconcile_required':raise ValueError('saved job requires reviewed unknown-outcome reconciliation')
        if job['status']=='paused':
            if self._stop_requested():return False
            self.cli.invoke(['resume','--key',job['key']])
            self._event('resume_after_safe_start',key=job['key'])
        elif job['status']not in ACTIVE|TERMINAL:raise ValueError('saved job cannot be safely resumed')
        self.resume_job_key=None;self._save(resume_job_key=None)
        return True

    def _apply(self,decision,context):
        observation,prompt_status,_,actor,_=context
        if self._stop_requested():return
        # A potentially slow model call cannot authorize a stale actor/world.
        self._world_guard();fresh=self.cli.invoke(['observe']);fresh_actor=actor_binding(self.cli.invoke(['actor-status']),self.world)
        if fresh.get('world_id')!=self.world or not _same_actor(fresh_actor,actor):raise ValueError('world/actor changed during model decision; discard and observe again')
        status=self.cli.invoke(['status']);capabilities=self.cli.invoke(['capabilities'])
        if status.get('world_id')not in(None,self.world)or 'rollback_tick'in status or fresh.get('tick',-1)<observation['tick']:
            raise ValueError('fresh decision status world or tick changed')
        jobs=list(status.get('jobs',{}).values());active=[j for j in jobs if j['status']in ACTIVE]
        if len(active)>1:raise ValueError('multiple native jobs active during model decision')
        if self._stop_requested():return
        self.work.refresh(jobs,capabilities)
        self.work.merge(decision['queue'],capabilities,self.cli.validate_requests,fresh['tick'])
        operation=decision['operation'];self._event('decision',operation=operation,capability=decision.get('capability'),reason=decision['reason'],queue_nodes=len(decision['queue']))
        if operation=='request':
            if active:
                self._event('decision_deferred',operation=operation,reason='fresh_active_job_must_finish',key=active[0]['key']);return
            node=dict(id='now_'+str(self.state['decision_sequence'])+'_'+_hash([decision['capability'],decision['parameters']])[:12],capability=decision['capability'],
                parameters=decision['parameters'],priority=0,depends_on=[],reason=decision['reason'])
            self.work.merge([node],capabilities,self.cli.validate_requests,fresh['tick'])
            self.wait_until=0
        elif operation in('continue','retry','pause'):
            allowed=ACTIVE|{'paused'}if operation in('continue','pause')else{'blocked'}
            # Bind intent to the job Luna saw, never whichever successor has
            # become active during the asynchronous call.
            intended=self._target(decision,prompt_status,allowed,missing_ok=True)
            if intended is None:
                self._event('stale_decision',operation=operation,reason='no_target_in_decision_snapshot');return
            job=status.get('jobs',{}).get(intended['key'])
            if not job or any(job.get(k)!=intended.get(k)for k in ('key','world_id','capability','parameters')):
                raise ValueError('original decision target identity missing or changed; will not retarget')
            if job['status']not in allowed or(operation=='pause'and job['status']=='paused'):
                self._event('stale_decision',operation=operation,key=job['key'],reason='original_target_no_longer_eligible',status=job['status']);return
            if operation in ('retry','continue')and job['status']not in ACTIVE and active:
                self._event('stale_decision',operation=operation,key=job['key'],reason='another_job_already_active');return
            node=self.work.node_for_job(job['key'])
            command='resume'if operation=='continue'and job['status']=='paused'else operation
            if operation=='retry'and(job.get('pending_action')or job.get('reason')=='action_outcome_unknown_reconcile_required'):
                raise ValueError('uncertain outcome requires reviewed reconciliation, never model retry')
            if command!='continue':
                if self._stop_requested():return
                self.cli.invoke([command,'--key',job['key']])
            if node:node['paused']=operation=='pause';self.work.save()
            self.wait_until=0
        elif operation=='wait':self.wait_until=self.clock()+self.decision_interval
        else:self.wait_until=0

    def _run_authorized(self,context):
        observation,status,capabilities,actor,_=context
        if self._stop_requested():return False
        jobs=list(status.get('jobs',{}).values());active=[j for j in jobs if j['status']in ACTIVE]
        if len(active)>1:raise ValueError('multiple jobs active')
        if not active:
            self.work.refresh(jobs,capabilities);node=self.work.ready()
            if node is None:return False
            if(self.root/'stop.json').exists():raise ValueError('worker stop marker present; explicit lifecycle start/ownership check required')
            self._world_guard();fresh=self.cli.invoke(['observe']);binding=actor_binding(self.cli.invoke(['actor-status']),self.world)
            if fresh.get('world_id')!=self.world or not _same_actor(binding,actor):raise ValueError('world/actor changed before queued request')
            if self._stop_requested():return False
            try:
                self.cli.invoke(['request','--key',node['job_key'],'--world-id',self.world,'--observed-tick',str(fresh['tick']),
                    '--capability',node['capability'],'--parameters',json.dumps(node['parameters'],separators=(',',':'),allow_nan=False)])
            except Exception as exc:
                node.update(status='blocked',dispatch_blocked=True,last_reason=str(exc));self.work.save();raise
            node.update(status='running',submitted=True);self.work.save();key=node['job_key']
        else:key=active[0]['key']
        if(self.root/'stop.json').exists():raise ValueError('worker stop marker present; no new run slice')
        if self._stop_requested():return False
        self._world_guard();self._save(status='running',active_job_key=key)
        receipt=self.cli.invoke(['run','--key',key,'--seconds',str(self.slice_seconds)],stop_requested=self._stop_requested,on_stop=self._worker_stop)
        self._event('slice',key=key,status=receipt.get('status'),reason=receipt.get('reason'),actions=receipt.get('actions'),tick=receipt.get('last_tick'))
        return True

    def safe_stop(self):
        marker=self.stop_marker or{'request_id':'exit-'+self.controller_id,'world_id':self.world,'source':'supervisor_exit'}
        self._save(status='stopping',stop_request_id=marker.get('request_id'))
        self.pending_decision=None
        cancel=getattr(self.client,'cancel',None)
        if callable(cancel):cancel()
        try:
            self._world_guard()
            if marker.get('world_id')!=self.world or marker.get('invalid'):raise ValueError('stop marker world or format changed')
            status=self.cli.invoke(['status']);jobs=list(status.get('jobs',{}).values())
            if status.get('world_id')not in(None,self.world):raise ValueError('job world changed while stopping')
            active=[j for j in jobs if j['status']in ACTIVE]
            if len(active)>1:raise ValueError('more than one active job while stopping')
            pause=None
            if active:pause=self.cli.invoke(['pause','--key',active[0]['key']])
            checkpoint=self.cli.invoke(['checkpoint'])
            if checkpoint.get('world_id')!=self.world or checkpoint.get('status')!='checkpoint_requested':raise ValueError('native checkpoint receipt missing')
            self._save(status='safe_stopped',state='stopped',saved=True,pause_receipt=pause,checkpoint_receipt=checkpoint,
                stop_request_id=marker.get('request_id'),active_job_key=None,stopped_at=self.clock())
            self._event('safe_stopped',stop_request_id=marker.get('request_id'),saved=True)
        except Exception as exc:
            self._save(status='blocked',state='stopping',saved=False,stop_error=str(exc));self._event('stop_failed',reason=str(exc))
        return deepcopy(self.state)

    def run(self):
        with RunLock(self.root/'controller.lock'):
            self._save()
            try:
                while not self._stop_requested():
                    try:
                        context=self._snapshot()
                        if self._stop_requested():break
                        if self._resume_known(context):context=self._snapshot()
                        if self._poll_decision():
                            if self._stop_requested():break
                            context=self._snapshot()
                        if self._decision_due(context[1]):
                            self._start_decision(context)
                        if self._stop_requested():break
                        # Already approved queue successors run while the next
                        # bounded Luna call is pending; no per-action model wait.
                        if not self._run_authorized(context):
                            until=self.clock()+2
                            while self.clock()<until and not self._stop_requested():
                                if self._poll_decision():break
                                self.sleep(.25)
                    except Exception as exc:
                        self.errors+=1;self.backoff_until=self.clock()+min(300,10*2**min(5,self.errors-1))
                        self._save(status='backoff',last_error=str(exc),next_decision_at=self.backoff_until)
                        self._event('error',reason=str(exc),backoff_until=self.backoff_until)
                        # A rejected model response cannot invent a fallback.
                        # Already authorized work is revisited only after a new
                        # native snapshot, with bounded polling during backoff.
                        until=self.clock()+min(1,max(0,self.backoff_until-self.clock()))
                        while self.clock()<until and not self._stop_requested():self.sleep(.25)
            except KeyboardInterrupt:
                self.stopping=True;self.stop_marker={'request_id':'interrupt-'+self.controller_id,'world_id':self.world}
            finally:return self.safe_stop()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path)
    parser.add_argument('--decision-interval',type=float,default=60)
    parser.add_argument('--slice-seconds',type=float,default=25)
    parser.add_argument('--max-seconds',type=float)
    parser.add_argument('--validate-requests',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if args.validate_requests:
        from .jobs import Request
        rows=json.load(sys.stdin)
        if not isinstance(rows,list)or len(rows)>64:raise ValueError('bounded typed request batch required')
        for row in rows:Request(**row).validate()
        print(json.dumps({'ok':True}));return
    if args.runtime is None:parser.error('--runtime ABS is required')
    from .luna_client import LunaDecisionClient
    client=LunaDecisionClient(model='gpt-6-luna',reasoning_effort='medium',timeout_seconds=120)
    result=Supervisor(args.runtime,client,decision_interval=args.decision_interval,slice_seconds=args.slice_seconds,max_seconds=args.max_seconds).run()
    print(json.dumps({k:result.get(k)for k in('status','world_id','saved','stop_request_id')},ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except(Exception,)as exc:
        print(json.dumps({'status':'error','reason':str(exc)},ensure_ascii=False),file=sys.stderr);raise SystemExit(1)
