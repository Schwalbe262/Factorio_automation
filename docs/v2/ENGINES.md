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
a twenty-second planning budget. Each legal site gets its best station before
any site gets a second station; steel routes and future footprints are cached
only during that preparation. The power connector uses the first physically
clear pole already in the planned group, without changing the blueprint or its
build order. Native land/resource surveys, both whole adapters,
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

Read-only native whole preparation passed at tick **2803699**: anchor
`(118.5,-114.5)`, iron input `(115.5,-114.5) E`, steel input `(143.5,-108.5) W`
and engine output `(144.5,-98.5) E`. Six material-route attempts reached this
third legal site. Steel and iron adapters were 112 and 118 tiles, each four turns
and within the unchanged detour gate. The production plan has 370 entities and
requires four additional armed coverage posts. Current and future whole-plan
preflight passed; no new entity or buried span enters the refinery, red science
or laboratory reservations, and the fixed gas exit stays clear.

Underground belts preserve crude pipes 6905 and 6801 with their exact reciprocal
native peers. Engine construction and proof both call `surface_pipe_proof` before
acceptance. Observed preparation wall time was 20.765 seconds; the existing
twenty-second routing/preflight query budget remains unchanged. Initial source
checks and perimeter coverage retain their existing call scope. This records prospective preparation and
pipe preservation, not live engine construction or commissioned throughput.

The component mixer includes an empty eastbound belt at local `(-2,7)`.
Its inline approach makes the pipe input a side load onto the right lane;
the gear input occupies the left lane. A sole perpendicular feeder would form
a curve and preserve its incoming lane, putting both components on the left.
The static audit checks this distinction and rejects the core without that tail.

Before any consumer asset is built, `prepare_prebuild_feed_replan(obs)` can
preview replacement feeds around subsequently commissioned foreign producers.
It requires the original two exact source bindings, unchanged surplus tap,
fresh steel and coverage proofs, and canonical registered plans and saved units
for every overlapping foreign asset. Such assets remain foreign and are never
added to Engine bindings. The original core must match the canonical design or
its exact previous form missing only the empty mixer tail. Machines, recipes,
ports, anchor, reservation and extension slots remain fixed.

The sole automatic replan attempt releases only this receiver's old unbuilt
external feeds through the transport revision contract. Native whole/future
preflight and preserved pipe peers must pass, ignoring only exact conflicting
new Engine coverage posts pending their later paid relocation. The publication
intent retains old core/plan hashes and the immutable-state guard, plus the new
core, fresh lane audit and replacement feeds. A board compare-and-set precedes
one atomic Engine checkpoint update; interruption can resume with either the
old or new board revision. Changed sources, coverage, foreign reservations or
candidate core fail closed. Relocation can incur costs only after this adoption,
and then binds the newly accepted core and plan. No positive native replan or
post recovery is claimed by the focused offline tests.
