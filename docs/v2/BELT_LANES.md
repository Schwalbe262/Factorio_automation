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

The new `lane_balancer` template splits one same-item belt into two branches,
then side-loads those branches from opposite sides into a straight output.
It costs14yellow belts and1splitter per output. It distributes onto both output
lanes; it does not claim equal draw from both input lanes or full throughput
under every downstream blockage.

The existing furnace collection rows stay in place. Each now feeds a small
reserved output module, so later consumers use its both-lane port:

| Product | Collection port | Declared collection lane | Both-lane supply port |
|---|---|---|---|
| Iron plate | (-17.5,-29.5), east | right | (-9.5,-29.5), east |
| Copper plate | (-17.5,-17.5), east | right | (-9.5,-17.5), east |

## Observation and acceptance

`observe.belt_lane_observation` reports native left/right item counts at typed
ports, declared lane items/policy, and unstacked nominal capacity per lane.
Counts are instantaneous inventories, never flow rates or utilization.
Normal yellow belts in this pinned world report7.5items/sec per lane.

`balance_smelter_lanes` requires real same-item content in both output lanes,
correct installed directions and native default splitter policies. EXP-0008
records Luna's first successful construction and an independent fresh check.
Sustained throughput still requires downstream load and timed machine/consumer
counters; full stationary lanes do not satisfy that gate.
