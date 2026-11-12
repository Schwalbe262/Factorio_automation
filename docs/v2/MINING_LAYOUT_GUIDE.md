# Paired mining layout and capacity contracts

`mining_layout_policy.py` is a read-only planner and diagnostic module. The
typed executor under the sole gameplay writer executes normal collection,
clearance, building and publication.
It never moves an accepted output, removes a drill, inserts resources, changes
productivity or treats a capacity estimate as measured production.

## Mechanics used

An electric drill occupies3×3 tiles and mines a5×5 area. Its output lands on its
physical front tile. This allows two opposing rows to feed the two physical
lanes of one straight collector. [Official electric drill reference](https://wiki.factorio.com/Electric_mining_drill).

Base output is `mining_speed / resource_mining_time`, multiplied by the
deterministic resource yield. Productivity increases output without consuming
additional resource for the bonus items. The installed runtime is authoritative
for bonuses and rates. [Mining formula](https://wiki.factorio.com/Mining),
[mining productivity](https://wiki.factorio.com/Mining_productivity_(research)).

An unstacked yellow belt carries15 items/second across two lanes:450/min per
lane and900/min total. Drills must actually feed opposite lanes; a one-lane bank
cannot claim the whole belt's capacity. [Official belt transport reference](https://wiki.factorio.com/Transport_network).

Native2.1.20 evidence supplied at tick3,840,835 gives mining speed0.5, ore time1,
radius2.49, belt speed0.03125 and force productivity0.1. This means30 base or33
productivity-adjusted items/minute per standard drill. Fourteen drills on each
lane offer924/min; the collector ceiling remains900. Thirteen on each lane
offer858/min without clipping. These are recipe and transport budgets, not
timed throughput measurements.

## Layout chosen before construction

`make_policy(world_id, catalog_fingerprint, resource, ...)` declares material
flow, opposite column growth, perpendicular bank growth, grid origin, four-tile
column pitch and eight-tile bank pitch. The current executable policy is EAST
collector flow, WEST column growth and SOUTH additional banks. The three-tile
pitch is not native-verified here. Quarter-turn cores are pure geometry models;
other execution orientations fail closed until their exit and lane proofs exist.

The four-tile pitch retains the current normally powered paired geometry and
provides repeatable pole/collector space. Candidate scoring counts unique ore
once, requires both rows to have pure, sufficient resource, and favors contiguous
reserved growth. No already accepted drill grid or interface is rotated to fit
the new policy. Execution is bounded to eight paired columns per bank; the
fourteen-drill-per-lane example above is capacity arithmetic, not a claim that
this wrapper has commissioned such a bank.

`prepare_new_bank(game, builder, obs, resource, count, reservations, *, policy=None)`
is compatible with the existing paired planner's call signature. `builder.catalog`
provides the pinned catalog. Planning is limited to20seconds,64 native queries,
twelve candidates, a96×96 patch window and4,096 charted resource entities.
Truncation or unknown/mixed resource data blocks rather than becoming free land.

Before routing power it selects the shared legal local mining escape, at most
twelve steps with at most one initial corner, and a resource-free EAST outlet
with three clear continuation cells. Only the on-patch escape receives mining
annotations; the outlet and its empty continuation are ordinary off-resource
transport. The exact local `collector_port` is retained separately. Future drills,
the exit and output access are protected while planning the normally paid power
bridge. Whole current/future preflight remains mandatory. Future trees are
reserved rather than mined early.

The returned bank and plan carry `layout_contract`, fixed output and power
interfaces, growth geometry, `flow_contract`, `capacity_profile`, and separate
`mining_reserved_area`. The outer reservation also protects the outlet and its
access. The contract exists before the caller accepts or publishes the bank.

## Monotonic extension

`validate_extension_contract(bank, new_plan)` verifies the immutable contract
revision, old ports, every old entity and scope annotation, exact paired grid,
reserved slot count and allowed added geometry. It requires a separate native
identity and lane recheck; geometric equality is not proof of ownership.

`prepare_extension(game, builder, obs, bank, added_drills)` preserves the existing
off-patch exit verbatim and adds paired columns behind it. The old
`retain_output` collar generator must not redraw a newly accepted exit.
The prepared result records every `retained_units` identity.
`validate_retained_units(obs, prepared)` must run before each construction step.
Missing, changed or rotated accepted units block extension.

Legacy banks without a contract are not silently migrated. The caller can derive
a matching grid origin and inspect `layout_contract(bank, policy)` read-only,
then explicitly accept that metadata after its normal ownership proof. An old
on-resource output is not relabeled as resource-free.

## Planned, installed, active and measured capacity

`capacity_budget(...)` computes each independent lane and collector ceiling.
`diagnose_bank_capacity(game, obs, catalog, bank)` rechecks native world, force,
surface, units, positions, directions, physical drop sides, power, remaining ore,
normal quality and actual entity speed/productivity bonuses. The entity bonus
already includes force effects; it is not multiplied by the force bonus twice.
Unsupported quality or negative speed profiles are reported without optimistic
capacity. Missing or unpowered/exhausted drills reduce the separate active
potential. Backpressure can make working count zero while resource-backed
powered potential remains positive.

Read-only native tick3,969,680 verified coal drills3832/3833: actual bonus0.1,
both physical drop lanes verified, installed and active potential66/min, working
count0. `measured_rate_per_minute` remains `None`. Output-packet evidence,
defense, downstream connectivity and sustained loaded measurements remain the
existing owning controllers' separate proofs.

## Controller integration

These exports are connected to the existing controllers. They do not register
a second writer or a separate mining job.

- `paired_mining.prepare_new_bank` locally delegates to the same-named policy
  wrapper, avoiding an import cycle with the shared pure generator.
- All new solid-resource `MiningOutposts.prepare` banks use that entrypoint.
  Contracted extensions delegate to the policy extension planner and recheck
  `retained_units` before any defense or mining construction. Existing
  uncontracted banks retain their legacy extension path and fixed live ports.
- Outposts publish the full outer reservation, separate `mining_reserved_area`
  and layout contract. Native paired-lane and downstream proofs remain required.
- DedicatedMaterials consumes the fixed off-resource output. Its ordinary coal
  hub branch verifies the exact hashed contract and fresh resource/terrain/entity
  clearance, without inventing mining annotations or requiring a nearby drill.
  The legacy tagged mining-exit branch remains available for accepted old plans.
  Coal distribution preserves the original collector/output and paid aliases,
  and replans power while protecting reserved growth and output approaches.
- SteamExpansion routes from the ordinary outlet, registers its contracted bank
  and narrow mining reservation before construction, and promotes that same bank
  to ready after its existing native lane and generation proofs.

Read-only native tick4,053,144 exercised the actual paired planner entrypoint:
whole stone-bank preparation passed in4.641seconds/eight queries. The14 current
entities include two opposing drills and power; three paired columns are reserved.
The fixed ordinary outlet is `(-7.5,50.5) EAST`, separate from collector
`(-13.5,50.5)`. The outer reservation is `[[-25,47],[-4,54]]`; only
`[[-25,47],[-13,54]]` is a mining reservation. Current/future geometry, exit,
three clear outlet cells and power passed native preflight. The66/min figure is
potential with current productivity. This diagnostic did not construct a bank
or publish runtime metadata, and these coordinates are not a global guarantee.

Focused tests cover policy integrity, unique ore scoring, grid phases, capacity
clipping, unchanged assets and interfaces, off-resource exit and access before
power, read-only preparation, exact-unit extension guards and native Lua
capacity/exit failures. No new bank was built by this implementation agent.
