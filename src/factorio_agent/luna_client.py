"""Structured strategy decisions through the user's authenticated Codex CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading


OPERATIONS=('plan','request','continue','retry','pause','wait')
PARAMETERS={'type':'string','description':'A JSON object of documented capability parameters; use {} for no parameters.'}
NODE={'type':'object','additionalProperties':False,'required':['id','capability','parameters_json','priority','depends_on','reason'],
      'properties':{'id':{'type':'string','maxLength':64},'capability':{'type':'string'},'parameters_json':PARAMETERS,
                    'priority':{'type':'integer','minimum':0,'maximum':1000},
                    'depends_on':{'type':'array','items':{'type':'string'}},'reason':{'type':'string'}}}
DECISION_SCHEMA={'type':'object','additionalProperties':False,'required':['operation','capability','parameters_json','queue','reason','target_task_id'],
 'properties':{'operation':{'type':'string','enum':list(OPERATIONS)},'capability':{'type':['string','null']},
               'target_task_id':{'type':['string','null'],'maxLength':64,'pattern':'^[A-Za-z0-9_-]{1,64}$',
                    'description':'Existing stable work_queue ID for continue/retry/pause, otherwise null; never a job key.'},
               'parameters_json':PARAMETERS,'queue':{'type':'array','items':NODE},'reason':{'type':'string'}}}

PROMPT='''You are the strategy planner for a Factorio Space Age framework.
Return only the required structured decision. Do not call tools, browse files,
execute commands, modify code, or interact with the game. The snapshot contains
your entire allowed context. The framework executes validated capabilities.
Plan a small dependency queue so ready work can continue without another model
call. Replenish that queue before its remaining ready work runs out; include
missing prerequisites as tasks instead of repeatedly selecting an unavailable
target. Repeated material waits are a reason to assess producer capacity using
the observed item/have/need evidence and available capacity capabilities.
Compare measured item production per minute with the outstanding raw-material
construction budget. Estimate the wait before committing to slow serial supply;
invest in the resource that actually limits construction. Repeated one-item
collections do not establish adequate capacity. A paused job preserves its paid
work: if supply dominates the delay, pause that exact job, queue a bounded
capacity improvement, then continue the original job after the improvement.
Zero measured production with FULL output buffers is not evidence of a shortage.
Use available stocks and current producer statuses before requesting expansion.
Read resource_supply_options: collect existing stock or advance when the bill
is funded; clear downstream blockage before adding supply. For a real shortage,
compare net output, extra-cell costs, projected wait and payback. An expansion
must shorten the current limiting wait and remain affordable in all inputs.
After native electric mining unlocks, expand iron/copper through mining_outpost
and smelting rather than new large burner starter_capacity targets. Up to two
starter cells remain available for bootstrapping or recovery; paid cells stay.
Finish or repair an existing funded science batch before parallel handcrafting
or starting another instance of the same research work.
There is one active task: ready capacity work cannot run concurrently with a
material-starved job. Merely queuing that investment behind the active task
cannot shorten its wait. Explicitly pause it or approve a strictly higher
priority ready task so the scheduler can switch at a safe boundary.
During a proven autonomous_wait with background_work_safe, approve a ready
prepare_components task for the next approved job's actual unlocked buildings.
Use bounded absolute stock goals, current recipes and protected primary costs;
never prepare a future locked design. The scheduler may temporarily run this
approved opportunistic work even below the waiting parent's priority, then
resume that exact parent. Native waits are opportunities to shorten later work;
choose useful preparation from the real next job, not a fixed drill example.
Use only documented capabilities and parameters. Current native research,
enabled recipes, resources, power and producer readiness govern design choice.
Never assume a later design is available because its prototype or an old game's
maximum tier exists. Bootstrap with available early designs, then upgrade after
their real prerequisites. Do not choose coordinates or arbitrary commands.
Prioritize observed threats, power and resource failures. Read adaptive_defense_risk.
In the quiet early stage its turret target is zero: advance production and
research without preemptive turret factories or whole-area turret coverage.
Do not turn an absent threat into mandatory early defense spending. The framework
also surveys proposed industrial sites before deferring their defense gate.
As enemies approach or damage appears, establish the risk-scaled defenses;
if needed recipes are locked, use the finite machine research bridge.
Preserve paid partial work and stable queue IDs. An ID permanently identifies one capability and exact
parameters, including after completion; use a new ID for a different target.
Reuse the SAME ID for retries of the SAME effect. Do not create new IDs just to
retry a fresh plan or escape an ambiguous existing-job error. For continue,
retry or pause, set target_task_id to the particular existing paused/blocked or
active queue ID. Repeat that node's exact parameters; capability may be null or
its exact capability. Other operations must use target_task_id=null.
Read the latest planner error before proposing a replacement decision.
Read native_capability_gates before planning factories. Research that unlocks a
factory recipe must run BEFORE that factory, using science already available.
For example, unlock logistic-science-pack and fast-inserter with existing red
science before logistic_science; never make either unlock depend on that factory.
Never retry unknown outcomes. If a job is running,
normally continue it. A locked or blocked task needs a concrete prerequisite or
repair, not repeated blind retries. Do not claim completion without native proof.
A waiting job's reason is its real executor condition, even after a time slice.
Judge that condition and native progress before continuing; a slice ending does
not resolve missing fuel, inputs or prerequisite research.
For combined coal/water/steam waits, compare drill_fuel/drill_powered, water,
steam and energized_engines to locate the missing condition before choosing
a repair. Positive water and steam with an unfueled drill calls for coal-source
startup or its documented repair, rather than a separate water-gathering plan.
Layout planning or search-budget failures are executor faults, not evidence of
insufficient mining capacity. Do not repeatedly expand a resource to treat them.
The user's goal is rapid scalable progress toward a rocket, with clean layouts,
both belt lanes, main-bus priorities, capped construction stock, full factory
radar coverage, timely defenses and eventual external mines/rail transport.
Use operation=plan to add/update a short queue; request for one immediate job;
continue for the existing job; retry only a known blocked outcome whose cause
is now resolved; pause when necessary; wait when no safe executable work exists.
Routine replanning must not pause a progressing job. A wait decision delays the
next planning opportunity; already approved executable queue work continues.
parameters_json must encode an object. Queue dependencies refer to queue IDs.
'''


def _codex_command():
    found=shutil.which('codex')
    if not found:raise RuntimeError('Codex CLI is not installed; sign in with codex login before starting')
    path=Path(found)
    if path.suffix.lower()in('.cmd','.bat'):
        # Run the executable directly: Windows shell metacharacters must never
        # interpret a model prompt, a file path, or snapshot data.
        content=path.read_text(encoding='utf-8-sig')
        match=re.search(r'^"([^"\r\n]+\.exe)"( -c cli_auth_credentials_store=file)? %\*\s*$',content,re.M|re.I)
        if not match or not Path(match[1]).is_file():raise RuntimeError('Unsupported Codex CLI wrapper; configure a native executable')
        return [match[1]]+(['-c','cli_auth_credentials_store=file']if match[2]else[])
    return [str(path)]


def _redact(value):
    if isinstance(value,dict):return {k:_redact(v)for k,v in value.items()
        if not re.search(r'password|secret|authorization|api.key|access.token|refresh.token',str(k),re.I)}
    if isinstance(value,list):return [_redact(v)for v in value]
    return value


def _parameters(raw):
    if not isinstance(raw,str)or len(raw)>12000:raise ValueError('Luna parameters must be bounded JSON text')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('Luna parameters must be an object')
    return value


def normalize_decision(value):
    if(not isinstance(value,dict)or value.get('operation')not in OPERATIONS
            or set(value)-{'operation','capability','parameters_json','queue','reason','target_task_id'}):raise ValueError('Invalid Luna decision envelope')
    operation=value['operation'];capability=value.get('capability');queue=value.get('queue',[])
    if capability is not None and(not isinstance(capability,str)or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}',capability)):
        raise ValueError('Invalid Luna capability')
    if operation=='request'and not capability:raise ValueError('A request needs a capability')
    target=value.get('target_task_id')
    if target is not None and(operation not in('continue','retry','pause')or not isinstance(target,str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',target)):raise ValueError('Invalid Luna target_task_id for job control')
    if not isinstance(queue,list)or len(queue)>16:raise ValueError('Luna queue is limited to sixteen tasks')
    nodes=[]
    for node in queue:
        if(not isinstance(node,dict)or set(node)!={'id','capability','parameters_json','priority','depends_on','reason'}
                or not isinstance(node['id'],str)or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',node['id'])
                or not isinstance(node['capability'],str)or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}',node['capability'])
                or type(node['priority'])is not int or not 0<=node['priority']<=1000
                or not isinstance(node['depends_on'],list)or len(node['depends_on'])>16
                or any(not isinstance(q,str)or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',q)for q in node['depends_on'])
                or not isinstance(node['reason'],str)or len(node['reason'])>2000):raise ValueError('Invalid Luna queue task')
        nodes.append({k:v for k,v in node.items()if k!='parameters_json'}|{'parameters':_parameters(node['parameters_json'])})
    reason=value.get('reason','')
    if not isinstance(reason,str)or len(reason)>2000:raise ValueError('Invalid Luna reason')
    result={'operation':operation,'capability':capability,'parameters':_parameters(value.get('parameters_json','{}')),
            'queue':nodes,'reason':reason}
    if 'target_task_id'in value:result['target_task_id']=target
    return result


class LunaDecisionClient:
    def __init__(self,model='gpt-6-luna',reasoning_effort='medium',timeout_seconds=120):
        if model!='gpt-6-luna' or reasoning_effort!='medium':raise ValueError('This runner is pinned to GPT-6 Luna medium')
        if type(timeout_seconds)not in(int,float)or not 1<=timeout_seconds<=300:raise ValueError('Bounded Luna timeout required')
        self.model,self.reasoning_effort,self.timeout_seconds=model,reasoning_effort,timeout_seconds
        self.last_usage=None;self.last_session_id=None
        self._lock=threading.Lock();self._process=None;self._cancelled=False

    def cancel(self):
        with self._lock:
            self._cancelled=True
            if self._process is not None and self._process.poll()is None:self._process.terminate()

    def _invoke(self,command,**kwargs):
        payload=kwargs.pop('input');timeout=kwargs.pop('timeout')
        with self._lock:
            if self._cancelled:raise RuntimeError('Luna client stopped; no decision was authorized')
            process=subprocess.Popen(command,stdin=subprocess.PIPE,**kwargs);self._process=process
        try:
            stdout,stderr=process.communicate(payload,timeout=timeout)
            return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
        except subprocess.TimeoutExpired:
            process.kill();process.communicate();raise
        finally:
            with self._lock:self._process=None

    def decide(self,snapshot):
        if not isinstance(snapshot,dict):raise ValueError('A structured current-world snapshot is required')
        raw=json.dumps(_redact(snapshot),ensure_ascii=False,separators=(',',':'))
        if len(raw)>180000:raise ValueError('Planner snapshot too large; compact entities and logs first')
        with tempfile.TemporaryDirectory(prefix='factorio-luna-')as directory:
            root=Path(directory);schema=root/'decision-schema.json';output=root/'decision.json'
            schema.write_text(json.dumps(DECISION_SCHEMA),encoding='utf-8')
            command=_codex_command()+['exec','--model',self.model,'-c','model_reasoning_effort="medium"',
                '--sandbox','read-only','--ephemeral','--skip-git-repo-check','-C',str(root),
                '--json','--output-schema',str(schema),'--output-last-message',str(output),'-']
            env=dict(os.environ)
            for key in('OPENAI_API_KEY','CODEX_API_KEY'):env.pop(key,None)
            if 'cli_auth_credentials_store=file'in command:env['CODEX_HOME']=str(Path.home()/'.codex')
            try:
                result=self._invoke(command,input=(PROMPT+'\nCURRENT SNAPSHOT:\n'+raw).encode('utf-8'),
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=self.timeout_seconds,cwd=root,env=env,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except subprocess.TimeoutExpired as exc:raise RuntimeError('Luna decision timed out; no game action was authorized')from exc
            if result.returncode:
                detail=result.stderr.decode('utf-8',errors='replace')[-1400:]
                raise RuntimeError('Codex Luna failed (no model fallback): '+detail)
            for line in result.stdout.decode('utf-8',errors='replace').splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                item=event.get('item',{})
                if item.get('type')in('command_execution','file_change','mcp_tool_call','web_search','tool_call'):
                    raise RuntimeError('Luna planner attempted a tool; its decision was not authorized')
                if event.get('type')=='thread.started':self.last_session_id=event.get('thread_id')
                if event.get('type')=='turn.completed':self.last_usage=event.get('usage')
            if not output.exists():raise RuntimeError('Luna did not produce a structured decision')
            return normalize_decision(json.loads(output.read_text(encoding='utf-8-sig')))
