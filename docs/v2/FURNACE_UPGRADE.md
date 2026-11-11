# Paid steel-furnace replacement

`FurnaceUpgrade(game, builder, catalog, smelting, paired, outposts=...)` exposes read-only
`prepare(obs, {"resource": "iron-ore"})`, resumable `step` with the same
parameters, and `proof(obs)`. Copper uses `"copper-ore"`. Optional
`"rate_per_minute": 300` selects the minimum replacements needed to reach
that catalog capacity, alternating rows by column. The public `furnace_upgrade`
job accepts the same resource/rate flags and binds the live outpost inspector.
It checks remaining active mining before preparation and each new replacement
transaction; a committed gap is completed before checking the next transaction.

The controller requires naturally completed `advanced-material-processing`,
the enabled native steel-furnace recipe, a commissioned paired smelting
district and connected nominal mining sufficient for the selected bank's
requested capacity. Resource-only requests upgrade the full bank. Existing six-pair iron rows derive 450 plates/min
from the installed catalog; three-pair copper rows derive 225/min. These are
nameplate limits, with no sustained-rate claim.

For the current twelve stone iron furnaces, requesting 300/min upgrades four
furnaces, two on each producer row, and preserves eight stone furnaces. Mining
must cover the requested 300/min. A later 360/min request selects four further
replacements, giving 375/min catalog headroom, and requires mining of at least
360/min. The controller accepts a new target only after its previous target
has verified; mixed furnace rows retain their actual types throughout.

Every transaction stocks one **normal-quality, paid steel furnace before
mining**, persists the old stone furnace's exact unit/world identity, then
returns one guarded ordinary mine. It reobserves the gap before returning an
ordinary build. A different stone-furnace unit is never adopted as a recovery
target. A replacement must complete real production before the next old
furnace is removed. An uncertain executor action must be reconciled by the
normal durable job boundary; this controller never retries transport errors.

A working input arm can drop a stack into the temporarily empty footprint.
If placement fails, a native check accepts only normal coal or the requested
ore inside that exact committed gap, with no other obstruction. Separate pickup
and build actions are insufficient: a working arm can spill again between game
ticks. `build_furnace_gap` therefore performs normal inventory pickup, exact
one-item payment and construction in one native command, without advancing a
tick between them. It validates the saved pending old unit/world, actor, normal
prepaid item, all ground stacks and pickup capacity before mutation. It never
removes neighboring hardware or unrelated items. A dry run makes no changes.

Preparation preflights the complete prospective source plan while ignoring
only the exact old units selected for replacement. Stone and steel furnaces
share the installed two-by-two footprint. Ore, coal, plate ports, adapters,
belts, inserters and poles retain their geometry. If another job changes these
ports or infrastructure during replacement, the controller blocks instead of
publishing a stale layout. Run after bus migration has finished and avoid
interleaving source expansion jobs during a replacement transaction.

Each replacement synchronizes the smelting plan/banks, paired plan/banks,
PCB board and any existing `bus-factory.json` / `logistic-science.json`
`source_plan` copies. It preserves each consumer's production layout, routes,
ports and unrelated metadata. Missing consumer files are not created. Before
the first recovery, publication checks every document's world/catalog identity
and the source entities/ports/underground-pair revision. A changed copy blocks.
A persisted publication intent accepts only the exact previous or resulting
revision after an interruption; atomic writes are per file and a resumed call
finishes all copies before returning a recovery action. Repeating a completed
publication performs no writes. Nominal rates come from actual furnace entity types and catalog
recipe yields. Later `smelting_capacity` requests preserve existing furnace
types and use the upgraded type for additional slots; the six-pair limit
remains in force. Native proofs cover every furnace's products, burner fuel
or electric supply, powered inserters, distinct ore/coal feed lanes and both
paired plate-output lanes.

The installed steel-furnace recipe costs six steel plates and ten stone
bricks. A 225-to-300/min partial upgrade needs **24 steel plates and 40 bricks**.
Replacing twelve iron furnaces therefore needs **72 normally produced
steel plates and 120 normally produced bricks**. The controller delegates
ordinary collection/crafting to the bootstrap and never handcrafts steel or
feeds furnace inputs from the actor. A steel-production block alone does not
provide the required bricks; commission an ordinary brick source or retain
normally produced brick stock before live deployment.

The new controller has pure and temporary-file regression tests for payment
before recovery, exact identities, fresh-observation builds, restart recovery,
authority synchronization, consumer identity/revision guards, interrupted
publication recovery, unchanged ports, subsequent 450/min expansion and
native-proof requirements. No live replacement or throughput acceptance has
been performed by the implementation worker. After verified completion, derived
bus capacity profiles refresh from registered furnace types without changing
layout or port contracts. Live-rate diagnostics remain separate.
