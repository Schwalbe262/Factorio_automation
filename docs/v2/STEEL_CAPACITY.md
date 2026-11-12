# Additive paired steel capacity

`SteelCapacity(game, builder, catalog, perimeter, *, steel, industrial=None)`
provides read-only `prepare(obs, {"rate_per_minute": target})`, ordinary-action
`step` with the same parameters, and `proof(obs)`. State is `steel-capacity.json`.
The first target15 adds one opposing furnace pair;22.5 fills all three reserved
pairs. Catalog recipe yield, time, speed and coal energy determine rates.
The `steel_capacity` capability accepts the absolute target through
`rate_per_minute`; its independent proof uses the same controller. Existing
Engine pins stay immutable: only verified additive ancestry and fresh native
health of all current furnaces permit the expanded source.

Every old plan entity, unit ID, source descriptor, input header, shared output
collector, output belt6213 and reserved boundary remains fixed. New entities are
limited to stone furnaces, inserters, small poles and short header extensions.
The controller does not mine or replace existing hardware and cannot silently
repair a lost original unit.

The15/min expansion adds2 furnaces,4 inserters,2 poles and8 belts. Its14-entity
cold plan omits the two input inserters. Only after the newly paid furnaces have
exact native IDs, empty input/output inventories and zero product counters are those two
input arms built. Each new furnace must then actually finish steel, the complete
producer must have healthy automatic inputs and both physical output lanes must
contain steel for commissioning. Existing full-output furnaces need healthy
historical production, not an artificial fresh-counter increment. Published
consumer checks permit legitimate downstream draining of the output buffers.
Normally inserted coal may already occupy the separate fuel inventory at the
zero-production baseline; its exact normal item metadata is still validated.

All new footprints undergo strict resource/future-transport/native preflight.
Power headroom and the existing complete industrial defense scope are checked;
defense is followed by a fresh complete-plan preflight. New furnace inspection
uses the installed2.1 `defines.inventory.crafter_input`; its executable Lua test
rejects missing/obsolete inventory enums, nonnormal quality and contamination.

## Shared input budgets

At15 steel/min, input demand is75 iron plates/min and maximum catalog coal use
is5.4/min. At22.5, demand is112.5 iron and8.1 coal/min. The same industrial coal
bank also supplies Brick; its maximum2.7 coal/min is included exactly once.
Native paired-drill diagnostics must freshly prove adequate powered,
resource-backed potential and both physical drill-drop lanes. Full source and
consumer world/catalog frames and fixed coal registry identity are guarded.

The iron bus source is375/min nominal. With the capped construction mall included,
the first expansion's declared peak is683.5/min, a308.5/min design deficit.
The controller reports **zero guaranteed spare rate**. Capped buffers and surplus
priority may permit production; this does not certify that all consumers can
sustain their simultaneous nameplate loads or that splitters create capacity.

Existing bus-port descriptors retain their original declared demand, because
other consumers pin those exact interfaces. Additional iron allocations are
append-only entries in `pcb-board.json.consumer_capacity_allocations`, with
consumer, item, old/new rate, delta and receipt ID. Subsequent capacity budgets
include those deltas. `bus_budget.declared_bus_demand` accounts for this ledger
in Steel and Mall diagnostics, rejecting duplicate material allocations,
nonfinite/negative rates, inconsistent deltas and discontinuous ancestry.
A single expansion receipt can allocate multiple materials without duplication.

## Safe consumer ancestry

Each accepted expansion appends `capacity_revisions` to `steel-production.json`.
A receipt includes:

- Mode `additive_steel_capacity_v1`, world/catalog, digest and old/new plan hashes.
- Complete old/new plans and every retained old unit binding.
- Immutable full output descriptor including its existing native unit ID.
- Original/current/added furnace IDs, positive new-counter witness, dual-lane
  commissioning witness, rated capacity and shared input budget.

Producer, PCB and Industrial authorization are published using a durable
three-document old/new hash intent. Interrupted publication resumes only exact
old or intended new payloads; foreign changes block. The original source aliases
and Industrial input-source contracts are not rewritten.

ROOT's minimal Engine hook is:

```python
verify_steel_capacity(
    game, obs, catalog, steel,
    pinned_plan_revision=engine.state['steel_plan_revision'],
    pinned_source=engine.state['steel_source'],
    pinned_furnace_units=engine.state['steel_furnace_units'],
)
```

The helper returns `ok`, `current_plan_revision`, `original_furnace_units`,
`current_furnace_units`, `source`, `proof`, `rated`, and receipt IDs in `chain`.
It verifies canonical publication, receipt digests, unique complete ancestry,
strictly additive allowed assets, all old unit bindings, original furnace subset,
unchanged output and actual health of **all current** furnaces. Original Engine
hash/IDs stay immutable. ROOT should replace both direct plan-hash rejection
sites and exact original-vs-all-furnace comparisons with this checked result;
blindly updating a saved hash is not equivalent. No unreceipted upstream rebuild
is accepted.

## Evidence and remaining scope

Read-only complete preparation passed at4641837 in2.234s. New furnace centers are
(102,-40) and(102,-35); new input arms are(102.5,-41.5)N and(102.5,-33.5)S.
All285 existing plan units and output6213 at(108.5,-37.5)E are retained. Shared
industrial coal potential was66/min against8.1/min combined Steel+Brick maximum
coal demand. Evidence is `diagnostics/steel-capacity-15-prepare-part17.json` in
the local world runtime. Integrated preparation4790769 confirmed16 additions.

Luna commissioned the first extension4810835 on the same key with13 normal
actions. The first baseline rejection preserved12 paid actions; inspection4804723
proved both new process inventories empty, products0 and separate normal coal8.
After the process-only baseline correction, new furnace16921/16924 each finished
one steel cycle; old6172/6176,285 old plan assets and outlet6213 remained fixed.
Both output lanes contained actual steel. Native4799204 guarded Mall stock
pickup moved the requested8 belts from chest16490, proving factory-funded
construction. This is commissioning evidence, not a sustained15/min measurement.
Fresh Engine source proof4818782 accepted the added capacity receipt while its
original source hash, furnace IDs and full port descriptor stayed immutable.

Thirteen focused capacity tests and18 existing SteelProduction tests pass,
including real Lua inventory/counter/quality inspection, cold-feed ordering,
additive two-step ancestry, original-pin preservation, forged/missing receipt
rejection, shared budget deltas and interrupted three-authority publication.
Integrated focused checks90PASS. Indexed canonical snapshot
`part19-steel-20261112-173554`:1159 tests,1157PASS/2skip in56.732s.
