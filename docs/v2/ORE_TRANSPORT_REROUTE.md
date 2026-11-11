# Ore delivery off foreign resource patches

`OreTransportReroute(game,builder,catalog,smelting,outposts,*,bus_factory=None,green=None)`
provides read-only `prepare(obs, parameters=None)`, resumable `step` and fresh
`proof`. The current capability handles iron delivery; copper delivery was
audited and has no crossings in the offending smelting-owned set.

Native audit at tick 2047789 found 29 smelting-owned resource overlaps, all in
iron delivery: 23 belts over copper ore and six over iron ore near local source
aggregation. Registered joins 1119 and 1122 and all mining-bank ports stay fixed.
Preparation derives the final join from saved `consumer_connection` identities
and follows its existing straight belts to the first off-patch tile, within
twelve steps. The current off-patch source is belt 1126 at `(20.5,-54.5)S`.
It preserves the exact local collector, joins and necessary existing escape;
it adds no mining tags or permission to rebuild those assets if lost.

The bounded normal transport planner connects that source to the fixed iron
furnace input `(-43.5,-36.5)S`, avoiding resource patches and mining reservations.
Native preparation at tick 2075356 passed all three gates: the whole alternate
smelting plan, the intermediate cutover with later retirement assets still
present, and the nonconflicting prebuild. The result is 86 tiles for 82 Manhattan,
four turns, and two underground pairs crossing the main bus. Existing six-turn
and `length <= 1.35*Manhattan + 4` limits remain unchanged.

The reviewed plan requires 68 normal belts and four normal underground mouths:
71 prebuild entities, one changed corner, 69 later recoveries and 13 retained
delivery entities. All added hardware is stocked before prebuild; replacement
stock is checked again before guarded cutover. Recoveries target exact units,
persist their started batch before returning an action, and prune confirmed
completed recoveries before rebuilding. Mining batches contain at most eight
ordinary actions. No drills, poles, source ports or producer recipes move.

Publication validates and updates only `smelting-district.plan`, the board's
`smelting_district` block, `bus-factory.source_plan` and
`logistic-science.source_plan`. Other bus, red and compact factory plans stay
unchanged. Mixed old/new publication resumes idempotently after interruption.
The anonymous original route metrics remain historical; the new delivery
suffix has explicit source, destination and metrics in `ore_delivery_reroutes`.

Retirement requires exact new underground pairs, uncontaminated iron on the
added path, fresh powered mining-bank proofs, complete smelting proof and an
increase in iron products finished after the new route was built. Proof is
rechecked during retirement. This commissions normal transport and production;
it does not certify a sustained rate or claim that local mining overlaps vanish.

Only read-only preparation was performed by this implementation task. Parent
owns CLI/job wiring, deployment, journals and commits. State is stored in
`ore-transport-reroute.json` only when the parent later executes the controller.
