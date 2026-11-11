# Move green plate transport off resources

`GreenBusReroute(game,builder,catalog,bus_factory,science,green,smelting)` offers
read-only `prepare(obs)`, resumable `step(obs)` and fresh `proof(obs)`. It moves
the two existing green bus stations; it does not add duplicate consumers or
rebuild the production factory. Default station candidates are tried with the
normal bounded planner, not a custom maze route.

The first candidate changes copper to splitter y-107.5/output(12.5,-108.5)E
and iron to splitter y-83.5/output(12.5,-84.5)E. Red stations, trunk columns,
source capacities, producer ports, reserved parallel columns, compatible old
underground pairs and the green core's fixed raw inputs remain unchanged.
Current core inputs remain copper(40.5,-80.5)E and iron(40.5,-77.5)E; the
science output and lab entry also stay fixed.

Preparation traces the exact two old bus branches and both old adapters from
saved plans. It binds native units, recompiles the moved bus and plans both new
adapters together. Whole alternate preflight ignores only obsolete owned
transport. A separate cutover preflight ignores only the actual cutover group,
leaving later retirement assets physically present. A third preflight proves
the nonconflicting prebuild can coexist with the original factory.

Execution acquires all added hardware normally, clears approved nature and
prebuilds safe new assets. It stocks the remaining replacements before mining
cutover units in guarded batches of at most8. Surface input/output adjacency,
changed material ownership and complete underground pairs determine what must
wait for cutover. Completed recoveries are pruned before replacement builds at
the same coordinates. All poles and production machines are preserved.

After new construction and native splitter configuration, publication updates
the full bus/green network copies and red bus-only copies. It adds no machines.
Fresh documents are checked before writes; mixed old/new publication can resume
idempotently. Both legacy `green-*-plate` metric rows and canonical green station
rows receive new adapter metrics. The core port list is checked explicitly.

Retirement waits for both typed raw feeds, exact new underground partners,
unchanged retained units, all native priorities and the compound factory proof:
fresh source smelting, all12 compact machines and all8 powered stocked labs.
It rechecks proof while retiring only obsolete owned green transport. It emits
no actor feed, grants, recipe changes or producer-bandwidth claims.

The generic trunk candidate generator preserves accepted legacy corridors and
their original ranking. Only when that family has no accepted corridor does
it try hard-component edge lanes, short independent endpoint escapes and
outside-port clearance bars, within the remaining search budget. Every
candidate is still a complete orthogonal corridor under six turns and
`length <= 1.35*Manhattan + 4`; no quality gate was loosened.

Native read-only preparation of the current world passed without injected
paths: copper78tiles/4turns for56Manhattan, iron43tiles/6turns for35Manhattan.
The reviewed plan has147 prebuild entities,8 cutover recoveries and85 later
retirements, costing143belts,2splitters and8underground mouths in added hardware.
The cutover check passed with all85 retirement entities still present. These
are preparation results; no live cutover or sustained-rate success is claimed
by this implementation. Root owns deployment, journals and token reporting.
