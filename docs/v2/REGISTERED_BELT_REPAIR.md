# Registered Advanced Circuit belt recovery

`RegisteredBeltRepair(game,builder,catalog,*,advanced=None)` exposes read-only
`prepare(obs)`, continuous `step(obs)` and `last_diagnostic`. The Watch hook
receives an ordinary funding/build action or `None`; diagnostics do not stop
gun upkeep. The optional Advanced Circuit object is refreshed after publication.

The first owner whitelist contains only the verified `advanced-circuits.json`
and its exact `advanced_circuit_production` PCB plan. At most eight missing
ordinary internal transport belts are eligible. All other556-plan identities,
directions, recipes and normal quality must match; pole orientation alone is
ignored. Published ports and other controllers' current unit witnesses are
protected. Historical job receipts are not current ownership pins.

The controller derives typed external routes from the saved three source-to-core
corridors and combines them with the existing core audit. It checks actual belt
contents and all declared native underground partners. Every restored belt must
have the exact expected native inputs and outputs. Ordinary funding uses
`builder.bootstrap.ensure_item`, including its installed Construction Mall
provider. Six real belt items must be held before the six-site batch starts.

Shared preflight normally rejects spilled item entities. This controller keeps
that shared behavior unchanged: its narrow replacement-only preflight retains
the same resource-policy specifications and mining reservations, native terrain
and placement checks, and foreign production keepouts. It permits only normal
plain correctly typed item spills and inert corpses at exact proven lost
footprints. It never explicitly picks up, deletes or normalizes goods. Ordinary
native belt placement can pick spilled items onto its new transport lines.
Version2 therefore saves MAIN material counts and complete floor/transport
counts before any payment, then proves combined per-item floor-plus-line counts
are conserved in the same game tick. Surviving ground stacks retain quality,
metadata and position; corpse object/name/position identity remains exact.
Native creation explicitly sets `preserve_ghosts_and_corpses=true`, whose normal
API behavior retains existing remnants instead of the default clearing behavior.
See the official [LuaSurface creation parameters](https://lua-api.factorio.com/latest/classes/LuaSurface.html#create_entity).
Other obstructions and new foreign belt sidefeeds fail before payment.

Root's runtime hook is action `repair_registered_belts` handled by
`execute_repair_registered_belts(game,action)` in this module. The envelope names
only a persisted transaction, exact world/catalog/actor and no caller positions.
Native intent at `d.registered_belt_repairs[transaction_id]` precedes all effects.
One query normally pays and builds every site in the same game tick, recording
each old/new unit and paid prefix. Complete replay never charges twice.
Incomplete markers, post-effect failures, transport errors and malformed
receipts return `infrastructure_repair_outcome_unknown`; Root's existing runner
retains the durable action intent for explicit reconciliation.

`registered-belt-repair.json` stores transactions and publication intent. Only
the owner's binding values and append-only `registered_belt_repair_history`
change, after exact paid receipt and native edge proof. Owner publication uses
before/after SHA256 checks so a restart after rename cannot duplicate history.
An unrelated owner/PCB revision blocks. Original plans, machine geometry, bus
ports, source aliases and all previous history remain unchanged. Partial goods
are never used as an ammunition or material grant.

Read-only native prepare **4844139** passed in **5.484 seconds** for six northbound
belts at x182.5: old7764/y-40.5,7782/y-55.5,7786/y-59.5,7793/y-66.5,
7821/y-92.5 and7835/y-105.5. It found25 plain normal spill stacks; normal belt
inventory was zero, so funding must precede deployment. The surveyed loss bounds
plus32 tiles contained no current hostiles; no owned asset was currently damaged.
These are snapshot findings, not proof that an attack cannot recur. Full local
preview: `runtime/v2/dev-101/diagnostics/registered-belt-repair-prepare-part18.json`.
No repair action, owner publication or combat action was executed by this task.

The first deployed version paid six belts and created units17010..17015, then
returned `registered_belt_spilled_goods_changed` because native placement moved
ground items onto the belts. The original marker did not persist MAIN/floor
baselines. Read-only4935622 proved the exact new units, full typed/native graph
and all underground partners. An earlier current snapshot4903139 contained zero
floor items,25 normal plastic bars on the new belts and all six remnants.
Those observations cannot reconstruct unknown historical counts.

Legacy recovery is an explicit reviewed continuation:

```python
proposal = repair.prepare_reconciliation(obs, job_key, transaction_id)
# Root reviews the proposal; under writer.lock, metadata only:
repair.activate_reconciliation(proposal['proposal'])
```

`approved_reconciliation(root,job_key,transaction_id)` is a read-only prerequisite
for Root's frontend unknown-action clear. It requires an accepted immutable
epoch and exact historical intent/failure hashes; it does not require the future
native reconciliation action to have already completed. This avoids circular
approval. `step` then emits `reconcile_registered_belts`, handled by
`execute_reconcile_registered_belts`. That action only appends a metadata receipt
under the old marker's `reconciliations[epoch_id]`. It does not rebuild, repay,
move items or rewrite the original marker/failure. It rechecks exact units,
original marker projection, current typed graph and known historical evidence.
Only then can owner binding CAS proceed.

Legacy receipt mode is `legacy_paid_belt_reconciliation_v1`; both historical
ground and material conservation remain explicitly `unknown`, with both
baseline-available flags false. No before/after inventory counts are invented.
The ledger ends in `reconciled_legacy`, and the separate owner repair history
retains that label. Future version2 operations have actual durable baselines
and cannot use this missing-baseline legacy exception. The read-only proposal
packet is `diagnostics/registered-belt-reconciliation-proposal-part18.json`.

Root accepted that exact epoch under the writer lock without gameplay effects.
Luna's new typed action4953084 appended the native reconciliation receipt and
published the six existing paid unit bindings through owner CAS. Watch-y retained
two total actions and the original failed outcome; it then paused4956800.
No belts were created or paid twice. Historical conservation remains unknown.
Integrated focused tests:65 passed, including14 belt-repair cases and frontend
denial without a reviewed epoch.
