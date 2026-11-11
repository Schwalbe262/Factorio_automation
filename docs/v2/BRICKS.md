# Automatic bricks

`brick_core(catalog, x=82, y=-18)` plans two opposing stone furnaces with
33 belts, one splitter, two fast input inserters, two normal output inserters
and two poles. `brick_rates` derives recipe yield, time, furnace speed and
maximum coal use from the actual catalog. `audit_brick_core` checks footprints,
automatic stone/coal reach, opposite output lanes and inserter budgets.

The default core inputs are stone `(78.5,-22.5)S` and coal `(77.5,-18.5)N`;
both brick lanes leave at `(92.5,-15.5)E`. Three furnace pairs are reserved;
expansion preserves every existing entity and all ports. No furnace recipes
are assigned and no actor stone, fuel or brick insertion is emitted.

For a 2-stone / 3.2-second recipe at speed 1, the initial pair rates at
37.5 bricks/min, requiring 75 stone/min and at most 2.7 coal/min by catalog
energy use. Each fast input arm budgets 38.85 items/min against a conservative
120-item limit; each normal output arm budgets 18.75 against 30. A dedicated
stone pair rated at 60/min provides a stone-only ceiling of 30 bricks/min.
Shared source ratings do not multiply across outlets. These are nominal
balances and upper bounds, not measured production.

`BrickProduction(game,builder,catalog,*,stone_source=None,coal_source=None)`
provides read-only `prepare(obs)`, resumable `step(obs)` and fresh `proof(obs)`.
Absent explicit sources, preparation reads `material-sources.json` entries
`stone` and `brick-coal`. Both require the current world, a material supply ID,
exact native belt unit, position, direction and uncontaminated contents. The
stone source's mining marker is preserved for the shared bounded escape
planner. Missing sources fail before construction; a lost registered source
blocks resumption instead of rebuilding mining assets.

Preparation tries `(82,-18)`, `(98,-18)` and `(82,-2)`, rejects reserved cores,
then requires whole-site survey, both bounded transport routes, power and
resource-aware whole-plan preflight. No site is claimed native-ready here.
The ordinary builder executes the accepted plan and reserves its board block;
controller state is `brick-production.json`. Commissioning requires powered
arms, both furnaces having normally produced bricks with coal available, exact
underground pairs, native splitter priorities, separate stone/coal input lanes
and bricks on both output lanes. It does not certify sustained throughput.

An automatic furnace can report no recipe while output is full and fewer than
two raw stone remain. This idle state requires finished products, positive
expected output and compatible pure raw/fuel inventory, plus a same-query native
witness of normal output/input/fuel slots on the exact furnace unit. Wrong
recipes, products, qualities or mixed raw items still fail. The pinned native
input inventory is `defines.inventory.crafter_input`.

The public `brick_production` job is wired. Native commissioning in EXP-0016
passed at2276064: two normally fed furnaces, both brick lanes and complete paid
defense coverage. Its coal source remains bound by registry identity. The
separate SharedCoalTap migration can preserve this consumer while adding a
steel branch; this production module itself performs no source migration.
