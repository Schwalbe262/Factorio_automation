# Supervisor: framework sequencing, model as exception handler

```text
python -B -m factorio_agent.supervisor --runtime ABS --planner luna|none --decision-interval 60 --slice-seconds 25
```

`--max-seconds N` ends through safe stop. `--bridge inprocess` (default) keeps one game session in
the process; `--bridge cli` starts a child per command. Lifecycle passes `controller.planner`,
`decision_interval_seconds` and `execution_slice_seconds` from `automation_config.json`. The
supervisor never sets research, grants goods, chooses coordinates or sends raw RCON.

## Role split

The framework sequences routine work: `progression.autopilot_nodes` (roadmap steps and
demand-driven capacity), `_auto_retry` (a blocked job is retried after another job succeeds, at most
3 times; never unknown outcomes, rollbacks or deadlines) and dispatch of up to 4 concurrent jobs.
The model (`gpt-6-luna`, medium, Codex CLI, no tools) is consulted only for an open incident.

- `planner: none`: no model client, no decision thread, no Codex import. Incidents are still
  opened, closed and recorded (`handled_by: "none"`) so the run can be audited.
- `planner: luna`: one call per open incident not consulted within `incident_retry_seconds` (300),
  one incident per call, most severe first, at least `decision_interval` seconds between calls,
  never during error backoff. Calls run in a background thread; dispatch never waits.

## Cycle

1. Snapshot (`observe`, `status`, `capabilities`, `actor-status`), world/rollback guards, queue refresh.
2. Resume jobs paused by the last successful safe stop.
3. Autopilot when fewer than 2 nodes are pending/ready (`auto_*` ids, same merge validation).
4. Auto-retry; accept a finished model decision.
5. Update incidents; maybe start one consultation.
6. Dispatch ready nodes (priority order, one job per capability, at most 4 active), then one
   `run --all --seconds <slice>`.

## Incidents (`incidents.py`, pure)

| kind | opens when | resolved when |
| --- | --- | --- |
| `empty_queue` | no active job, no pending/ready node, roadmap unfinished, for `idle_grace_seconds` (60) | an active job or queued node exists |
| `blocked_job` | a job stays blocked for `blocked_grace_seconds` (60); auto-retry restarts the timer | the job leaves `blocked` |
| `no_progress` | jobs active, no success for `progress_minutes` (10), top two of the last 100 actions >= `repeat_share` (0.5) | a job succeeds |
| `planner_review` | every `review_minutes` (off by default) | a job succeeds |

Severity: `empty_queue` > `blocked_job` > `no_progress` > `planner_review`. An episode still open
after `outcome_minutes` (10) closes `expired`; a persisting condition opens a new episode. The id is
a stable hash of kind and subject (job key or `queue`). Facts are bounded and redacted.

## Remedy menu

Each incident carries its menu; anything else is rejected (`decision_rejected`, at most once per
incident per 5 minutes, no error backoff). The decision schema is unchanged:

- `retry` -> `operation=retry` on a listed blocked task (never for an unknown outcome);
- `pause` -> `operation=pause` on a listed active task (`no_progress`, `planner_review`);
- `add_nodes` -> `operation=plan` with 1-3 nodes of listed capabilities (`requires` satisfied,
  native recipes not locked), with their parameter contracts;
- `wait` -> `operation=wait` with a reason; it changes nothing (the retry window spaces calls).

`request` and `continue` are never on the menu. A decision for an incident that closed while the
model was thinking is a `stale_decision`. Retry/pause still go through `_control`: exact target
from the decision snapshot, never a successor, unknown outcomes never retried.

## Consultation input

`document()`: the incident (kind, subject, facts), the menu, and a factory status (researched count
and the 10 most recently seen technologies, 15 most common entities, per-item `shortfall`,
`produced_per_minute`, `consumed_per_minute`, `net_per_minute`, open jobs one line each, next 5
roadmap steps with what each waits on). Hard cap 24,000 characters (~6,000 tokens) with
`...<truncated>` markers; a typical document is about 3,000 characters plus a 1,350-character prompt.

## Logs

Controller events: `supervisor_started` (planner), `incident_opened`, `incident_decision` (remedy,
accepted, latency, input/cached/output tokens), `decision_rejected`, `incident_outcome` (`resolved`
or `expired`, seconds, remedy). `logs/incidents.jsonl` stores each document sent (or that would be
sent with `none`), once per incident per retry window, capped at 5 MB. State shows `planner` and
`open_incidents`. `tools/report_run.py` summarises them; `tools/luna_incident_replay.py RUNTIME
[--limit N] [--dry-run|--send]` replays recorded documents (dry run by default).

## Stop protocol

The supervisor holds `controller.lock`; state is `logs/controller-state.json`. Lifecycle writes
`supervisor-stop.json`; a running slice gets the exclusively created `stop.json` latch plus
`stop-worker` and finishes its action. Every active job is paused, then a checkpoint is requested;
only after both succeed does state record `safe_stopped`, `saved=true` and `pause_receipts`.
Late model responses after stop are discarded; another operator's `stop.json` is never claimed.
