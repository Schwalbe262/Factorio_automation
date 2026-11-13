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
arms, poles, belts), the `pack_growth.block_view` contract. A verified block with a slot
reports kind `in_place` (see "In-place growth" below; no slot left: `rebuild`).
`growth_providers` has a `blocks` provider for the advanced-circuit bottleneck
(`state["root"]` = the runtime folder). Blue packs are in `pack_growth.PACK_STATES`
(`chemical-science.json`). The rocket bill's `bus` rows
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
when the plan has no block), `fluid_products` (owner `fluid_product_<product>`,
`fluid_product_no_site_allocation`), `chemical_production` (owner
`chemical_production_<product>`, `chemical_no_site_allocation`), `advanced_circuits` (owner
`advanced_circuit_production`) and `chemical_science` (owner `chemical_science_production`,
`blue_science_no_site_allocation`). `survey_slots` stays as the native land check per anchor.
The remedy rung `site:more_blocks` (`remedy.SITE_MORE_BLOCKS`, `block_site_candidates` 12,
`block_sites.CANDIDATES_MAX`) widens the block search for all of them.

**Planned-bus inputs** (`block_sites.bus_taps`, the `production_site._taps` pattern): on a
planned world (`block_sites.planned_bus`: the published `planned_main_bus`) the acid cell's iron
port and the advanced-circuit block's iron and copper ports are `planned_tap.connect_inputs`
taps on the block's allocation. The taps are stored in the state (`taps`; acid
`iron_supply: planned_bus_tap`; advanced `bus_kind: planned`, phase starts at `build`), kept out of
the block plan (`own_entities`), published and cut over (`planned_tap.publish` / `cutover`)
before the block's `ensure_plan`; the advanced block then pins each tap port's belt unit as its
`tap_sources`, and both proofs accept the planned bus (taps built, stations built). A tap that fits
nowhere returns `plan_needs` kind `tap`. The bus-factory / `BusTap` path runs only without a
planned bus (development map).

**Headers** `gas_distribution` / `fluid_distribution` stay source-relative (an 8-pipe manifold at
the outlet) but every candidate core and inlet route is checked against unbuilt external plans
(`block_sites.planned_tiles`: board blocks, the planned bus, `board.unboarded_plans`); every
rejected candidate is a failure row and the final failure carries `plan_needs` kind `site`
(`block_sites.header_needs`, blockers named).

**In-place growth** (`block_sites.extended`): a verified `advanced_circuit_production` /
`chemical_science` block whose requested count (`rate_per_minute` -> `count_for_rate`, or
`science_growth {pack: chemical-science-pack, machines}` -> `ChemicalScience.grow`) exceeds its
built count regenerates its core at the same anchor, requires the old core to be a subset,
re-audits, preflights the added entities, republishes the board plan and builds (phase `build`
again until the proof). `block_growth` reports `in_place` with the action; the `blocks` growth
provider proposes it; `pack_growth.PACK_STATES` has blue (`PACK_CONTROLLERS` routes the
`science_growth` handler and dry run). Declared tap demand stays at the first rate (a built
planned tap is not republished).

**Engine units for blue on a planned world**: `ChemicalScience` reads the verified
`production-row-engine-unit__belt.json` belt row when there is no fixed engine block.

## Known limits

* Ordering: on a planned world `chemical_science` (roadmap) reads engine units from the tail's
  `rocket_produce_engine_unit_belt`; that row no longer waits for the anchor
  (`rocket_plan.TAIL_ANCHOR_INPUTS`). It exists only because the 2.1.20 rocket bill needs engines
  on a belt; a bill without that need would leave blue without an engine belt on a planned world.
* None of these has a native deployment proof yet (see `ADVANCED_PETROLEUM.md`); provable only
  live: typed pipe survey/router, handoff declare/bind, isolation, real taps and throughput.
