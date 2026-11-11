# Automatic advanced circuits

`AdvancedCircuitProduction(game, builder, catalog, perimeter, *, bus_factory=None,
science=None, green=None, chemical=None)` exposes `prepare(obs)`, `step(obs)` and
`proof(obs)`. Its resumable checkpoint is `advanced-circuits.json`. Preparation is
read-only. Jobs and the single world executor must invoke `step`; calling
`prepare` never publishes reservations, checkpoints or gameplay actions.

The initial 31×22 reservation contains four assembling-machine-1 entities: two
advanced-circuit machines, one dedicated electronic-circuit machine and one
dedicated copper-cable machine. All green circuits and wire come from these
subcells. Existing science components retain their original machines and feeds.
The fixed ports, headers and four underground pairs survive expansion to four
advanced-circuit machines, one green machine and two wire machines. Expansion is
reserved and audited; this controller initially builds the four-machine form.

Installed recipes and machine speed give these **nameplate** balances:

| Quantity per minute | Initial | Reserved full expansion |
| --- | ---: | ---: |
| Advanced circuits | 10 | 20 |
| Iron plates | 20 | 40 |
| Copper plates | 50 | 100 |
| Plastic bars | 20 | 40 |
| Electronic circuits made internally | 20 | 40 |
| Copper cable made internally | 100 | 200 |
| Cable machine capacity | 120 | 240 |

The iron and copper feeds are new, finite, native **surplus** bus taps. Existing
critical and normal consumers retain their priorities. Spare producer capacity
and sustained delivery remain prerequisites; a second wire machine does not add
raw copper supply. At current plate capacities the larger form needs a fresh
shared-demand assessment. Component buffers also create transient demand: each
wire machine can consume 60 copper/min and the green machine 60 iron/min at
nameplate while filling. Those figures are distinct from the steady BOM above.

The raw mixer carries copper on its left lane and iron on its right lane. A
second mixer carries wire on its left lane and electronic circuits on its right
lane. Plastic has a separate westbound header and ordinary long-handed
inserters. Green input/output and both wire output paths use fast inserters,
including the future 120-wire/min input and 40-green/min output budgets.
Inserter planning limits are explicitly conservative design budgets of 30 items
per minute for normal/long arms and 120 for fast arms. The current catalog does
not export arm motion speeds; these limits are not native measured rates.
Preparation checks native prototype types and default pickup/drop vectors.
No custom vectors, actor production feeds, item grants or handcrafted advanced
circuits implement this line.

Preparation requires naturally researched `advanced-circuit`, all ordinary
construction recipes and a fresh `ChemicalProduction.proof(obs,
{'product':'plastic-bar'})`. The exact world/unit/facing of its fixed plastic
outlet and its producer-plan revision are pinned. Missing or unverified plastic
fails before any stock acquisition or bus changes.

Both prospective taps are previewed against the same actual old bus. The joint
compile must preserve the independently accepted added geometry, retain all
other bus contracts and remove precisely two distinct owned northbound belts.
All three ingredient routes, the initial/future cores, power and new tap
footprints pass native resource/terrain/collision preflight before accepting a
plan. Routing retains the shared six-turn and 1.35×Manhattan+4 gates, strict
producer reservations, typed source aprons and exact native pipe witnesses.
Routing and native layout preflight share one 20-second query budget and at most
twelve material-route attempts across a finite set of district and station
candidates. Initial source proof/setup and the separate coverage preview are
outside that query budget; no end-to-end 20-second guarantee is claimed.

Execution stocks both tap additions before the first cutover. Each actual tap
uses the ordinary paid `BusTap` coordinator. The second tap is **freshly prepared
against the now-published bus**, binding the first tap's real native units. Its
geometry, fixed contracts and original second recovery unit must match the
accepted proposal. A cached second snapshot of the old bus is never executed.
`BusTap` preserves and republishes its authoritative factory, red/green and board
copies. Advanced-circuit assemblers are registered only in their own board block.

The complete core and all external adapters are registered before actual
`ensure_coverage` placement. The shared perimeter protection therefore sees
future core space, planned feeds and fixed-port exits before selecting a paid,
normally armed post. The read-only coverage preview covers the proposed core;
it does not certify future post placement along unpublished adapters. A fresh
whole-plan preflight after defense blocks unexpected new obstructions before
the first production asset. Existing pipe-crossing identities and reciprocal
peers are checked before construction and in commissioning proof.

Commissioning requires exact bound asset units, all four powered recipe
machines with real completed products, normal pure inputs/outputs, ordinary
inserter vectors and held items, native underground pairs, both native surplus
splitters, typed mixed lanes, current plastic-producer proof and actual advanced
circuits at the fixed output. Every underground transport line, including buried
lines, is inspected for contamination. Failure evidence identifies the stage or
native subgate. This snapshot is not a sustained throughput or combat proof.

Validation: fifteen focused tests cover BOM/capacity, all growth geometries,
lane reachability, native mouths, future slots, preservation, both-tap whole
preflight, fresh second-tap identities, missing prerequisites, defense/pipe/lost
asset blockers and executable real-Lua proof rejection cases. Read-only native
tick **2972759** confirmed research complete and correctly returned
`commissioned_automatic_plastic_factory_required`, with no checkpoint written.
After normally commissioned Plastic proof2991431, fresh whole preparation
**2993245 passed in18.437s** at core(100.5,-138.5),555entities. Plastic route
is150tiles/two turns/1.0417ratio; iron121/two/1.0; copper141/four/1.1102.
Both prospective surplus taps, all three feeds, initial/future cores, power
and defense preview passed. Luna started actual construction afterwards;
commissioning is still pending at this checkpoint.
