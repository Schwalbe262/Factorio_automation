# Dedicated bulk producers and main-bus growth

Implement this incrementally: verified additive source upgrades, dedicated bulk
Green/Steel production, then new material trunks and priority consumer stations.
Existing source ports, native units, claimed consumer edges and accepted science
layouts remain fixed. Every stage needs normal costs, fresh native production,
resource/future-producer/power/defense checks and the existing routing budgets.

## Existing contracts and the first bottleneck

`BusMaterial` already declares parallel active sources, reserved columns and
four-tile line pitch. `ConsumerTap` supplies named, typed, fixed EAST outlets;
`SourceMerge` combines two inputs into one continuing belt. `BusTap` preserves
old ports/units/tunnels and permits only its one exact trunk-belt replacement.
Adding a material or activating a reserved line needs a new migration capability;
the current tap API intentionally requires an existing active material line.

Metadata inspected in the installed run declares Iron375/min and Copper150/min,
against existing peak tap demands466/min and138.5/min: Iron is already91/min
short, while Copper has11.5/min nominal slack. These are design contracts, not
measured simultaneous consumption. Resolve that ledger before promising bulk
output; a splitter or extra belt does not create plates.

| Installed-catalog production stage | Output/min | Iron/min | Copper/min | Coal/min ceiling |
| --- | ---: | ---: | ---: | ---: |
| Existing 2 stone-furnace Steel | 7.5 | 37.5 | 0 | 2.7 |
| Add one opposing furnace pair | 15 | 75 | 0 | 5.4 |
| Fill all 3 reserved furnace pairs | 22.5 | 112.5 | 0 | 8.1 |
| Bulk Green: 1 AM1 circuit + 2 AM1 cable | 60 | 60 | 90 | 0 |
| Expand to 2 AM1 circuit + 3 AM1 cable | 120 | 120 | 180 | 0 |
| Same 3:2 cell using AM2, when unlocked | 180 | 180 | 270 | 0 |

These use circuit0.5s/1Iron+3Cable, cable0.5s/1Copper→2Cable,
AM1 speed0.5, AM2 speed0.75, and Steel16s/5Iron→1Steel at furnace speed1.
The initial Green cell has spare cable capacity; steady consumption is bounded
by its one circuit machine. Use fast local inserters sized for the actual flow.
Coal ceilings use90kW per stone furnace and4MJ coal; electricity is additional.
Nameplate math must remain separate from measured sustained production.

## First implementation: additive Steel with accepted source revisions

Build `SteelCapacityExpansion.prepare/step/proof(obs, {'pairs':2})`, initially
supporting only1→2→3 pairs inside the existing full growth reservation. Reuse
`paired_smelting_core`; prove every old entity/configuration is an unchanged
subset, all old bindings remain exact, and only added furnaces/arms/poles/feed
extensions are built. Keep the existing Iron(94.5,-44.5)S, Coal(93.5,-40.5)N and
Steel(108.5,-37.5)E ports, including the Steel output belt unit and Engine route.
No furnace replacement or existing-belt recovery belongs to this additive job.

The2→4-furnace stage adds37.5Iron/min and2.7Coal/min ceiling. It must therefore
clear at least128.5/min of the current Iron ledger deficit before declaring all
old peak contracts plus expanded Steel supplied. Preserve the science-first
surplus tap policy; starvation remains an explicit wait, not an implicit waiver.

Add `source_upgrades.py` as the shared receipt/verifier used by this first job.
A `producer-addition-v1` receipt records world/catalog, producer ID, exact old
and new plan hashes, unchanged typed-port/unit/configuration, all retained native
bindings, exact added bindings, old/new producer memberships, capacity delta,
fresh native production/lanes/coverage proof and an idempotent publication ID.
Require old entities/configurations/ports/tunnels to remain subsets of the new
plan; reject replacements, missing old units, changed outlets and unproven extras.

Engine currently pins both `steel_plan_revision` and `steel_furnace_units`;
Advanced pins the Plastic revision, and Blue pins all producer plans. Keep those
checks. An explicit receipt chain may advance a consumer only from its exact
pinned old hash to the verified new hash and exact extended membership. Unrelated
source/route changes still block. Save the publication intent before updating
producer/board/consumer files; compare-and-swap expected revisions, tolerate only
the declared old/new states after interruption, and retain original proof history.
Extend the existing derived-capacity refresh pattern; do not edit rates alone.

## Dedicated bulk Green and a separate Steel bus source

Next build `BulkCircuitProduction` with fixed Iron/Copper inputs, direct/local
cable insertion, one pure `electronic-circuit` belt outlet and an empty second
circuit/third cable slot. First60/min, then additive120/min, preserving that
outlet and old units through the same receipt protocol. Do not siphon the compact
Green-science cell's internal circuits. Keep copper cable local by default.

The old Steel outlet already feeds Engine. A second alias does not create a
second usable consumer edge. Feed the new Steel material trunk from a separately
commissioned bulk Steel bank/collector with its own fixed port; alternatively,
use a separately reviewed priority-splitter cutover, outside the additive job.
Neither path silently modifies the existing Engine feed or duplicates capacity.

For a concrete first combined target—Steel15, bulk Green60 and a separate
bulk-Steel7.5 bank—the present ledger becomes Iron601/min, Copper228.5/min.
That is226/min more verified Iron and78.5/min more Copper than current sources.
Recheck mining, smelting, fuel, inserters and measured power before activation.
Further shared gear/pipe/plastic/brick producers use the same typed-source model,
with explicit demand-driven targets rather than every recipe getting a bus lane.

## Trunks, stations and proof gates

Yellow belt capacity is450/min per lane and900/min across both. At601Iron/min,
both occupied lanes or independent parallel lines are required. Existing Iron
columns-11.5/-15.5 and Copper-3.5/-7.5 stay fixed; unused columns remain reserved.
Survey a separate expandable band for Steel and circuits with2 reserved lines
each, four-tile pitch, full future crossings and reserved splitter/service rows.
No new world coordinates are approved by this document.

`MainBusMaterialExpansion` must preserve old station ports, native units and
underground peers while adding new trunks/merges. Reserve at least3-tile station
spacing. New typed taps choose an explicit active line and critical/normal/surplus
policy; keep native NORTH splitter mapping right/none/left output priority.
Report upstream priority inversions: priorities are local, not global guarantees.
`SourceMerge` still shares one900/min belt. Each active parallel line needs a
verified source/physical feed mapping; aliases of one producer share one budget.

Acceptance requires fresh exact-source proof, pure native outlet lanes, real
finished products, normal paid items and retained units, then current-consumer
proof after receipt adoption. Report producer, per-lane, belt and consumer-rate
limits independently. Add interruption/replacement/resource/priority regression
cases before deployment; sustained rates require a separate timed native run.
