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
| mining_outpost | `--resource iron-ore` (or copper-ore/coal/stone), `--rate-per-minute 1..960` absolute nominal target | power and electric-mining-drill |
| smelting_district | none; initial district has iron60/copper30 nominal inputs | power, bootstrap_logistics, mining_outpost; one ready bank each iron60/copper30/coal30 |
| smelting_capacity | `--resource iron-ore` or copper-ore, `--rate-per-minute` finite (0,225] absolute nominal target | paired_smelting; connected ore supply must cover the target |
| paired_smelting | none | smelting_district; migrate to independent upper/lower producer rows |
| paired_mining | none | paired_smelting; normally recover legacy ore cells, directly feed both collector lanes |
| automation_science | `--rate-per-minute` finite (0,30] requested red-science rate | paired_smelting; normally connect raw plate belts, machines and labs |
| logistic_science | `--rate-per-minute` finite (0,30] requested green-science rate | automation_science; native green-science and fast-inserter unlocks |
| steam_expansion | none | electric_coal; ordinary paired coal bank and another boiler/engine pair |
| defense_perimeter | none | starter; native gun-turret research and recipes |
| main_bus_factory | none | paired_smelting, logistic_science, defense_perimeter; migrate to shared bus and compact green |
| dedicated_materials | `--resource coal` or stone | paired_mining; independent opposing drill pair and identity-bound material outlets |
| green_bus_reroute | none | main_bus_factory; move only green raw branches off resources, preserve production core |
| steam_coal_reroute | none | steam_expansion; prebuild alternative, verify loaded engines, recover obsolete owned transport |
| legacy_coal_cleanup | none | steam_expansion and dedicated_materials; isolated burner/cache recovery after replacement supply and headroom proof |
| research | `--technology` native catalog name | automation_science; native prerequisites and continuous supported science supply |

Use the typed parameter flags above to avoid Windows shell JSON quoting issues.
Starter capacity funds initial construction using extra drill-fed starter cells;
it still requires starter fuel upkeep. This is not the final scalable mining
system. Starter research uses finite normally crafted science budgets to unlock
the first machines; it cannot handcraft its way through later research.
Permanent green science, strategic armed posts and shared-bus migration are
implemented. Construction mall, automatic ammunition production, repair, oil,
rail and rocket jobs remain unimplemented.
Do not claim a rocket-capable system.

New construction checks resource tiles and registered mining growth space before
clearing or collecting materials. Ordinary factories, buses and long routes
avoid both. Only explicitly scoped mining cores and a verified minimal surface
exit may overlap their matching ore; a blanket `resource_cell` flag is insufficient.
An existing asset is retained by exact native identity. Losing it does not
authorize rebuilding it on ore. Migration jobs prebuild clear alternatives,
pay for the cutover, then prove real supply before recovering old transport.
See [resource policy](RESOURCE_POLICY.md). Dedicated coal's two outlets share
one 60/min nominal source; a splitter does not double production.

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
Paired smelting replaces output redistribution with independent furnace rows
feeding opposite sides of one collector. Supply ports stay iron(-9.5,-29.5) and
copper(-9.5,-17.5), east/both lanes. Paired mining similarly uses opposing drill
rows, retaining connected ore outputs. Extend only within native resource-backed
reservations; additional ore capacity uses another paired block when the mineral
edge prevents a full new column. Both-lane inventory is not a throughput proof.
`observe.belt_lane_observation` exposes native left/right contents and contracts;
left/right is relative to travel direction. Stock is not measured throughput.
See [the lane rules and tested layout](BELT_LANES.md).

Permanent red science reserves a six-assembler row plus one gear assembler,
two iron input inserters, separate copper/gear input lanes and eight belt-fed
labs. A 30/min request has nominal36/min machine capacity; it does not itself
prove five minutes of consumption. `research` selects native technology only;
it never crafts or inserts packs. `observe.available_research` reports native
prerequisite-ready costs; unsupported science tiers block explicitly.

Green science uses seven science assemblers plus local gear, cable, circuit,
belt and inserter machines. The requested30/minute has35/minute nominal machine
capacity. It consumes normally belt-fed plates and joins the other lane of the
existing lab belt upstream of all eight pickups. Commissioning requires actual
products, powered machines, native routes and evidence that every lab received
green; it does not establish a sustained rate. Research supports red and green
only after their real production proofs are healthy.

`main_bus_factory` replaces separate raw delivery corridors with one shared
northbound iron trunk and one copper trunk, with a second line reserved for
each. Four native-priority splitters supply red and compact green. It preserves
producer/lab ports and recovers only saved owned assets. The old green site is
retired only while fresh replacement AND source-smelting proofs remain healthy.
See [bus contracts](MAIN_BUS.md) and [compact layout](COMPACT_GREEN.md).

`defense_perimeter` normally builds eight strategic gun posts and loads ten
full normal magazines each. During later jobs, upkeep replenishes live known
posts below five magazines to ten. There is no idle background worker, automatic
ammo factory, or general repair system. Missing posts require another perimeter
job; an unexpected replacement identity blocks. See [defense evidence](DEFENSE.md).

`steam_expansion` preserves the original plant, adds a dedicated paired coal
bank and another boiler/two-engine cell, then samples actual native generation
for thirty game seconds. Added1800kW is nameplate capacity; loaded positive
generation alone does not prove delivery of the full rating.

`retry --key KEY` reopens a known blocked outcome after a fix and always replans
from fresh observation. It preserves the request, action history and consumed
execution budget. Recorded terminal-blocked intervals are excluded from active
budget; original creation time and full elapsed time remain visible. Unknown
action outcomes require `reconcile`; retries cannot bypass
world rollback or replay an old pending action. `resume` is for intentionally
paused jobs, including the same science job after supply construction.

`observe` reports time-window production rates only after two timed samples;
lifetime counters are never labeled throughput. Saved-project construction
budgets are separate from the next immediate component deficit and exclude
unplanned future factory stages. `status --key <key>` gives one full receipt.
`observe.factory_constraints` separates nominal supply/demand deficits, live
power faults, electrical peak demand and generation nameplates. These are
diagnostics, not a measured sustained factory rate.

Construction upkeep reserves four empty actor inventory slots. At low space it
normally crafts/builds dedicated recovery chests on surveyed clear land outside
factory and bus reservations, then deposits surplus plain normal items with
exact chest identity and source-slot conservation. Full-inventory crafting
accounts for ingredient stacks that must be consumed to fit its chest outputs.
Science packs, ammunition and metadata-bearing stacks are excluded. The regular
construction collector can later retrieve eligible stock from these chests.
Sufficient observed headroom skips the extra native inventory query. Uncertain
deposits retain durable intent and require reconciliation, exactly like an
uncertain construction or recovery action.

The layout migration recovers obsolete paid belts through normal mining and
installs a prevalidated regular corridor. It refuses an operating supply that
would need a parallel cutover. A layout receipt proves geometry only. The power
job must still seed fuel and verify actual flow. Idle steam/energized engines
do not prove measured electric generation; verify it under a real normal load.

When blocked, report the exact job reason, measured state, and missing capability
or interface information. Do not fix code or invent successful game actions.
When reporting success, state the accepted postcondition and remaining scope.
