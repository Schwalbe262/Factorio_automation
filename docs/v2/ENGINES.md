# Automatic engines

`EngineProduction(game, builder, catalog, perimeter, *, bus_factory=None,
science=None, green=None, tap=None, steel=None)` exposes `prepare(obs)`,
`step(obs)` and `proof(obs)`. The optional components keep existing bus and steel
controllers synchronized. `prepare` is read-only. The caller executes ordinary
actions returned by `step`; the controller never inserts ingredients into machines
or handcrafts engines, gears or pipes for production.

The initial 31×19 block has two engine AM1s, one shared gear AM1 and one shared pipe
AM1. Four engine positions are reserved. `engine_core(catalog,x,y,count=2)` supports
one through four engine assemblers without changing old entities, the reservation,
input ports or output port. Live expansion is not exposed yet. Shared gear/pipe
headers carry distinct native belt lanes. Steel uses a separate westbound header
and native long-handed inserters. Four declared underground pairs cross the pipe
return, two input-arm groups and the two headers. Every mouth has a straight
approach/exit; no custom pickup/drop vectors are installed.

`engine_rates` derives time, yields and all material balances from the installed
catalog. With the inspected 2.1.20 recipe, engine crafting takes ten seconds and
AM1 speed is 0.5. The initial block is rated **6 engines/min**, requiring
**24 iron plates/min and 6 steel plates/min**. Four engine machines would be rated
12/min and require 48 iron and 12 steel/min. The current two-furnace steel source
is shared and rated 7.5 steel/min, so it cannot sustain that future engine capacity.
None of these numbers certifies measured output. Component assemblers have a
combined catalog iron demand of 180/min at maximum crafting speed before inserter
and backpressure limits; startup buffer filling can exceed steady engine demand.
The 30-item/min normal-inserter budget is a conservative design capacity check,
not an upper bound on actual inserter throughput.

Preparation requires native engine research, enabled component/machine/inserter
recipes and fresh proof of normally produced steel. It binds the existing fixed
steel outlet and furnace units. A new **SURPLUS** iron tap is selected from finite
valid shared-bus station rows using existing `BusTap`, preserving all other source,
consumer and lab ports and bus copies. The tap's changed corner is normally
stocked before its one guarded recovery.

Six district-relative core sites and at most twelve complete material routes share
a twenty-second planning budget. Native land/resource surveys, both whole adapters,
future machine footprints, power and armed coverage must pass before the operation
is accepted. Shared corridor gates remain six turns and `1.35×Manhattan + 4`.
Resources are forbidden under underground spans as well as surface construction.
Virtual keepout footprints preserve empty future space and never enter the build
plan. The engine core is reserved before coverage deployment; a fresh whole-plan
preflight follows coverage and precedes the first producer build.

`engine-production.json` binds every installed entity to its native unit. Loss or
replacement blocks automatic reconstruction. Proof checks all source identities,
the native surplus priority, underground partners, recipes, real product counters,
power, native inserter pickup/drop tiles, held items and normal machine inventories.
All core belt contents, including buried transport lines, must match their types;
the shared component lanes must remain gear/pipe. Actual normal engines must reach
the fixed output belt. `proof()['output']` is the typed exact-unit handoff for later
blue science consumers. Empty finite belts eventually backpressure production.

Read-only native whole preparation passed at tick **2597954**: anchor
`(118.5,-92.5)`, iron input `(115.5,-92.5) E`, steel input `(143.5,-86.5) W` and
engine output `(144.5,-76.5) E`. It preserved steel outlet 6213 and producing
furnaces 6172/6176. The new surplus tap selected station -91.5. Steel and iron
adapters were 90 tiles/two turns and 109 tiles/four turns respectively, passing
current and future whole-plan preflight. The plan needs 289 belts, sixteen
underground belts, four AM1s, eight normal inserters, two long inserters and
eighteen poles, plus normal tap assets and four armed coverage posts. There were
42 ordinary tree/rock clearables. This records preparation, not live engine
construction or commissioned throughput.
