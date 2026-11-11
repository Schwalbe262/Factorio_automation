# Gameplay evaluator API

Model: GPT-6 Luna, reasoning medium. Use a fresh context. Do not read v1 journals,
modify source/settings, call raw RCON or execute arbitrary game Lua. Only choose
documented high-level capabilities through the CLI. The parent may prepare the
runtime/client but must not choose gameplay actions during a scored attempt.

PowerShell setup for every shell invocation:

```powershell
$env:PYTHONPATH='src'
```

The command format is `python -B -m factorio_agent --runtime <directory> <command>`.

- `observe`: current world ID/tick, concise production/inventory, technology,
  entities, available capabilities and job receipts.
- `capabilities`: implemented contracts. Missing skills are genuinely unavailable.
- `request --key <unique-key> --world-id <id> --observed-tick <tick> --capability <name>`:
  submit one goal. Use a recent observation. A key always identifies the same
  effect; retrying it cannot create another job. One active job per world.
- `run --key <key> --seconds 25`: execute a bounded slice of that job. Repeat
  until succeeded/blocked/failed. A waiting slice is still a live unfinished job.
- `worker --key <key> --max-seconds 900`: keep executing that chosen job without
  another model decision each slice. Poll the exact shell `session_id` until it
  completes. A tool's 10-second yield is NOT completion; never start another
  worker while the first process still owns the writer lock. Worker prints a
  concise heartbeat about every 50 seconds and stops on a real terminal status.
- `pause --key <key>` / `resume --key <key>`: suspend an unfinished goal while
  another goal runs, preserving partial construction. Only one unpaused goal.
- `reconcile --key <key>`: explicitly reobserve an uncertain action outcome and
  replan from actual state; never replay the old mutation. World rollback needs
  separate reconciliation and is not accepted by this command.
- `watch` / `watch --hide`: show/hide the existing automation client.
- `status`: persisted job receipts. Historical success needs live reinspection
  before claiming the factory is still producing after damage or rollback.

Current capabilities:

| Capability | Parameters | Requires |
| --- | --- | --- |
| starter | none | none |
| power | none | starter |
| starter_capacity | `--resource iron-ore` (or copper-ore/coal/stone), `--cells 1..8` absolute productive cell target | starter |
| starter_research | `--technology automation` or electric-mining-drill | power |
| regularize_power_layout | none | starter; existing unfinished power plan |

Use the typed parameter flags above to avoid Windows shell JSON quoting issues.
Starter capacity funds initial construction using extra drill-fed starter cells;
it still requires starter fuel upkeep. This is not the final scalable mining
system. Starter research uses finite normally crafted science budgets to unlock
the first machines; it cannot handcraft its way through later research.
Broader automatic production, defense, rail and rocket jobs remain unimplemented.
Do not claim a rocket-capable system.

The layout migration recovers obsolete paid belts through normal mining and
installs a prevalidated regular corridor. It refuses an operating supply that
would need a parallel cutover. A layout receipt proves geometry only. The power
job must still seed fuel and verify actual flow. Idle steam/energized engines
do not prove measured electric generation; verify it under a real normal load.

When blocked, report the exact job reason, measured state, and missing capability
or interface information. Do not fix code or invent successful game actions.
When reporting success, state the accepted postcondition and remaining scope.
