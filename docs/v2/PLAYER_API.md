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
| bootstrap_logistics | none | power and naturally unlocked Automation |
| electric_coal | none | power and naturally unlocked electric-mining-drill |
| mining_outpost | `--resource iron-ore` (or copper-ore/coal/stone), `--rate-per-minute 1..240` absolute nominal target | power and electric-mining-drill |
| smelting_district | none; initial district has iron60/copper30 nominal inputs | power, bootstrap_logistics, mining_outpost; one ready bank each iron60/copper30/coal30 |
| smelting_capacity | `--resource iron-ore` or copper-ore, `--rate-per-minute` finite (0,100] absolute nominal target | smelting_district; connected ore supply must cover the target |
| balance_smelter_lanes | none | smelting_district; fixed east boundary outputs |

Use the typed parameter flags above to avoid Windows shell JSON quoting issues.
Starter capacity funds initial construction using extra drill-fed starter cells;
it still requires starter fuel upkeep. This is not the final scalable mining
system. Starter research uses finite normally crafted science budgets to unlock
the first machines; it cannot handcraft its way through later research.
Broader automatic production, defense, rail and rocket jobs remain unimplemented.
Do not claim a rocket-capable system.

Bootstrap logistics builds a small reserved16x16 PCB cell with two assemblers,
four real inserters and finite iron/copper chest inputs. Gear and red-science
crafting occur in the actual machines, never through player handcrafts. The
normally produced packs fund the native logistics research, unlocking initial
underground belts/splitters. This is finite bootstrap funding, not automated raw
supply or a sustained30SPM proof. Native preflight checks the entire cell and
its straight-hop power connection before any construction.

Electric coal preserves the existing regular trunk. It crafts the replacement
normally, prebuilds paid poles, waits for an ordinary boiler/belt reserve, then
recovers the exact owned burner drill and installs an electric drill whose
actual prototype drop is prechecked against the retained belt. Proof requires
electric power and native coal reappearing at the cleared direct output tile.
The obsolete self-fuel inserter is recovered after output is verified. This is
one source upgrade; full-load fuel capacity and ore/smelter logistics need their
own throughput evidence.

Mining outposts add aligned electric-drill rows and shared collector belts with
fixed output ports. The framework derives drill counts from actual prototype
rates and surveys pure resource coverage, complete placements and power before
construction. Existing productive banks stay in place when capacity is added.
The receipt proves powered extraction/native item output. Nominal capacity is
not a measured downstream smelting or science rate; those require further jobs.
Expansion first tries the upstream row while preserving its output. If blocked,
reserve an additional aligned bank and connect it to an identity-bound same-item
smelter trunk through a declared side-load. Its native output and consumer
connection must both be valid. Natural obstacles on a preflighted corridor are
cleared only by ordinary mining of approved neutral minable trees/rocks.
Smelting reserves a32x32 expansion footprint before construction. Parameterized
furnace rows have separate ore/coal side-load lanes, ordinary inserters and fixed
plate outputs. A separate coal bank supplies one real balanced splitter; the
existing power feed remains operating. Full route/placement preflight precedes
payment. Perpendicular foreign belt crossings use normal underground endpoints
with span, role, orientation, pairing and unintended-merge checks. Foreign
surface belts are preserved. Native proof requires every furnace to produce,
plate output, actual underground partners and distinct ore/coal feed lanes.
Its initial inputs are rated60/30; sustained output needs downstream consumption
and a measured window. The actor never inserts ore/fuel into these new furnaces.
Capacity grows inside six reserved furnace slots per row. The output stays at
the block's east boundary for every count. Old movable ports can migrate only
before a consumer is registered; preserve connected ports thereafter.
Lane balancing preserves the collection rows and adds one splitter/opposite
side-load module per product. New supply ports are iron(-9.5,-29.5) and
copper(-9.5,-17.5), both facing east with the same plate declared on both lanes.
`observe.belt_lane_observation` exposes native left/right contents and contracts;
left/right is relative to travel direction. Stock is not measured throughput.
See [the lane rules and tested layout](BELT_LANES.md).

`observe` reports time-window production rates only after two timed samples;
lifetime counters are never labeled throughput. Saved-project construction
budgets are separate from the next immediate component deficit and exclude
unplanned future factory stages. `status --key <key>` gives one full receipt.

The layout migration recovers obsolete paid belts through normal mining and
installs a prevalidated regular corridor. It refuses an operating supply that
would need a parallel cutover. A layout receipt proves geometry only. The power
job must still seed fuel and verify actual flow. Idle steam/energized engines
do not prove measured electric generation; verify it under a real normal load.

When blocked, report the exact job reason, measured state, and missing capability
or interface information. Do not fix code or invent successful game actions.
When reporting success, state the accepted postcondition and remaining scope.
