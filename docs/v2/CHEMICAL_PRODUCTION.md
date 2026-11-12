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

Unbuilt products prefer their published `plastic-gas` or `sulfur-gas` alias.
If no such alias exists, the unowned original `petroleum-gas` port is considered.
Accepted product sources remain pinned even if a new alias later appears;
changing their explicit override blocks. Other default registry keys are `water` in
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

The producer-approved fluid exit is narrow: a source inside its own PCB
reservation may leave through at most four straight outward cells, ending at
the first cell outside that reservation. The exact published typed port
must match the owning block and its defense scope. Other reservations, producer
assets, ores, terrain and buried pipe peers retain their normal checks. The
entire producer reservation is never removed from route keepouts.

An apron normally consists entirely of surface pipes. One controlled crossing
is available when the all-surface preflight reports an immovable obstruction at
an exact observed, owned ordinary transport belt. A fresh native survey must
confirm that belt's unit, position, type and direction. The existing bounded
pipe crossing compiler may then replace later apron cells with straight paired
underground mouths and their buried interior. The first new neighbor remains a
normal pipe, the full original apron path and all its headings remain fixed,
and the four-step limit is unchanged. Resource, terrain, foreign/unknown asset,
reservation and prospective producer obstructions still block.
Only that guarded crossing passes `minimum_start_run` to the shared router:
the native obstruction's zero-based apron index plus two, preserving a straight
cell after the paired mouth. Belt 6439 at index 3 requires five straight steps.
Other connections pass `None`, keeping their established routing priority.

All buried fluid-route assets must be owned belts, underground belts, poles or
inserters with exact native witnesses. New plans save `fluid_underground_pairs`
separately from solid-belt `underground_pairs`, and save unique asset witnesses
in `preserved_fluid_crossings` with name, type, unit, world, force, position and
direction. They are preserved existing assets, never chemical-owned bindings.
`fluid_crossing_proof` checks fresh observation and native identities before
handoff/build and commissioning, and rescans every tunnel interior for added
assets, resources, terrain and mining reservations. Legacy accepted plans
without these fields continue without claiming newly witnessed crossings.

The Water regression preserves source `(90.5,-25.5)` and its reservation. It
keeps the first new pipe at `(91.5,-25.5)` and pairs west-facing `(92.5,-25.5)`
with east-facing `(94.5,-25.5)` beneath existing northbound belt unit 6439 at
`(93.5,-25.5)`. Real-router and executable native-Lua fixtures reject replaced
units, unknown/foreign assets, resources, terrain, changed directions and new
tunnel obstructions. These fixtures do not establish live Sulfur production.

Candidate search retains the original six source-relative sites, then appends
two more north rows at y offsets -28 and -42, each with the same x offsets 0/22:
ten candidates total. Accepted product plans remain pinned. Every new candidate
still needs complete native land, resource, future-producer, power, fluid and
defense checks within the same twenty-second planning budget.

The fluid survey margin is `max(12, floor(((1.35-1)*Manhattan+4)/2))`.
Each tile outside the endpoint rectangle costs at least two extra route steps,
so this covers every excursion already permitted by the router's existing
length rule. The forty-thousand-cell survey limit, 200,000 materialized nodes,
six turns and existing detour rule remain unchanged. A distance 150 route gets
margin 28; ordinary short Plastic connections retain margin 12.

Read-only capture 3117605 and offline shortest-path diagnosis found the earlier
Sulfur core `(165.5,-74.5)` needed at least 170 Water tiles before crossing checks,
exceeding its existing 168-tile allowance. Larger survey margins did not fix it.
The further grid site `(165.5,-102.5)` has an offline Water corridor of 202 tiles,
six turns and ratio 1.34667, with two bounded underground pairs. Its unsurveyed
core rows and complete Gas, power and defense preparation still require fresh
native validation before construction.

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
