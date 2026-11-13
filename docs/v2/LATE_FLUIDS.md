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

## Fluid rows (`produce` with one fluid ingredient)

Processing units, rocket fuel, concrete, electric engines and solid fuel are `produce` rows
(`blocks.row` fluid rows, `blocks.sizing` `fluid=True`): the machine comes from the recipe's
category and must have an input fluid box (assembling machine 2+, chemical plant); it is turned
so the box opens on the output side (`row.fluid_slot`, from the catalog's
`fluidbox_prototypes`). That column carries no output arm but a pipe-to-ground riser under the
output belt onto a header pipe over every slot; the header's west end is the row's typed
`fluid` input port (one fluid per row). `audit_row._check_fluid` checks every machine's box,
riser pair, one header network with the port, and no foreign fluid neighbour; growth extends
risers in place. `rocket_plan` gives the row a `fluid_source` key and a `FluidSupply` row.

**Fluid input** (`production_fluids`): `production_site` looks the row's `fluid_source` up in
`fluid-sources.json` (missing: planning failure `production_row_fluid_source_missing` naming the
key), routes the typed pipe to the header port with `plan_typed_fluid_connection` (other fluids
hard, buried crossings compiled, `route_relax` rungs of `produce`) and checks
`chemical_plan_isolation`; the plan carries the fluid underground pairs and crossings.
`ProductionRows.step` declares the handoff (consumer `produce:<item>`) before any build and
binds the first neighbour once it stands; the proof requires the bound edge. Offline proof:
`test_v2_fluid_rows.FluidRowControllerTests` (fake site, patched router). Only provable live:
the native pipe survey/router, the isolation query and the handoff declare/bind Lua.
Electric engines: the engine row keeps its stock chest (construction, blue science); when a
consumer needs a stock-only block's item on a belt, the tail adds a belt-output `produce` row of
that item (`rocket_produce_engine_unit_belt`). A belt row is its own instance
(`production_rows.row_key`: state `production-row-<item>__belt.json`, block
`production_row_<item>__belt`; `progression._same` matches produce rows by item and output).

## Growth and bus lanes as data

`block_growth.growth_options(root, world_id)` lists every built block's growth option for the
science-growth / bottleneck machinery (a `growth_options` provider): each `produce` row
(including fluid and lane-merge rows) offers `in_place` growth while reserved slots remain (one
machine more: `step_rate`, `cost.items`, and the `produce` request at the higher rate as
`action`), else `rebuild`. `advanced_circuit_production` and `chemical_science` take
`{"rate_per_minute"}` (validate; `count_for_rate`: two to four machines for a new block; the
roadmap's `chemical_science` step is rate-sized from `research_capacity` like red/green, default
5/min) and publish `core.growth_slots` (one entity group per reserved machine slot: machine,
arms, poles, belts), the `pack_growth.block_view` contract. A built block reports kind
`rebuild` (`limit: no_in_place_extension`): neither controller extends in place yet.
`growth_providers` has a `blocks` provider for the advanced-circuit bottleneck
(`state["root"]` = the runtime folder). For blue packs `pack_growth` needs
`PACK_STATES["chemical-science-pack"] = "chemical-science.json"` (the state has `plan`,
`core.growth_slots`) and an in-place extension in `ChemicalScience` before a slot can be built. The rocket bill's `bus` rows
declare lanes by demand (`lanes_needed`, `lanes_short` against the site plan's `lane_order`,
`taps` per consuming recipe). A new site plan reserves them: `StarterExecutors` builds its
`SiteService` with `rocket_plan.site_config(catalog)`, whose `lane_order` is the default plus one
lane per missing belt of every shared non-raw bill item (`bus_lane_order`, keeping
`SPARE_LANES` free; on 2.1.20: `plastic-bar`; ores stay on their mine trunks). An existing map
keeps its saved `site-plan.json`; there is no bus-lane extension capability yet, so
`tools/rocket_plan.py <run>` reports the shortfall against that map's lanes (`short`).

## Sites from the master plan (`block_sites`)

Fixed-shape oil/chemistry cores take their anchor from `SiteService`: `block_sites.anchors`
probes the core's footprint, allocates district blocks of that size and returns one anchor per
block (core reserved area on the block corner); the controller keeps its audits, routes and
land checks per anchor and `block_sites.keep` settles the chosen block (or releases all on a
failed prepare). Without a site service the legacy relative anchors stay (development map).
Done: `advanced_petroleum` (`site_allocation` in its state; `advanced_no_site_allocation`
when the plan has no block) and `fluid_products` (owner `fluid_product_<product>`,
`fluid_product_no_site_allocation`). Still relative anchors + `survey_slots`:
`chemical_production`, `advanced_circuits`, `chemical_science` (the same three-line change),
and the headers `gas_distribution` / `fluid_distribution`, which are source attachments (an
8-pipe manifold beside its outlet, like a tap) rather than district blocks.

## Known limits

* `fluid_product` sulfuric acid takes iron from the development-map bus tap (`bus-factory.json`,
  `IRON_TAP`); on a planned world it blocks `acid_requires_verified_shared_main_bus` until it is
  moved onto `planned_tap`. Its cell siting uses `survey_slots`, not `SiteService`.
* None of the three has a native deployment proof yet (see `ADVANCED_PETROLEUM.md`).
