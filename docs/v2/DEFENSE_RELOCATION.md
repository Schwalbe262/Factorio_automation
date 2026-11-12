# Paid engine post relocation

`DefenseRelocation(game, bootstrap, builder, catalog, perimeter, engines)` exposes
read-only `prepare(obs)`, action-producing `step(obs)`, `proof(obs)` and `active`.
The executor assigns `engines.relocation` after construction; its default is
`None`. An active relocation gates the engine builder. Completion returns a
waiting boundary so engine construction uses a new observation.

The operation is limited to at most four conflicting, exact-owned posts created
by the engine coverage scope. It requires the fixed engine core, ports, tap and
source units to stay unchanged and no engine consumer construction to have
started. Unknown posts, changed units and ambiguous assets fail closed. The
controller preserves logical post names and every registered scope referencing
those names.

Every participant in those referenced scopes and every scope revision stays
exact. An unrelated known post may be normally repaired at the same position,
name, scope and access geometry if its lost-unit history includes the original
unit and its replacement is commissioned. New unrelated posts require their own
registered, commissioned coverage scope. Existing native probes/actions bind
those current units and reject foreign or changed live identities. Maintenance
retains at most sixteen lost unit IDs, including the previous singular ID.

Preparation surveys a finite eight-tile neighbourhood for replacement sites.
Candidates must preserve every referencing scope's complete-cell coverage and
native local walk access. All board reservations, empty future machine space,
shared-bus bands, full external plan footprints and fixed-port continuation cells
are protected. Combined replacement-post/current-engine/future-engine native
preflight ignores only the exact old posts scheduled for later recovery. Existing
pipe peer witnesses remain required.

For each post, the controller obtains an ordinary paid turret and full normal
magazines, builds the replacement, and loads ten full magazines through the
existing guarded loader. It verifies all referencing scopes before publishing
the same logical name at the exact replacement unit. Only then may it recover
the old unit. The replacement stays armed throughout old-post recovery.

`defense-relocation.json` records each build, arm, publication and retirement
phase. A publication intent contains old/new descriptor revisions before changing
`defense-perimeter.json`; retry accepts only those two exact descriptors and
preserves all other post/scope metadata. Neither file can silently adopt a
replacement entity. Callers hold the normal writer lock.

The runtime intercepts three guarded actions from this module:

- `build_defense_replacement` calls `execute_build_replacement`. It consumes one
  exact normal plain turret item, performs the ordinary paid entity build and
  atomically records the resulting unit in `d.defense_relocations`. That receipt
  allows a lost response to be retried without paying twice or adopting an
  unrelated post.
- `retire_defense_post` calls `execute_retire_post`. It verifies the paid receipt,
  replacement's ten full normal magazines, all referenced native coverage, exact
  old identity and full normal old ammunition, then calls ordinary
  `a.mine_entity`. It requires unfiltered empty slots for the turret **and** all
  ammunition together, conservatively ignoring possible merges. Native before/
  after item and round totals must conserve the recovered goods. Partial or
  unsupported old ammunition is rejected; no ammunition is recreated manually.
- `reconcile_defense_post_retirement` calls `execute_reconcile_retirement`.
  It changes only the framework receipt for a separately reviewed legacy mining
  failure, using the exact persisted evidence described below.

Retirement totals explicitly count the main inventory and
`defines.inventory.character_ammo`. Native mining may auto-equip recovered
magazines. Inventory enum aliases are never iterated: a character corpse alias
can refer to the main inventory and would count it twice. Existing actor partial
magazines retain their exact rounds. Old turret ammunition must still be full
and normal, and the aggregate main-inventory capacity guard remains unchanged.
Unrelated material conservation aggregates inventory/name/quality counts, so
ordinary automatic sorting and stack merging cannot create false losses. Exact
slot snapshots remain mandatory for reviewed legacy inventory compare-and-swap.

Known unrelated posts keep a permanent first-loss unit and the latest sixteen
lost units. A pending transaction may accept ordinary same-post repair ancestry
through that stable origin after recent history rolls over. Referenced posts and
scope hashes remain exact, and new unrelated scopes must be fully commissioned.
Earlier missing origins require actual ledger review; they are never invented.

Before calling ordinary mining, the native operation persists a version2
`retirement_intent` with exact world/actor/old/new identities, observed actor
totals and old stock. The resulting receipt stores actual before/after totals.
A missing-old retry can finish only that exact intent with its expected item and
round deltas; changed actor inventory or unrelated missing posts fail closed.

The first native recovery at tick3,170,394 removed old7152 and auto-equipped its
ammunition, exposing the earlier main-only check. Its original pre-mine totals
were not persisted. `prepare_legacy_retirement_reconciliation(obs, job_key)` is
read-only and requires one exact paired action-intent/result in the bounded
ledger tail, including the specific post-mining conservation-failure reason and
line SHA256 hashes. It also requires the paid replacement, every referencing
scope, old absence, clear ground and normal current actor stock.

The returned `proposal['intent']` must be explicitly reviewed and copied under
the writer lock into the matching entry's `legacy_retirement_reconciliation`.
Only then does ordinary `step` emit the reconciliation action. Its native helper
rechecks identities, the exact reviewed actor snapshot and coverage, then writes
a receipt only. It never mines again, transfers goods, or invents original
totals/recovered counts. Both `baseline_available` and
`recovered_counts_available` remain false, including in later proof reporting.
An unapproved missing post remains blocked.

Read-only proposal preparation passed at tick3,209,178, observing one normal
turret and ten full normal magazines/100rounds across the two inventories.
This records the observed stock and eligible action evidence, not retrospective
pre/post conservation proof. Actual reconciliation execution is a separate gate.

Native read-only preparation at tick 2,951,770 found replacements
`7152: (135,-95) -> (135,-93)` and `7154: (146,-109) -> (146,-111)`, with combined
whole/future engine preflight and pipe preservation passing. This is prospective
evidence, not native recovery execution. At tick 2,980,541 old post 7152 held ten
normal full magazines with health 1, spoil tick 0 and no extra item metadata.
The actor held neither turret nor magazine stock at the earlier inspection;
normal paid stock and actual ordinary-mining conservation still require live
execution checks.

Later plastic construction introduced a separate belt crossing in the unbuilt
engine feed. The current strict prebuild guard blocks that ambiguity. A producer
feed replan must be accepted before relocation can incur costs; the relocation
then binds that accepted plan and keeps it fixed.
