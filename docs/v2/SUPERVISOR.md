# Periodic Luna supervisor

Run with an absolute existing runtime:

```text
python -B -m factorio_agent.supervisor --runtime ABS --decision-interval 60 --slice-seconds 25
```

`--max-seconds 3600` optionally ends the session through the same safe-stop path.
Lifecycle owns the visible game/client process and game pause state. The
supervisor never sets research, grants goods, chooses coordinates or sends RCON.
It uses the current repository's absolute source root and a new Python process
for each typed CLI operation/slice. Sol can patch the development framework;
the next fresh CLI process imports those changes. Slice duration is checked at
ordinary action boundaries, so an in-flight native action can exceed25 seconds.

`LunaDecisionClient(model='gpt-6-luna', reasoning_effort='medium',
timeout_seconds=120)` is supplied by `luna_client.py`. The supervisor calls
`decide(snapshot)` asynchronously, normally every60 seconds, with a ten-second
minimum event interval. Already approved queue work runs while Luna is thinking.
Only newly terminal jobs or changed blocked/failed reasons trigger an early
decision; ordinary running/waiting transitions do not. `wait` delays idle
planning only: active jobs and ready approved nodes continue. `pause` applies
only to the selected job, so independent ready nodes may still execute.
There is one in-flight planner call and no fallback model. Errors are logged
with bounded exponential backoff. Usage/session metadata from the client is
recorded when available.

The compact snapshot comes from typed `observe`, `status`, `capabilities` and
`actor-status`. It contains current native researched technologies and enabled
recipes, research options, power/constraints, inventory/entity counts, recent job
states and the pending queue. A bounded semantic blocker summary includes
material item/have/need, prerequisite identifiers and the last executor failure.
It strips coordinates, raw commands, paths and credential fields or strings.
The most recent 16 completed queue identities keep their immutable ID,
capability, parameters and accepted dependencies visible; the latest planner error is included until
a decision is accepted. Full entity lists, geometry, old world histories,
credentials and JSONL journals are not given to Luna. World and the current
native pinned owner's actor, job status and capability schema are checked again
after a decision before accepting work. A job-control decision retains the
original prompt's exact job key and identity. If that job completed while Luna
was thinking, `continue` is logged as a harmless stale response; it cannot pause,
retry or resume the successor. A fresh active job also defers a stale new request.
Unknown actions, rollback and actor/world changes are not automatically
reconciled or retargeted.

Decisions use:

```json
{"operation":"plan","capability":null,"parameters":{},"target_task_id":null,"reason":"bootstrap",
 "queue":[{"id":"starter","capability":"starter","parameters":{},
 "priority":100,"depends_on":[],"reason":"normally produce starting materials"}]}
```

Operations are `plan`, `request`, `continue`, `retry`, `pause`, `wait`. Queue nodes
have stable IDs, documented capability/parameters, integer priority and dependency
IDs. `work_queue.json` is bound to one world and persists pending/ready/running/
blocked/completed nodes. Job keys derive from world, node ID, capability and
parameters. Node task identity cannot change; use a new ID for a new effect.
For `continue`, `retry` or `pause`, `target_task_id` can select one existing stable
queue ID when multiple paused or blocked jobs match. Repeat that task's exact
parameters; capability may be null or the exact accepted capability. The selector
resolves the current-world queue's stable job key and binds only the job present
in the original decision snapshot. A missing/outdated prompt target is a harmless
stale response; unknown IDs and mismatched capability/parameters are rejected.
It cannot redirect control to a successor. Other operations use null. Older
responses without this field retain unambiguous selection behavior. Luna's wire
schema requires the nullable field; normalization still accepts old missing data.
Retries keep the same queue ID and paid job; creating another ID for the same
effect is not a retry. Full output buffers with zero measured production do not
establish shortage. Existing stocks and native statuses come first, and a funded
science batch should be finished or repaired before another instance is started.
Dependency order and repeated dependency IDs have no meaning and are normalized.
For a running or completed node the actual dependency set remains immutable,
even when a newly proposed prerequisite has already completed. Such additions
or removals reject the entire merge and name the node and changed edges in the
bounded planner error. Unstarted nodes may still update their dependencies.
Cycles, unknown references, raw coordinates/commands and invalid Request schemas
are rejected before queue acceptance. Request validation runs in a fresh private
subprocess mode, without accessing the game. Native capability prerequisites
must have succeeded before a node becomes ready.

After completion the next ready approved node can dispatch without waiting for
Luna. Native CLI rejection blocks a node instead of blindly submitting it again.
Retry is an explicit decision for a known blocked outcome; a pending/uncertain
effect needs reviewed reconciliation outside this decision schema. Only one
active job is accepted per world. A lifecycle START resumes the exact job paused
by the preceding successful safe stop before considering unrelated new work.

At a completed slice boundary, an approved **strictly higher** ready-node
priority can preempt an active queued job that is `waiting` or `pending`.
The scheduler assigns no capability-specific priorities and no default priority
to an unqueued active job. Equal/lower priorities or unmet dependencies do not
preempt. Native world, actor, job identity/status, rollback, uncertain action and
operator stop state are checked again before the exact CLI pause. Paid partial
plans and their original job keys remain in place.

The node's durable `scheduler_pause` stores its original world/job/actor,
higher approved task, pause intent and completed CLI pause binding. Only a
confirmed scheduler-owned pause can automatically resume, ordered against
remaining ready work by approved priority and stable queue ordering. Its
`scheduler_paused` state preserves started dependency identity. A higher task
that is still waiting does not cause priority oscillation. Completed, blocked
or otherwise terminal higher work leaves the original pause eligible; it does
not reset or retry that higher job.

Ownership also checks actual control events in `job-events.jsonl`, incrementally
from the accepted pause cursor, without giving journal contents to Luna. An
external repeated pause is detected even if the job's paused fields stay the
same. Explicit model/user control, a changed binding or an uncertain action
releases automatic-resume ownership. Truncated or unavailable audit history
fails closed. A restart preserves a confirmed marker; a crash leaving only an
unconfirmed pause intent never adopts a possibly user-created pause. Stop
between pause and dispatch retains the confirmed marker and submits no new
job. Lifecycle's separate exact safe-stop resume remains unchanged.

The supervisor holds the OS lock `controller.lock`. State is
`logs/controller-state.json`; bounded events are `logs/controller-events.jsonl`.
State records PID, exact Windows process creation FILETIME in both `creation_time`
and `native_creation_time`, controller ID, world, active job, child, decision and
planner usage. `process_creation_time` is a human-readable epoch equivalent.
PID metadata alone is not ownership authority.

Lifecycle requests stop through `supervisor-stop.json`:

```json
{"request_id":"unique-id","world_id":"installed-world-id",
 "requested_at":0,"source":"lifecycle"}
```

The marker is polled every quarter second while idle/model waiting and by the CLI
bridge during a run. No new request, retry or run starts after it is seen. An
active slice receives the standard `stop.json` latch and finishes its current
ordinary action. The supervisor waits for the child to exit, pauses the unfinished
job through CLI and requests a native checkpoint through CLI. It does not kill
the gameplay child or discard durable action intent.

Only after pause/checkpoint success does state acknowledge
`status='safe_stopped', state='stopped', saved=true, stop_request_id=<exact id>`.
The ACK is written before controller lock release/process exit. Lifecycle waits
for both that exact ACK and the process to exit, then owns game pause. A failed
checkpoint or mismatched world yields `saved=false` and no successful ACK.
Pending model work is cancelled when the supplied client supports cancellation;
late model responses cannot authorize actions after stopping.

For a running slice, the supervisor creates `stop.json` using exclusive creation,
tagged with controller/request identity, then calls standard `stop-worker`.
`worker_stop_owned` is true only for that exclusive creation, and
`worker_stop_sha256` hashes the exact raw bytes. An existing operator marker is
never overwritten or claimed. Lifecycle may clear a prior owned latch only after
matching the saved ACK/world/raw hash. The supervisor does not silently clear
stops on startup.

Tests use fake CLI/client/processes, temporary runtimes and deterministic clocks.
They exercise DAG/prerequisites, queue identity, continuous dispatch through wait
and per-job pause, stale target isolation, terminal-only event scheduling,
bounded blocker/identity summaries, rejection,
actor/world binding, fresh-process argv, stop during execution/planning, ownership,
checkpoint failures and exact paused-job resume. Sixteen additional scheduler
cases cover priority/dependency ordering, preserved paid partial progress,
unknowns, stop/actor races, restart, explicit user/model pause and no oscillation.
They make no game changes and
do not establish an unattended one-hour factory or rocket success claim.
