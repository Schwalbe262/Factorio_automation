# Additive engine capacity

`EngineCapacity(game, builder, catalog, perimeter, *, engine)` exposes read-only
`prepare(obs, {'rate_per_minute': 12})`, resumable `step` with the same arguments,
and `proof(obs)`. It owns `engine-capacity.json`; it emits existing, normally paid
builder/defense actions. There is no new runtime action or ingredient courier.
The original EngineProduction must already be commissioned with its exact PCB
plan, fixed ports, unit bindings and current Steel ancestry intact.

Root wired the public `engine_capacity` capability with its rate validator,
lazy controller and independent proof. Integrated Engine/Capacity/budget/job
checks71PASS. Fresh complete preparation4994693 passed with24 additions and
the explicit24/min extra iron allocation. Luna commissioned the extension5016406
with18 normal actions and zero failures. New assemblers17104/17114 each finished
one engine; original outlet16066 remained fixed. Native5010319 collected the
requested12 normal belts from capped Mall chest16490. This verifies construction
and new production, not sustained12/min throughput.

The existing 31 by 19 reservation contains four engine slots and two component
assemblers. Increasing two engine assemblers to four adds 24 entities: two AM1s,
four normal inserters, two long arms, four poles and twelve belts. All existing
entities, source descriptors, Steel hash/furnace pins, transport, underground
pairs and output port remain. The generator accepts only the exact canonical
installed core; it does not relocate or recover hardware.

The cold phase builds twenty additions, excluding both input arms of each new
assembler. Native guards check exact units, force, surface, normal quality,
engine recipe, power, zero product counters, empty process inventories and zero
crafting progress before persisting the new-only baseline. Then the four input
arms may be built. Commissioning requires both new counters to advance and the
complete EngineProduction native proof, including old machines, mixed gear/pipe
lanes, sources, fixed output, defense and preserved pipe peers. Old baselines and
history are copied unchanged. A completed counter is never manufactured.

With the pinned recipe and AM1 speed 0.5, four assemblers have a 12 engine/minute
nameplate and require 12 steel, 12 gears and 24 pipes per minute. The shared gear
and pipe assemblers consume 48 iron/minute at that rate. Their 180 iron/minute
internal burst nameplate is not a bus reservation. The original fixed tap declares
24 iron/minute; this extension appends a 24-to-48 allocation outside that immutable
port descriptor. Once published, the same allocation is not counted again. The Steel source
must freshly prove at least 12/minute, normally the additive 15/minute producer.
Its existing coal bank is checked once against Steel plus Brick maximum usage;
the 66/minute source is shared, not multiplied by consumer count.

Budgets report global iron/copper claims, deficits and zero guaranteed surplus.
For the current 375 iron/minute source, the extension changes 683.5/minute declared
peaks to 707.5/minute, a 332.5/minute design deficit. Steel's nominal 15 minus engine demand 12 leaves
3/minute before other consumers. These are catalog budgets and snapshot source
potential, not measured sustained throughput. Capped malls and other consumers
can reduce actual engine production through ordinary backpressure and priority.

Publication uses a durable compare-and-swap intent covering `engine-production.json`
and `pcb-board.json`. Resume accepts only each recorded old or new hash and
completes a known prefix idempotently. It appends one capacity receipt and a Steel
consumer allocation under `producer_capacity_allocations` and the iron delta
under `consumer_capacity_allocations`; original main-bus port descriptors are
unchanged. The receipt records old/new full plans, retained old
unit IDs, new machine IDs, actual production witnesses and unchanged source pins.

Future Blue consumers can call
`verify_engine_capacity(game, obs, catalog, engine, pinned_plan_revision=...,
pinned_source=..., pinned_machine_units=None)`. It walks at most eight unique
additive receipts, checks current canonical publication, unchanged ports/pairs,
complete retained units, source pins and fresh native proof of all machines.
The caller keeps its original plan hash and exact full output descriptor.
Missing, ambiguous or invalid ancestry fails closed. This module does not edit
Blue, jobs, runtime or accepted Engine source code; parent integration owns those
consumer/capability hooks.

Focused tests cover layout/cold gates, native inventories and contamination,
old/new identities, complete production, shared budgets, future-consumer ancestry
and interrupted two-document publication. Live commissioning and measured rates
remain separate from an offline test or read-only whole-plan prepare.

Read-only whole prepare passed at observation 4884250 in 5.406 seconds: 24 paid
additions, twenty cold entities, Steel ancestry15/minute, shared coal66 versus8.1,
and 250.8 kW additional peak load with native network reserve preserved. Full
preview: `%TEMP%/factorio-engine-capacity-4884250.json`. No action, producer
checkpoint, PCB publication or commissioning was executed by that prepare.
