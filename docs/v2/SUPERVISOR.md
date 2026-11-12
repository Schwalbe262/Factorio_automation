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
{"operation":"plan","capability":null,"parameters":{},"reason":"bootstrap",
 "queue":[{"id":"starter","capability":"starter","parameters":{},
 "priority":100,"depends_on":[],"reason":"normally produce starting materials"}]}
```

Operations are `plan`, `request`, `continue`, `retry`, `pause`, `wait`. Queue nodes
have stable IDs, documented capability/parameters, integer priority and dependency
IDs. `work_queue.json` is bound to one world and persists pending/ready/running/
blocked/completed nodes. Job keys derive from world, node ID, capability and
parameters. Node task identity cannot change; use a new ID for a new effect.
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
checkpoint failures and exact paused-job resume. They make no game changes and
do not establish an unattended one-hour factory or rocket success claim.
