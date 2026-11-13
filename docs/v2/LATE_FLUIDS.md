# Late fluids: advanced petroleum, acid/lubricant, fluid headers

Wired for the rocket tail (`rocket_plan.tail`). Controllers and their own docs:

| Capability | Controller | Parameters | Publishes |
|---|---|---|---|
| `advanced_petroleum` | `advanced_petroleum.AdvancedPetroleum` (`ADVANCED_PETROLEUM.md`) | `{}` | `heavy-oil`, `light-oil`, `advanced-petroleum-gas` |
| `fluid_product` | `fluid_products.FluidProducts` | `{"product": "sulfuric-acid" \| "lubricant"}` | the product name |
| `fluid_distribution` | `fluid_distribution.FluidDistribution` | `{"source_key", "aliases": [a, b]}` | both aliases |
| `oil_gathering` / `water_gathering` | existing, now with optional `{"source_key"}` | namespaced independent source | the key |

Executors build them lazily, one instance per profile (`StarterExecutors._advanced_petroleum`,
`_fluid_products`, `_fluid_distribution(parameters)`, `_oil_source(key)`, `_water_source(key)`;
all take the shared `perimeter`). Handlers: `handlers/late_fluids.py`. Custom actions
`build_fluid_distribution` and `configure_advanced_petroleum` are dispatched in `runtime.py`.
Dry runs: `postmortem.DRY_RUN`; remedy rungs: `ROUTE_RELAX_1/2`, read by the typed pipe router
(`chemical_production.plan_typed_fluid_connection`: turns and detour from `remedy.route_limits`).

## Supply rule (`rocket_plan.FluidSupply`)

Every fluid a block reads has its own source key (the module declares it:
`advanced_petroleum.INPUT_KEYS`, `fluid_products.SOURCE_KEYS`). The tail emits, before the
consumer: water -> `water_gathering {source_key}` (an independent intake), crude ->
`oil_gathering {source_key}`, a produced fluid -> `fluid_distribution` splitting the fluid's
current free outlet into the consumer's key and the next free outlet (`<port>-2`, `-3`, ...),
so N consumers form a chain and no outlet is claimed twice.

## Cracking balance (`rocket_plan.crack_balance`)

The bill no longer reports refinery coproducts as surplus: the multi-product refinery runs only
as many crafts as the binding product needs after every heavier surplus is cracked down the
chain found in the catalog (`cracking_chain`: heavy -> light -> gas). `bill["cracking"]` gives
crafts and chemical plants per stage and whether the advanced block's single cracker per stage
carries it (`fits_block`). On the 2.1.20 Space Age bill gas binds, so neither heavy nor light
backs up; when light demand binds (a solid-fuel heavy bill) the gas left over is still reported
as surplus, because cracking cannot go upward.

## Fixed bug

`fluid_distribution`: `execute_build_fluid_distribution` records the ACK (binding, cleared
`pending_build`) through its own controller instance; the long-lived instance kept the stale
pending build and blocked on reconciliation (and could overwrite the binding). `step` now
re-reads the checkpoint (`_reload`).

## Known limits

* `fluid_product` sulfuric acid takes iron from the development-map bus tap (`bus-factory.json`,
  `IRON_TAP`); on a planned world it blocks `acid_requires_verified_shared_main_bus` until it is
  moved onto `planned_tap`. Its cell siting uses `survey_slots`, not `SiteService`.
* None of the three has a native deployment proof yet (see `ADVANCED_PETROLEUM.md`).
