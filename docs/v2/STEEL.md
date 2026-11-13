# Automatic steel preparation

## Fresh maps: site-planned furnace block (`planned_furnaces.py`)

On a world with a site service and a published planned bus,
`IndustrialOperations.steel_supply`/`steel_production` and
`SharedCoalTap` (`steel_coal_supply`) delegate to `PlannedFurnaces(kind="steel")`;
`brick_production` delegates to `kind="brick"`. The block is the smelting
district's paired bank (`smelting.paired_furnace_bank`, `product=` argument)
with 6 reserved slots per row (30 x 12 tiles), 1 built per row (2 furnaces,
7.5 steel/min nameplate for 37.5 iron/min). Site: one `SiteService` district
block (`returns_to_bus=True`), turned by `site.fit.orientations` so iron faces
the tap band, coal faces the dedicated `coal` outlet and steel leaves on the
bus-edge side. Iron: a planned-bus tap planned with the block
(`planned_tap.connect_inputs`); coal: a route from `material-sources.json`
`coal` (bricks: `stone` and `brick-coal`), recorded in board `connections`.
Growth tiles are hard for every route and the power bridge. The whole plan is
published on board block `steel_production` (`brick_production`) and the tap in
the bus state before anything is built.

Phases: `steel_supply` = plan, tap cut-over, iron route, proof iron at the raw
port; `steel_production` (needs the steel-plate recipe) = whole plan, proof:
every built furnace finished steel with pure raw/coal/fuel (idle furnaces get
the native witness), steel on the product port, one item per feed lane
(`smelting.native_feed_lanes`); `steel_coal_supply` = proof that the registered
coal outlet (same unit) feeds the block's coal port, no shared header. The
steel port is a free board output port, so `produce` rows can take it
(the engine row does, `planned_products.py`); their route connection declares
`demand_per_minute`. Growth: the observe receipt `furnace_block_claims`
(`planned_furnaces.furnace_block_claims`: built and full nameplate, declared
port demand) feeds `progression.steel_capacity_nodes` (the rule of
`smelting_capacity_nodes`: claims >= 0.8 x built -> target claims x 1.25,
capped by the reserved slots); `steel_capacity` on a planned world
(`SteelCapacity.step` -> `PlannedFurnaces.capacity_step`) fills reserved slots
in place with `grown`, republishes the plan, builds and proves every furnace
(`capacity_proof`). `growth_evidence` (busy furnaces) is not used by a planner.
The sections below are the development-map path (no planned bus).

`steel_production.py` provides `steel_rates(catalog)`, `steel_core(catalog,x,y)`,
`audit_steel_core(plan,catalog)` and `SteelProduction(game,builder,catalog,
iron_source=...,coal_source=...)` with `prepare(obs)`, `step(obs)` and `proof(obs)`.
No live steel site has been surveyed or built by this change.

The default core has two opposing stone furnaces, four powered ordinary
inserters, two poles,33belts and one input splitter. Iron and coal side-load
opposite sides of a straight belt before the splitter distributes both lanes
to both furnace rows. Eastbound mixed inputs declare iron LEFT/north and coal
RIGHT/south. Independent upper/lower output inserters fill opposite steel
lanes; a final output splitter is unnecessary. Furnaces select steel normally
from belt-fed iron after native steel-processing research. No actor fuel,
iron/ore insertion or handcrafted steel is emitted.

With the saved 2.1.20 catalog, 5 iron plates become 1 steel plate in 16 recipe seconds; stone
furnace speed1. Two furnaces rate7.5steel/min and require37.5iron/min. Exported
maximum furnace usage1500J/game tick and coal4MJ give2.7coal/min at maximum
catalog usage. This is a design calculation; actual fuel and sustained steel
throughput need native measurement. Inserter electricity is supplied separately.

At the example anchor(82,-40), fixed typed ports are iron(78.5,-44.5)S,
coal(77.5,-40.5)N and steel(92.5,-37.5)E. Three furnace pairs are reserved;
only one pair is initially built. Activating two or three pairs preserves every
existing entity, port and reservation, giving rated15/22.5steel per minute.
Candidate anchors(82,-40),(94,-40),(82,-24) are suggestions pending native
land/resource/reservation, transport and power preflight.

Preparation requires an **already installed** iron output with item
`iron-plate`, direction`output`, facing, position, exact`unit_number`,
priority`surplus`, and `priority_splitter={position,unit_number}`. Native checks
require that NORTH splitter's input/output priorities both be`left`, preserving
the continuing science trunk before steel. Coal requires a separate pure typed
`coal` output with facing, position and exact unit. Existing source contents
must match their types. Missing handoffs return explicit unsupported reasons
before construction; this controller never edits or recovers a running bus.
The parent must install and validate any new bus/coal branch first.

`prepare` reads only. It audits the core, surveys the entire growth reservation,
plans complete bounded source and power adapters, and runs whole native
preflight. It passes no ignored units and authorizes no existing-asset recovery.
Only a later explicit `step` saves steel state, publishes its PCB reservation
and emits ordinary paid construction actions. Proof requires native recipes and
production in every furnace, coal/burning fuel, powered inserters, unchanged
source identities and surplus priority, exact underground partners, correctly
typed mixed input lanes and real steel on both native output lanes.

If an automatic furnace releases its recipe while idle with full output,
acceptance additionally requires positive expected output and pure compatible
input/fuel. The exact native unit's normal output, raw input and fuel slots are
checked in the same query as lane evidence. An unset recipe alone is never
production evidence; wrong recipes, products, qualities and mixed raw items fail.

Tests cover catalog-driven recipe/fuel calculations, footprint/feed/output
audits, stable expansion, missing/unsafe handoffs, read-only preparation and
commissioning gates. No sustained-rate, capacity-upgrade or rocket acceptance
is claimed. Per-agent token/quota usage is unavailable; the root records meters.

For a commissioned downstream consumer, `proof(obs, consumer_furnace_units=...)`
can validate the exact previously pinned furnaces while their output belts have
been drained. It still requires the verified producer, matching furnace units,
actual products, normal fuel/iron inputs, pure output lanes, power and source
contracts. Ordinary commissioning still needs steel on both physical lanes.
The returned `output_buffers_present` reports the current distinction; this
consumer proof makes no buffered-stock or sustained-rate claim.
