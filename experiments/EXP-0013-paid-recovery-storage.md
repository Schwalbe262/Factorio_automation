# EXP-0013: normally paid storage restores inventory headroom

- Date: 2026-11-12 KST; same default development world and original bus job.
- Goal: recover from a full actor inventory without deleting items, granting
  storage/materials, or feeding ores into production machines.
- Hypothesis: four spare inventory slots plus dedicated paid recovery storage
  prevent ordinary mining/corridor recovery from repeatedly failing.
- Options: existing automation chests are not adopted. Survey clear land
  outside registered factory/bus reservations and use identity-bound dedicated
  storage. Ammo, science tools and metadata-bearing stacks remain excluded.

## Failure and implementation

The original bus key blocked at1561835 after130actions: all80actor slots were
occupied, wood298 left capacity2, and the next tree produced more. Held recovery
goods included624belts,371ironore,513copperore,1594ironplates,2063copperplates and
757green packs. This is an executor inventory failure, not a model diagnosis win.

Chest crafting itself needs headroom. The conservative slot calculation consumes
a full ingredient stack before claiming an output slot, regardless of which
stack the engine removes first. Here13iron chests consume104held ironplates,
freeing enough space for the outputs. Recipes must be natively enabled and
character-craftable. No synthetic inventory expansion is used.

Native deposits recheck exact world/unit/chest position/force and actual normal
plain source slots. Full-health, nonspoilable stacks without additional item
data are inserted only up to the destination's actual accepted count, then
removed from those exact source slots. Unknown transport/native outcomes keep
durable intent and require reconciliation, never automatic replay.

The main observation now includes empty inventory slots in its existing query.
At four or more free slots, upkeep adds no inventory RPC. Low-space snapshots
and deposits are performed only when needed. Regular eligible construction
stock remains available to the existing collector from the storage chest.

## Native result

Luna retried the ORIGINAL known-blocked key with its history intact. The engine
accepted13iron-chest crafting cycles; one paid chest was built and bound as
unit4234 at(1.5,-54.5). A guarded normal deposit moved371/371ironore into it.
Subsequent tree clearance and bus construction resumed; the observed job reached
181actions/zero consecutive failures at1604597. No parent game mutation occurred.

This resolves the demonstrated inventory block. It does not complete the
shared-bus migration, continuous science gate or rocket goal. Remaining work:
finish the same worker's build/commission/retirement, inspect actual compact
production and lab supply, then record the result in EXP-0012.

Validation:203v2tests pass, including11storage-planning tests,13guarded-transfer
tests and durable uncertain-deposit/retry rejection.53related legacy game/builder
tests pass. Actual wood stack health/spoil_tick/item fields and native recipe/site
preflight were checked read-only before execution. Part7 usage is sampled in
note.md, including cached parent input; exact subtask/child/weekly shares are not
exposed.
