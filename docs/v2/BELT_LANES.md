# Belt lanes are separate transport channels

`left` and `right` mean relative to the belt's travel direction, never screen
left/right. A straight eastbound belt has its left lane north and right lane
south. The runtime reads `defines.transport_line.left_line/right_line`.

## Inserters, turns and side-loading

- A default inserter dropping across a belt places on the far lane. Our furnace
  output inserters stand north of eastbound belts, so they fill the south/right
  lane. Rotating every inserter in the same way does not distribute the output.
- In-line drops have a left/right choice in2.1 through mirroring. Treat that
  separately from an across-belt far-side drop; do not use custom pickup/drop
  vectors unavailable to ordinary game entities.
- Pickup can reach both lanes, with a preference depending on approach and
  mirroring. Recipe availability, swing time and lane access all affect delivery.
- A genuine side-load onto a straight belt merges onto the receiving side.
  Without an upstream straight stub the belt may become a corner instead.
  Our ore/coal feed explicitly keeps that stub and proves native lane contents.
- Generic underground crossings require straight approach and exit at both
  mouths. Side-loading a mouth can gate a lane and needs its own declared
  template. Native endpoint pairing alone does not prove both lanes pass.

These default rules are documented by the [official inserter wiki](https://wiki.factorio.com/Inserters),
the [2.1 developer post](https://factorio.com/blog/post/fff-442), and the
[runtime entity interface](https://lua-api.factorio.com/latest/classes/LuaEntity.html).
The last link tracks latest; actual behavior is checked against installed2.1.20.

## A splitter is not by itself a lane distributor

A splitter divides flow between output **belts** while preserving lane identity.
Feed it one lane and its two outputs can still have only that lane populated.
See [belt transport mechanics](https://wiki.factorio.com/Belt_transport_system).

The earlier EXP-0008 output distributor populated both lanes but did not add
producer capacity. The user correctly rejected this as the production fix.
`balance_smelter_lanes` and its executor have been removed. Its paid hardware
was ordinarily recovered; historical receipts remain as historical evidence.
The pure optional `lane_balancer` adapter remains for redistribution needs.

## Independent producers on both sides

`paired_smelting` installs upper/lower furnace rows around one shared belt.
Upper inserters drop onto its south/right lane; lower inserters drop onto its
north/left lane. Real input splitters divide ore/coal before the two production
rows. There are no output splitters. Consumer ports remain unchanged.

| Product | Independent furnaces left/right | Nominal capacity left/right, per min | Shared collector | Consumer port |
|---|---|---|---|---|
| Iron plate | 5 / 5 | 93.75 / 93.75 | (-17.5,-29.5), east | (-9.5,-29.5), east |
| Copper plate | 3 / 3 | 56.25 / 56.25 | (-17.5,-13.5), east | (-9.5,-17.5), east |

`paired_mining` uses facing electric drill rows around an eastbound collector.
Drill drops are checked natively: the north row drops onto the north/left lane,
and the south row onto the south/right lane. Do not apply the inserter far-side
rule to a mining drill. Four-tile column pitch leaves ordinary pole space.

| Resource | Opposing pairs / drills | Nominal supply per min | Fixed downstream port |
|---|---|---|---|
| Iron ore | 3 / 6 | 180 | (18.5,-62.5), east |
| Copper ore | 2 / 4 | 120 | (12.5,-33.5), east |

Copper's collector ends at (10.5,-35.5); a two-turn collar retains its downstream
port. Expansion adds complete upstream pairs without moving the port. Survey
each future column for actual pure ore, terrain and protected buildings before
reserving it. These two mineral edges currently have no additional eligible
same-row columns. Further capacity uses another paired block and an explicitly
checked connection to the existing ore trunk. Never bend individual drills or
weave the collector around an obstacle. A new auxiliary iron pair passed native
placement/resource/power preflight; its construction/connection is not yet tested.

The design bound is the minimum of connected ore supply, independent producer
capacity on each lane and belt capacity. Inserters, fuel, power and downstream
demand may lower actual flow. Redistributing stock never raises that bound.

## Observation and acceptance

`observe.belt_lane_observation` reports native left/right item counts at typed
ports, declared lane items/policy, and unstacked nominal capacity per lane.
Counts are instantaneous inventories, never flow rates or utilization.
Normal yellow belts in this pinned world report7.5items/sec per lane.

Paired smelting requires fresh production from every furnace and native plate
content on both shared lanes. Paired mining requires correctly oriented powered
drill groups, actual drop positions/receivers and real ore on both collector
lanes. EXP-0009 records the correction and Luna's actual construction attempts.
Sustained throughput still requires downstream load and timed machine/consumer
counters; full stationary lanes do not satisfy that gate.
