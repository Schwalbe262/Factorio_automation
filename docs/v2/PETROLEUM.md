# Basic petroleum

`BasicPetroleum(game, builder, catalog, perimeter)` provides `prepare(obs)`,
`step(obs)` and `proof(obs)`. `prepare` is read-only. `step` persists a world/catalog
checkpoint in `basic-petroleum.json`, reserves the accepted district, commissions
normal armed perimeter coverage, and delegates paid construction to the existing
builder. The caller executes returned ordinary actions. There are no fluid inserts,
research edits, inventory grants or artificial production counter changes.

The controller requires naturally completed `oil-processing`, enabled refinery and
basic processing recipes, and the exact proven `crude-oil` pipe in
`fluid-sources.json`. It preserves that pipe, its upstream well and its power. A
missing/replaced bound asset blocks the operation rather than silently rebuilding
it. After native production proof, the registry gains an exact typed
`petroleum-gas` output; existing fluid entries remain intact.

`refinery_core(catalog, x, y, count=1)` builds one to three refineries at fixed
eight-tile spacing. The crude and gas headers, external ports and 30×12 reservation
stay fixed through this structural expansion. The controller initially installs
one refinery; adding further machines is not exposed as a live expansion action.
`basic_petroleum_rates` derives recipe quantities, time, crafting speed and electric
load from the installed catalog. On the inspected 2.1.20 catalog this is 100 crude
to 45 petroleum per five seconds, or **1200 crude/min, 540 petroleum/min and 420 kW**
nameplate. Crude extraction availability and measured output rate are separate;
commissioning does not certify those rates. The actual basic recipe needs no water.

Recipe fluid indices count inputs and outputs separately. Consequently crude input
ordinal 2 is physical fluid box 2, while petroleum output ordinal 3 is physical
fluid box 5. The core derives the connection offset from the installed rotated
prototype positions and rejects unsupported geometry. This distinction follows
the official [fluid product definition](https://lua-api.factorio.com/latest/types/FluidProductPrototype.html)
and [fluid ingredient definition](https://lua-api.factorio.com/latest/types/FluidIngredientPrototype.html).

Runtime storage indices differ from those static prototype indices. Actual
2.1.20 refinery6967 with the basic recipe exposed only storage1 (prototype2,
crude) and storage2 (prototype5, gas); querying storage3 was out of bounds.
Proof now enumerates bounded active storages with `get_fluid_box_prototype`,
maps their prototype identities to runtime indices, then checks purity and both
directions of every connection using those actual indices. Merged prototype
arrays are validated explicitly; incompatible fluid mappings fail. No recipe
or physical port is changed. Native2643020 passed51actual refinery cycles,
100gas at outlet6963 and349reciprocal edges; Luna completion2645174 published
the typed gas source. This is buffer commissioning, not a measured output rate.

Planning tries six district-relative sites with a 20-second budget checked between
native queries. Existing production reservations, mining growth areas, water and
all resource tiles are hard constraints, including tiles under buried pipe spans.
Whole orthogonal corridors retain the shared six-turn and `1.35×Manhattan + 4`
length gates. Underground mouths have straight approaches, clear landing sites and
catalog-bounded spans; nearby existing mouths that could steal a connection cause
rejection. Independent fluid networks cannot touch. Ordinary tree/rock clearance
uses the shared whole-plan preflight budget; no source or foreign hardware is mined.

Pipeline sizing uses the existing source segment's native bounding box plus the
new crude network. The 2.1.20 runtime exposes
`prototypes.utility_constants.default_pipeline_extent == 320`; the inspected source
and proposed central district fit that limit. A larger extent returns
`pipeline_requires_explicit_pump_segment`; this controller does not pretend that a
longer network works without a separately planned powered pump. Runtime methods
are documented on [LuaEntity](https://lua-api.factorio.com/latest/classes/LuaEntity.html).

Proof uses `get_fluid_box_neighbours(index)`, `get_fluid(index)` and
`get_fluid_segment_extent_bounding_box(index)`. Every expected connection must be
reciprocal with the exact physical index and bound unit. Unexpected peers,
contamination, wrong box contents, wrong recipe, absent power, zero product
counters, oversized native segments and an empty petroleum outlet all fail proof.
The original crude outlet may retain its upstream well peers. The refinery core's
existing and new pipes otherwise admit only the declared network.

Read-only native preparation passed at tick **2504240**: refinery center
`(128.5,-57.5)`, crude input `(124.5,-52.5) E`, petroleum output `(150.5,-61.5) E`;
305-tile/two-turn/1.0-detour corridor with two underground pairs, six power hops,
77.789×252 pipeline extent and seven ordinary clearables. The accepted plan needs
344 pipes, four pipe-to-ground entities, seven poles and one refinery, with three
new defended posts/30 magazines. This records a planning snapshot, not construction
or actual petroleum production. Nineteen focused tests include executable native
proof Lua and the actual shared router with hard resource constraints.
