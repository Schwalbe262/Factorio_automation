# Permanent chemistry cells

`ChemicalProduction(game, builder, catalog, perimeter, *, gas_source=None,
coal_source=None, water_source=None)` exposes read-only `prepare(obs, parameters)`,
paid resumable `step(obs, parameters)` and fresh `proof(obs, parameters)`.
Parameters are exactly `{'product': 'plastic-bar'}` or `{'product': 'sulfur'}`.
The controller checkpoint is `chemical-production.json`; public job wiring is
owned by the executor. Only the executor under the existing writer lock calls
`step`. Preparation does not write checkpoints or fluid declarations.

Each 18 by 11 reserved cell has one chemical plant, ordinary physical fluid
connections, fast inserters, a fixed eastbound solid outlet and an empty second
plant slot. The slot and two output continuation tiles are checked and kept
clear, without charging for or building a future plant. Natural plastics or
sulfur-processing research and enabled recipes are required. Ingredients always
arrive through ordinary belts or pipes.

The installed recipes give either plant a nameplate output of 120 items/minute.
Plastic consumes 60 coal and 1,200 petroleum gas/minute at that rate; sulfur
consumes 1,800 water and 1,800 petroleum gas/minute. These are catalog capacities,
not measured supply guarantees. Recipe expansion for 20 chemical science/minute
requires 60 plastic and 10 sulfur/minute: 30 coal, 750 petroleum gas and 150
water/minute. That gas demand requires two basic refineries at their 540/minute
catalog nameplate; one existing refinery cannot promise the complete demand.

Default source registry keys are `petroleum-gas` and `water` in
`fluid-sources.json`, and `plastic-coal` in `material-sources.json`. An override
must still equal an explicitly published typed source. Every source preserves
its exact world, unit, fixed position, direction and item/fluid. Existing coal,
brick-coal and steel-coal outlets are not selected implicitly. Sulfur reports a
missing independent gas branch after plastic owns the single petroleum outlet;
it also requires a separately published water source. Current fluid handoffs
permit one consumer per source edge.

Consumer construction first obtains complete geometric defense coverage, then
declares its fixed first fluid neighbor through `fluid_handoffs.declare_handoff`.
Once that neighbor is observed, `bind_handoff` receives only the controller's
saved layout unit. An identity change blocks recovery instead of adopting the
replacement. Pipes are ordinary paid items, and neither controller path inserts
fluids or alters research or production counters.

The producer-approved petroleum exit is narrow: a source inside its own PCB
reservation may leave through at most four straight outward cells, ending at
the first clear cell outside that reservation. The exact published typed port
must match the owning block and its defense scope. Other reservations, producer
assets, ores, terrain and buried pipe peers retain their normal checks. The
entire producer reservation is never removed from route keepouts. This permission
corresponds to the current refinery's deliberately empty east output apron;
it does not authorize a general factory crossing.

Fluid routes have bounded surveys and detours, native underground span limits,
separate prospective networks and explicit reciprocal fluidbox edges. The
recipe-relative fluid order maps plastic gas to physical input 1, and sulfur
water/gas to physical inputs 1/2. Those prototype indices are resolved to the
recipe's active runtime storage indices with `get_fluid_box_prototype`; inactive
outputs are not indexed, and merged storage cannot combine different fluids.
Proof requires exact saved assets, powered
machine and inserters, the configured recipe, real products finished, real input
fluid and output belt stock, native reciprocal peers and no undeclared peers.
`proof()['source']` returns the exact typed solid outlet for later consumers;
the controller does not silently publish or attach a consumer route.

Offline validation includes flattened executable Lua positive/negative cases
for fluid purity, power, recipe, production, physical input indices, reciprocity,
extent, output stock and exact identities. Read-only native tick 2,626,802 found
the candidate plastic core `[[154,-64],[172,-53]]` buildable with ordinary rock
clearance; this is a world-specific survey, not a global placement guarantee.
At that tick plastics/sulfur research and published gas/chemistry-coal sources
were still absent, so complete production preparation correctly remained blocked.

Planning failures now retain bounded `evidence` in the public blocked receipt:
candidate position, stage, typed source unit/position, destination, visited count,
candidate/land counts and the unchanged twenty-second budget. Rejected reservation
and land candidates are reported too. Full paths and native query logs are omitted.
Crossed existing pipes carry the shared transport planner's exact native witnesses;
fresh `surface_pipe_proof` guards them before any handoff/build and at acceptance.
Read-only tick2667408 isolated a later plastic blockage to the coal corridor at all
five land-valid sites, while the fixed gas source and its narrow producer apron
passed. This diagnostic does not authorize a source move or relax resource,
reservation, turn, detour or cost requirements.

Read-only complete plastic preparation passed at tick **2755259** in 8.73 seconds
after generic whole-skeleton obstacle pruning was added to the shared router.
The source ports remained unchanged. The accepted plant `(160.5,-43.5)` has an
18-tile/two-turn gas route and a 280-tile/six-turn coal route (268 Manhattan,
1.0448 detour). Only 5,070 nodes were materialized within the same 400,000-node
budget; 138,051 impossible skeletons were rejected by indexed segment checks
before expansion. Whole resource/terrain/future-reservation, power, defense and
the crossed existing steam-water pipe witness all passed. This is preparation,
not live plastic production. The executor is holding construction while planning
an upstream two-outlet gas manifold for independent plastic and sulfur feeds.
