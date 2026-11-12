# Registered fluid infrastructure recovery

`InfrastructureRepair(game,builder,catalog,*,petroleum=None,gas=None)` exposes
read-only `scan(obs)`, read-only `prepare(obs)` and continuous `step(obs)`.
Optional producer objects have their in-memory state refreshed after publication.
`step` returns a paid material/construction action or `None`; diagnostics are
available through `last_diagnostic`. A blocked repair does not monopolize Watch
or stop normal gun maintenance. Watch invokes it after perimeter arming and
before routine factory supply. The Petroleum/Gas/Plastic/Sulfur/Red/Blue job
chain also invokes it after base upkeep, before consumer work. Healthy and
rejected surveys retry after1800 ticks; material waits retry after180 ticks.

The initial owner whitelist is `basic-petroleum.json` / `basic_petroleum` and
`gas-distribution.json` / `gas_distribution`. Only exact bound ordinary `pipe`
losses are eligible, at most eight in one transaction. A live replacement with
an unproven unit number is rejected. Published ports, upstream source identities,
producer recipes, plans, future reservations and registry aliases stay fixed.
Pipe templates may retain historical directions; ordinary nondirectional pipes
are built with native direction zero. Pipe-to-ground directions are still exact.

Preparation reads all registered fluid nodes, both recipe-compressed refinery
storages and all bound downstream handoffs. Existing reciprocal edges, force,
surface, quality, typed fluids and pipeline extents must match. Ordinary whole
resource/terrain/collision preflight validates every missing footprint. Another
production reservation blocks replacement. Foreign exact unit witnesses,
including other controllers' binding maps, block rather than being retargeted.
The only accepted extra PCB annotation is `defense_scope_id` exactly matching
that producer's registered scope; all original plan fields must still agree.

The native action is `repair_registered_infrastructure`, handled by
`execute_repair_registered_infrastructure(game,action)` from the same module.
It accepts only an immutable prepared transaction from
`infrastructure-repair.json`; callers cannot supply arbitrary coordinates.
The exact world, catalog, actor, owner revisions, whole PCB revision and fluid
registry revision are checked again. Every target must still be absent. All
sites and foreign fluid neighbours are checked before the first item is spent.
All required plain normal pipe items must already be in MAIN. Actor inventory
totals from an aggregate observation are not accepted as payment evidence.

One native query records intent at `d.infrastructure_repairs[transaction_id]`,
normally debits one pipe per replacement and creates each exact registered
pipe. It records old/new unit IDs and paid prefix immediately. Whole reciprocal
fluid graph and per-request material conservation are verified before a complete
receipt is issued. No fluid, health, force, research, resource or enemy setters
are used. Complete receipt replay cannot pay or build twice; missing or altered
new units cannot be adopted through the old marker.

Any failure after effects may have begun returns
`infrastructure_repair_outcome_unknown`, preserving `original_reason`, actual
`paid_pipes` and available replacement prefix. Transport exceptions or malformed
successful receipts use the same classification. Root's action runner must keep
the durable action intent for that reason. An incomplete native marker never
silently replays. Ordinary zero-effect preflight failures remain ordinary
blocked results. Partial/unknown operations require explicit evidence review.

After complete native proof, the controller saves a publication intent before
updating either owner file. Each file uses exact before/after SHA256 comparison;
only the relevant binding values and append-only
`infrastructure_repair_history` change. A restart accepts either exact revision,
so an interruption between the two files neither duplicates history nor
overwrites unrelated progress. PCB plans and `fluid-sources.json` are never
rewritten. Other controllers' native witnesses are never silently rebased.

Connectivity proof does not claim an already delivered production rate. Normal
engine fluid movement and the existing producer/consumer proofs must establish
gas flow and actual sulfur output after replacement.

Read-only native evidence at tick **4719458**, in **3.047 seconds**, found six
eligible gaps: crude pipe6932 `(137.5,-52.5)`; gas pipes6950/6951
`(137.5,-61.5)` / `(138.5,-61.5)`; manifold pipes7224/7229/7230
`(156.5,-61.5)` / `(155.5,-62.5)` / `(155.5,-63.5)`. The remaining graph had354
reciprocal edges. All six replacement footprints passed whole preflight.
Actor plain pipe stock was zero, so normal funding must precede deployment.
The full preview is in the local diagnostic
`runtime/v2/dev-101/diagnostics/infrastructure-repair-prepare-part17.json`.
Integrated read-only preparation at4755546 confirmed the same six gaps and354
reciprocal edges. Focused module, dispatch, durable intent and job checks:
60 passed, including14 infrastructure tests. Historical job receipts are excluded
from live preservation pins; actual foreign controller bindings still block.

Luna's automatic Watch-u execution used two actions: pooled normal pipe pickup
and one typed repair. Native4766812 paid six pipes (MAIN6→0), created16915–16920
for the six exact losses and proved364 reciprocal edges. Both owner documents
published once through the transaction CAS and reached verified. Original
ports6963/7228/7231, PCB plans and source aliases were preserved. Gas delivery and
Sulfur commissioning remain independently checked consumer gates.
