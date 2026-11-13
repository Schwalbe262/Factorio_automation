# Registered simple-asset recovery

`RegisteredAssetRepair(game, builder, catalog, *, chemical=None)` repairs exact
registered internal losses. Its first code-owned adapter is
`chemical:plastic-bar`: the verified nested Plastic entry in
`chemical-production.json`, matching PCB block `chemical_plastic-bar`.
Selectors, filenames and coordinates cannot be supplied by an action.

`prepare(obs, owner='chemical:plastic-bar')` is read-only. `step(obs)` returns an
ordinary funding action, one guarded repair action, or `None`; a bounded
`last_diagnostic` explains waiting or failure. `proof(obs)` returns the fresh
intact producer infrastructure evidence, or `None`. The optional Chemical
controller is refreshed after publication. The source controller still owns its
complete product, source and defense proofs.

The runtime hook is:

```python
execute_repair_registered_assets(game, action)
```

Its action contains `type='repair_registered_assets'`, `owner`,
`transaction_id`, `expected_world_id`, `expected_actor_unit`,
`expected_catalog_fingerprint`, and an optional reason. The transaction hash
selects an immutable intent in `registered-asset-repair.json`.

Only one unfinished batch is allowed. A batch restores at most eight exact
ordinary pipes, transport belts or small poles. All known producer entities,
recipes, directions, underground partners, typed belt contents, reciprocal gas
edges, compressed runtime fluid-box mappings, unchanged source/handoff units and
preserved foreign pipes are checked. Published ports and the first bound fluid
neighbor cannot be replaced. Foreign current unit references, new resource
overlaps, foreign reservations, unexpected entities and new fluid neighbors
block the batch. Normal pipes and small poles ignore their physically irrelevant
saved orientation; their saved plans remain unchanged.

All normal MAIN materials and all sites are checked before any cost. One native
query records a version-2 BEFORE witness before its first debit: actual material
counts, normal floor stacks, complete typed belt-line goods, native copper
edges, fluid contents and machine counters. Each normal debit and native build
immediately records its paid prefix and exact old/new unit identity. Ghosts and
corpses are preserved. Floor-to-new-belt pickup is allowed only with unchanged
combined floor/line counts and plain normal metadata. No items, power or health
are granted.

New poles must connect by native copper wires to the unchanged known energized
network; pre-existing wires must remain. A successful structural build records
its paid receipt first. Publication waits for a strictly later game tick and
fresh positive machine/inserter energy, ordinary production history and actual
petroleum gas. Empty charge buffers cause waiting, never another build or
payment. If the game is paused, this wait remains pending.

Publication is an idempotent whole-document compare-and-swap. It updates only
the authorized nested binding keys and appends `asset_repair_history`, preserving
Sulfur, plans, PCB, fluid registry and published outlet identities. A durable
publication intent permits retry after either atomic file rename. Concurrent
owner, board, source-registry or actor changes fail closed.

Any failed operation after a native marker/effect has begun returns
`infrastructure_repair_outcome_unknown`, retaining its original reason and paid
prefix. It must remain a durable uncertain intent for explicit review; it cannot
adopt an existing replacement or replay an incomplete paid batch.

Validation: 15 executable flattened-Lua/unit tests cover mixed payment, exact
identity, conservation, native peer/power failures, interrupted publication,
strict receipts, and post-tick charging. Read-only native preview at tick
5104570 passed in 5.600 seconds including observation: four Plastic losses
7239/7251/7260/7566, cost one pipe, two belts and one small pole. Known power
anchor 7564 remained on network 2. This preview performed no repair or source
publication; live success requires the guarded action and subsequent proof.
