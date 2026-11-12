# Capped construction mall

`ConstructionMall(game, builder, catalog, perimeter, *, bus_factory=None,
science=None, green=None, smelting=None)` deploys one product per job:
`transport-belt` (stock target200), `gun-turret` (8), `underground-belt` (50),
`splitter` (20), `assembling-machine-1` (20) or `inserter` (50). Red ammunition
uses the separate Piercing factory. Poles/walls remain unsupported mall products.

Public methods are `prepare(obs, {"product": item})` (read-only), `step` and
`proof` with the same parameters, `upkeep(obs)`, `has_stock_cell(item)`,
`commissioned_items`, `pending_items` and `ensure_stock(obs, item, count)`. `count` is the total
desired actor inventory, not a missing-item count. The provider returns `None`
for an uncommissioned product, a guarded `take_mall_stock` action, or an explicit
waiting/blocked receipt. It never supplies raw inputs or handcrafts finished mall
stock. ROOT owns CLI, runtime handlers and bootstrap provider integration.
Only one unfinished cell is accepted at a time; automatic commissioning queues
must finish `pending_items` first and prepare the next product against fresh
bus/PCB state. Read-only previews do not create pending cells.

## Inputs and layout

Every cell normally assembles its own gears and takes iron/copper from new native
SURPLUS main-bus splitters downstream of existing critical consumers. Raw headers
flow EAST, gears move by ordinary direct insertion, output goes to one fixed
chest, and a second final-machine position plus output access remain reserved to
the EAST. Both north and south inserters have local power. Accepted source ports,
old machine units and historical bus interfaces stay fixed.

The four added products use `mall_cells.py`, a deterministic compiler of the
installed recipe DAG. One final assembler has one or two direct-insertion
branches, each at most two local machines deep. Gears/belts occupy the west
branch; circuits/cable occupy the east branch when required. All intermediates
are normally assembled locally from iron/copper plates. There is no circuit or
cable main bus and no actor ingredient insertion. Raw iron flows east on the
north header; copper flows east then turns north locally. Every arm is a normal
fast inserter, with complete ingredient/endpoint/lane connectivity audits.
The fixed output chest is south of the final assembler, with two empty service
tiles; growth space is reserved east. Both ordinary native arm endpoints and
held-item types are checked after construction.

Finite station trials first require an empty outlet and first outward cell;
otherwise an existing consumer belt could silently receive the new material.
Up to48 NORTH tiles are considered in12-tile increments. A successful extension
adds normal paid belts on existing active columns, preserves all old entities,
tunnels and port descriptors, and adds named frontier aliases. Rejected trials
create no saved aliases. Ordinary later BusTap compilation retains historical
aliases only on unchanged owned straight trunk belts.

Before any paid bus work, preparation compiles the final bus, cell, all input
routes, power and future space, then runs whole native resource/terrain and
defense preflight. Site selection uses a coherent16-tile grid, at most64 batched
land candidates and8 whole-route attempts under the existing shared planning
deadline and unchanged turn/detour gates. These are bounded planning stages,
not a guarantee that coverage setup and every call finish within20 seconds.

An accepted complete factory plan is registered before construction so defense
placement protects its core and external feeds. Extension is add-only. Each new
tap is freshly prepared against the actually updated bus; its geometry must
match the whole preview. Only the exact old corner is recovered after normally
stocking its replacement. All authoritative bus copies use the existing
resumable publisher. No source units are invented for prospective outlets.

## Native quantity and production proof

The complete paid core, including the destination belt shared with its future
route, is built before external raw feeds. Native circuit wires, chest readback,
machine/inserter stop conditions and output-hand signals are configured and
read back before zero product-counter baselines are recorded. External feeds
are connected last. Every local machine must actually advance its counter and
normal finished output must appear in the owned chest before commissioning.

Each chest stops at its product target above. Its native bar is a backup capacity
limit, not a claim that a20-item target physically reduces a50-item stack.
Single-item output hands participate in the
stock signal. Machine output buffers and hands remain separately reported WIP;
the target is chest stock, not total goods everywhere in the cell. Historical
production witnesses permit normal later pickup without falsely requiring a
permanently nonempty output chest.

`mall_stock_limit` calls `execute_mall_stock_limit`; `take_mall_stock` calls
`execute_take_mall_stock`. Both verify the known producer file, pinned catalog,
PCB plan, world, actor and exact paid units. Pickup transfers at most50 plain
normal items from declared exact slots. Destination insertion precedes decrement
of that same source slot, and only the actual inserted count is debited. Partial
capacity preserves all other goods. Throws or unexpected receipts return
`mall_stock_transfer_outcome_unknown`; they must not be automatically replayed.
Character backend reach remains mandatory.

## Capacity and current validation

Design potential is capped by native recipe speed and conservative normal
fast-inserter budgets of120 items/minute per arm:

| Product | AM1 count | Potential items/min | Chest target | Iron/min | Copper/min |
| --- | ---: | ---: | ---: | ---: | ---: |
| Belt, legacy | 2 | 120 | 200 | 180 | 0 |
| Turret, legacy | 2 | 3.75 | 8 | 150 | 37.5 |
| Underground belt | 3 | 24 | 50 | 210 | 0 |
| Splitter | 5 | 8 | 20 | 128 | 60 |
| Assembling machine1 | 4 | 12 | 20 | 264 | 54 |
| Inserter | 4 | 40 | 50 | 160 | 60 |

The underground cell's machines alone could produce48/minute; its final iron
input arm lowers the design potential to24. These are not sustained bus rates.
`declared_bus_demand` includes append-only consumer capacity allocations without
changing historical port descriptors. Every cell reports the actual declared
deficit and **zero guaranteed spare rate**; surplus priority and finite stock
limits constrain production when critical consumers need the plates.

Minimum recipe batches for a new target fill require439 iron for50 underground
belts;320 iron/150 copper for20 splitters;440 iron/90 copper for20 AM1s; and200
iron/75 copper for50 inserters. Underground ingredients need63 belt cycles,
leaving one intermediate belt. Floating per-product arithmetic would understate
that fill by1.5 iron. These are empty-cell minimum batches, excluding building
costs and actual input/output/hand WIP, which native proof reports separately.

Read-only whole prepares passed at5000074/5005185/5006182/5007171 for the four new
products in13.047/17.719/17.625/18.109 seconds respectively. They passed native
bus extension/taps, complete terrain/resource/future footprints, power and
defense previews. All selected32.5,-175.5 from the same baseline: they are
alternative previews, not four simultaneous reservations. Commission one,
then freshly prepare the next. Diagnostic files are
`runtime/v2/dev-101/diagnostics/mall-<product>-prepare-part18.json`.
No new-cell gameplay or commissioning is claimed by these previews. The old
commissioned belt cell and both legacy canonical layouts remain unchanged.

Read-only native belt preparation passed at observation4291072, power4291795:
core(32.5,-159.5), reserve[[26,-166],[46,-153]],58 entities (2AM,4fast arms,
41 belts,1chest,10poles), two additional paid defense guns/20 yellow magazines.
Prospective NORTH frontier-142.5 adds48 normal belts across two active trunks;
the surplus tap at-140.5 has fixed EAST outlet(12.5,-141.5). Native whole
resource/terrain, power and coverage planning passed, with1.9486MW headroom after
the385.2kW mall load and720kW reserve. This is prospective evidence; no mall
gameplay construction or native production has been executed by this author.

The first live partial extension exposed a phase-preflight error after32 paid
frontier belts: the final future splitter occupied the same tile as its newly
built, not-yet-recovered straight corner. The original preview could not know
that future belt's unit ID. Execution now pins matching paid extension units;
only an exact planned future tap corner may be virtually ignored in final-plan
preflight. Ordinary BusTap still stocks and normally mines that exact corner.
Changed unit IDs and unrelated belts remain protected. Read-only shadow-checkpoint
continuation at4360018 emitted the remaining16 copper-frontier builds in2.265s;
all original runtime-file SHA hashes stayed unchanged and no action was executed.

Focused validation:23 mall tests (including executable Lua caps, counters,
metadata rejection and conserved/partial stock pickup),5 frontier/station tests,
and the existing15 BusTap tests pass. ROOT runs the canonical integration gate.

Final indexed integration gate:1078 total tests,1076 passed and two ignored-asset
skips in58.386 seconds. The live same-key job performed30 normal actions,
commissioned its surplus tap and preserved history after a reviewed partial
frontier correction, then stopped on actual insufficient generation. Native
inspection found original coal drill956 exhausted, old Boiler167 unfueled and
only the other1.8MW steam pair available. Actual Mall output and construction
stock collection remain unproved; the headroom requirement was retained.
