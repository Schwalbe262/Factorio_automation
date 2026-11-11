# Semantic industrial operations

`IndustrialOperations(game,builder,catalog,defense,*,bus_factory=None,science=None,green=None)`
connects the existing paid `BusTap`, `SteelProduction` and `BrickProduction`
controllers. Optional `tap`, `steel` and `brick` arguments allow explicit
controller injection. Public jobs need no coordinates:

- `steel_supply(obs)` installs and commissions surplus iron plus registered coal.
- `steel_production(obs)` supplies and commissions the defended steel pair.
- `brick_production(obs)` supplies and commissions the defended brick pair.
- `steel_supply_proof`, `steel_production_proof`, `brick_production_proof` return
  fresh evidence or `None`.

Read-only `prepare_steel_supply`, `prepare_steel_production` and
`prepare_brick_production` return reviewable plans or explicit blockers. Only
the execution methods persist `industrial-operations.json` or invoke existing
controllers; they emit ordinary actions through those controllers.

`free_tap_parameters` derives available NORTH surface rows from the saved bus,
keeping existing station approaches and future columns fixed. Steel requests
the actual catalog iron demand, uses native left input/left output priorities
and delegates the one owned belt cutover, paid hardware, complete bus-copy
publication and native typed proof to `BusTap`. At most twelve candidate taps
are tried. Initial exact outlet/splitter identities are bound only after a
successful tap proof; every later use checks native identity, contents and
priorities. Coal comes from the current world's `material-sources.json` entry
`coal`; bricks use `stone` and `brick-coal`. No unit numbers or raw-source
coordinates are hardcoded, and a changed registered identity blocks resumption.

If the original coal outlet cannot egress, `SharedCoalTap` commissions an
explicit shared-header outlet first. `adopt_shared_coal_source` accepts that
proven `steel-coal` alias only while steel has no installed/planned controller
state, checks current native units and records the migration. It preserves the
unused original source and the original brick port; it never retargets a built
steel consumer. Both outlets retain one shared bank-capacity declaration.

`industrial_site_anchors` derives eight eastern candidates from current bus
bounds and production reservations. It includes northern land alternatives to
the initial water-adjacent brick sites. Existing libraries survey complete
land/resource footprints, plan both bounded feeds and power, and preflight the
whole alternate plan. Preview queries share a 20-second planning deadline,
checked between queries; one in-flight query may finish after the deadline.
Six-turn and detour gates are unchanged. Failure creates no partial producer.

Before producer construction the accepted core is reserved, and
`DefensePerimeter.ensure_coverage` commissions its scope with the normal ten
full-magazine requirement. Complete route/pole coexistence is checked again
after posts are placed. A hash records that authorization, avoiding repeated
whole-plan RCON work on every build action; coverage and source guards still
run, and final proof requires both coverage and ordinary production. A changed
authorized plan fails closed. If a new post obstructs an adapter, the explicit
post-coverage preflight blocker requires replanning rather than clipping it.

Native read-only steel-supply preparation at tick 2206287 selected row `-116.5`
from current geometry, demand 37.5 iron/min, exact old corner 4335 and added
stock of one splitter, 18 belts and four underground mouths. This was a tap
preparation result. Steel/brick commissioning, available surplus and sustained
rates are proved separately; no automatic throughput or actor-fed production
is claimed here. Parent owns public job wiring and live deployment.
