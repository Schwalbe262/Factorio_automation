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

The runtime intercepts two guarded actions from this module:

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
