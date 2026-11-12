# Event-driven Luna supervisor

```text
python -B -m factorio_agent.supervisor --runtime ABS --decision-interval 60 --slice-seconds 25
```

`--max-seconds N` ends the session through safe stop; `--max-decision-interval` overrides the
maximum Luna interval. `--bridge inprocess` (default) keeps one game session in the supervisor
process; `--bridge cli` starts a child per command. Lifecycle owns the game/client and game pause. The supervisor never sets research, grants goods,
chooses coordinates or sends raw RCON.

## Cycle

1. Snapshot: `observe`, `status`, `capabilities`, `actor-status`; world and rollback guards;
   queue refresh. State records `active_job_keys` (priority order) and `active_job_key` (first).
2. Resume every job the previous successful safe stop paused (`pause_receipts`).
3. Autopilot: when fewer than 2 nodes are pending or ready, `progression.autopilot_nodes(jobs,
   queue_nodes, observation, capabilities, limit=3)` adds up to 3 `auto_*` nodes in Luna's format
   through the same merge validation (rejections are logged, not backed off; no module = skip).
4. Accept a finished Luna decision, then maybe start the next call (below).
5. Dispatch: while fewer than `MAX_ACTIVE_JOBS` (4) are active, submit ready nodes, highest
   priority first (`request --priority`). A node whose capability is already active stays ready.
   A retryable admission refusal (capability active, 4 active, stale observation) keeps the node
   ready; any other refusal blocks that node only.
6. One `run --all --seconds <slice>` interleaves every active job per action. Stop handling
   (`stop_requested`/`on_stop`) is unchanged.

## When Luna is called

One call is in flight at a time, asynchronously; dispatch never waits for Luna.

- first cycle, or forced;
- a job became succeeded/blocked/failed, or a blocked/failed reason changed;
- the queue became low (fewer than 2 pending/ready nodes), once per low episode;
- `max_decision_interval` elapsed: default `max(decision_interval, 300)`, explicit values as given.

Never sooner than `minimum_decision_interval` (20 s) after the previous call, never during error
backoff (10 s doubling, at most 300 s). `wait` suppresses the low-queue and interval triggers for
`decision_interval` seconds; terminal events still trigger, and ready nodes and active jobs keep
running. Errors never select a fallback model.

## Decisions

```json
{"operation":"plan","capability":null,"parameters":{},"target_task_id":null,"reason":"bootstrap",
 "queue":[{"id":"starter","capability":"starter","parameters":{},
 "priority":100,"depends_on":[],"reason":"normally produce starting materials"}]}
```

- `plan` merges queue nodes. IDs are immutable identities (capability + exact parameters); job
  keys derive from world, ID, capability and parameters. Cycles, unknown references, raw
  coordinates/commands and invalid requests reject the whole merge. Started nodes keep their
  dependency set. A NEW ID whose capability and parameters equal an existing pending, ready,
  running or completed node is skipped and dependencies on it are redirected; blocked nodes do
  not absorb duplicates. This keeps Luna and autopilot from queuing the same work twice.
- `request` adds one `now_*` node; admission happens at dispatch.
- `continue` without a target is a no-op; with `target_task_id` it resumes that exact paused job.
- `retry` / `pause` act on the exact job from the decision snapshot (`target_task_id`, or one
  unambiguous match). A target that finished meanwhile is a logged stale response; nothing is
  retargeted to a successor. Unknown action outcomes are never retried.
- `wait` delays the next Luna call only.

The snapshot holds native unlocks, research options, power, bounded inventory/entity counts,
`max_active_jobs`, `active_jobs`, up to 12 jobs (all active ones first) with bounded blocker
summaries, the open queue, the last 16 completed nodes and the latest planner error. Coordinates,
commands, paths and credentials are stripped. World/actor/tick are re-checked before a decision
is applied. Older queue files may carry `scheduler_pause` fields; they are dropped on load.

## Stop protocol

The supervisor holds `controller.lock`; state is `logs/controller-state.json`, events
`logs/controller-events.jsonl`. Lifecycle writes `supervisor-stop.json`
(`request_id`, `world_id`, `requested_at`, `source`). It is polled while idle and during a slice;
a running slice gets the exclusively created `stop.json` latch (`worker_stop_owned`,
`worker_stop_sha256`) plus `stop-worker`, and finishes its current action. Then every active job
is paused (one `pause` per key) and a checkpoint is requested. Only after both succeed does state
record `status='safe_stopped', state='stopped', saved=true, stop_request_id=<id>`,
`pause_receipts` (all) and `pause_receipt` (first). A failed checkpoint or foreign world yields
`saved=false`. Late model responses after stop are discarded. Another operator's `stop.json` is
never claimed or overwritten. Tests use a fake CLI and clock and make no game changes.
