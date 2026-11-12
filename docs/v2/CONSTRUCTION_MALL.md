# Capped construction mall

`ConstructionMall(game, builder, catalog, perimeter, *, bus_factory=None,
science=None, green=None, smelting=None)` currently deploys one product per job:
`transport-belt` (stock target200) or `gun-turret` (target8). Red ammunition uses
the separately commissioned Piercing factory. Other existing pure recipe
templates are not advertised as deployable cells.

Public methods are `prepare(obs, {"product": item})` (read-only), `step` and
`proof` with the same parameters, `upkeep(obs)`, `has_stock_cell(item)`,
`commissioned_items`, and `ensure_stock(obs, item, count)`. `count` is the total
desired actor inventory, not a missing-item count. The provider returns `None`
for an uncommissioned product, a guarded `take_mall_stock` action, or an explicit
waiting/blocked receipt. It never supplies raw inputs or handcrafts finished mall
stock. ROOT owns CLI, runtime handlers and bootstrap provider integration.

## Inputs and layout

Every cell normally assembles its own gears and takes iron/copper from new native
SURPLUS main-bus splitters downstream of existing critical consumers. Raw headers
flow EAST, gears move by ordinary direct insertion, output goes to one fixed
chest, and a second final-machine position plus output access remain reserved to
the EAST. Both north and south inserters have local power. Accepted source ports,
old machine units and historical bus interfaces stay fixed.

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

The chest stops at200 belts or8 turrets. Its native bar is a backup capacity
limit (200 belts or one50-turret stack), not a claim that the gun chest physically
holds only8 slots worth of items. Single-item output hands participate in the
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

Pinned catalog AM1 potential: belt cell120/min, costing180 iron/min; turret
cell3.75/min, costing150 iron +37.5 copper/min. A complete200-belt/8-turret fill
costs620 iron +80 copper. Both cells together could request330 iron/min, while
the current375 iron/min supply already has a466/min committed peak. Therefore
the output is capped surplus stock with **zero guaranteed spare rate**, not a
claim that all consumers can run at full speed. No splitter adds source capacity.

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
