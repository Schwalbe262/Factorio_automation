# Shared material bus

## Planned bus on fresh maps (`planned_bus.py`, capability `planned_main_bus`)

Roadmap step `main_bus` (right after `smelting_district`, before
`automation_science`) builds the bus of the site plan on any seed: `plan.bus` gives head, flow direction (N/E/S/W) and the
lane table. The first `iron-plate` and the first `copper-plate` lane each get
a straight trunk of `TRUNK_TILES = 64` belts from the head; foreign belts
already on a lane are crossed with underground pairs. All other lanes stay
reserved. Each trunk is fed from its district plate port: three tiles upstream
of the port a splitter replaces the district belt and a five-belt branch
leaves the district beside the port (the side opposite green science's
branch), routed to the lane head with the `corridor:smelting->bus` guide. Red
science keeps the port; the feed splitter gives the bus branch output
priority, so the port gets what the bus does not take. Phases:
build (everything but the splitters), splice (splitters go into the district
plan, splitter stock first, the exact old belt is mined, splitter built),
commission (feed and tap splitters get their priorities), verified. Proof:
every planned belt as planned, native underground partners, every feed and
tap splitter with its wanted priorities, each built trunk carries only its
plate and its tail belt holds plates.

Splitter priorities (`planned_bus.feed_priorities`, `tap_priorities`):

| Splitter | Input priority | Output priority |
|---|---|---|
| district feed | none (one input) | the bus branch |
| tap | none (one input) | the consumer branch; the surplus continues down the bus |
| merge onto a bus lane (lane injection, second lane of a material; not built yet) | the bus lane | none |

Native left/right is relative to the splitter's facing; it is derived from
the facing and a point on the branch (`splitter_side`), so every bus
direction, tap side, rotated or mirrored district gets the right side. The
specs carry `splitter_input_priority`/`splitter_output_priority`; the bus
commission step and `planned_tap.cutover` issue `configure_splitter` (unit
and world guarded) for each built splitter whose live priorities differ.
That is idempotent and also configures splitters of a saved map built before
the rule. `board["shared_main_bus"]` gets `template:
planned_main_bus`, the bus frame, lanes (built/reserved/spare), trunks,
per-material lane-band `reservations` over the whole bus length, head/tail
`ports`, `entities`, `underground_pairs` and `sources`.

### Taps (`planned_tap.py`)

Consumers beside the bus take plates only from taps; no consumer runs its
own line out of the district. A consumer plans its taps with its block
(`planned_tap.connect_inputs`, called by red science, green science and
`produce` via `production_site`): per plate input, the tap row nearest the
input port inside the block's along range (+-16) that fits, then the route
from the tap port to the input. Geometry in the bus frame (any direction,
either side; `a` splitter row, `o` tapped lane, `s` = +1 right / -1 left):

| Rows | Hardware |
|---|---|
| `a` | splitter on lanes `o` and `o+s` (the partner tile; may not be a built trunk) |
| `a+1` | belt on `o+s`, bus direction |
| `a+2` | branch: curve on `o+s` turning outward, straight to the port, the second tile outside the band; reserved and spare lanes are tunnelled under (mouths on gap tiles, span <= max span) |
| `a+1..a+3` | each *built* trunk between `o+s` and the edge dives under the branch (underground pair `a+1 -> a+3`) |

Tap windows `[a, a+3]` keep one free row apart; rows of every involved lane
must be plain trunk belts. A lane that ends before `a + 8` is extended (paid
belts, foreign belts crossed underground, at most the bus length); past the
bus length the reason is `tap_beyond_bus_length`. Execution
(`planned_tap.cutover`, run by the consumer before its own `ensure_plan`):
stock every tap item, mine the exact trunk belts (unit numbers from planning),
build the tap, mark it built. Taps are recorded in `planned-main-bus.json`
`taps` and published as `stations`, tap `ports`, entities and pairs;
`planned_bus.effective_plan` applies them to the bus plan and proof.

Supply rule (`planned_tap.tap_budget`, `plate_supply.bus_budget`): a trunk
carries the district's nominal supply; each tap records its consumer's
declared demand; a new consumer plans with `max(nominal - other taps'
demand, nominal / 2)` and sizes its machine count down to it (red:
`science.py`, green: `green_science.supply_sized_count`, `produce`). With
branch priority every tap gets its full declared demand while the total stays
within the trunk, wherever it sits on the bus. The half floor is only an
over-subscription allowance: then the taps upstream are served first and a
downstream consumer gets the rest (the district green branch without a bus
is an even splitter and does hand half), measured by `tools/report_run.py`.

Bus band: from the moment `site-plan.json` exists the whole bus rect is hard
for every `transport.plan_connection` route except the bus's own feed
(`bus_route=True`); a route may not end inside it
(`route_endpoint_inside_bus_band`). The band (28) is wider than any
underground span, so a consumer cannot cross it; it taps from its own side.

Without a planned bus (no site plan, or no bus published for this world)
red science takes the district plate ports and green its splitter branch at
the district (`plate_supply.plate_bus` returns None).

Steel and bricks plan from the site plan on this bus
(`planned_furnaces.py`, see STEEL.md): the steel block taps iron with
`connect_inputs`; its steel leaves on a board port, the reserved
`steel-plate` lane is not built yet (it needs an injection: a branch from
outside the band onto a reserved lane plus a trunk starting there, crossing
the built trunks between, the reverse of a tap).
Engines and magazines are generic `produce` rows on this bus
(`planned_products.py`): `engine_production`/`ammo_production` hand over to
`ProductionRows` on a planned world; the engine row takes steel from the
steel block's board port by an ordinary route (no steel lane is needed) and
makes gears and pipes in-line from one iron tap (twin sub-rows,
`production_layout.py`). Not yet: `bus_tap.BusTap`,
`industrial.free_tap_parameters` and the consumers that still require the
development-map template below (advanced circuits, construction mall,
engine capacity, chemical science); on this bus they block with `planned_main_bus taps go
through planned_tap.connect_inputs` until they plan sites from `SiteService`
and call `connect_inputs`. Building a reserved lane later must pass under
existing taps' splitter/branch tiles (`planned_bus.trunk_plan` crosses only
plain belts). The legacy `main_bus_factory` below is no longer a roadmap step
(explicit request only).

## Development-map bus (`main_bus.py`, `bus_factory.py`)

`main_bus.py` compiles one real NORTH trunk for each declared material line.
Several consumer splitters draw from that same trunk. The initial layout uses
one iron belt at x=-11.5 and one copper belt at x=-3.5. A spare column four tiles
west of each is reserved: iron x=-15.5, copper x=-7.5. Spare belts are not built
and do not count as production. More parallel lines require sufficient reserved
space and an independent declared source rate for every active line.

All consumer outlets face EAST at a fixed x. Their splitter row is one tile
south of their outlet. Native underground crossings are installed across both
active and reserved future columns, so activating a reserved line preserves
existing branch entities and ports. The blueprint accepts foreign belts and
uses the existing crossing compiler; unsafe crossings fail without a maze
fallback. Source and consumer adapters use `transport.plan_connection` with the
existing six-turn and `1.35 * Manhattan distance + 4` length gates.

```python
from factorio_agent.main_bus import BusMaterial, ConsumerTap, plan_main_bus

plan = plan_main_bus(
    [BusMaterial('iron-plate', -11.5, (225,), reserved_lines=2),
     BusMaterial('copper-plate', -3.5, (112.5,), reserved_lines=2)],
    [ConsumerTap('green-iron', 'iron-plate', -76.5, 'critical',
                 demand_per_minute=192.5),
     ConsumerTap('green-copper', 'copper-plate', -79.5, 'critical',
                 demand_per_minute=52.5)],
    north_y=-118.5, south_y=-12.5, port_x=12.5,
    foreign_entities=observed_belts,
)
```

`source_rates` are design contracts, never measured throughput. Yellow belts
carry at most 900 items/minute across both lanes, 450/minute on one lane.
Capacity reporting separates producer shortage, belt shortage and lane limits.
The pinned live prototype reported speed .03125, underground span 5 and NORTH
splitter footprint 2x1 in Factorio 2.1.20. At full machine ratings, red36 plus
green35 need264.5iron/minute; a225iron/minute source still has39.5iron/minute
deficit. Extra belt columns do not solve that producer deficit.

## Native priorities

For NORTH splitters, left is WEST and right is EAST. The continuing trunk uses
the left input/output; the eastbound consumer uses the right output.

| Tap policy | Native input priority | Native output priority |
| --- | --- | --- |
| critical | left | right |
| normal | left | none |
| surplus | left | left |

`SourceMerge` accepts a second source on the right input and combines both
inputs into the single left/continuing output. Its explicit input priority is
left, right or none; output priority is left. This shares one belt's bandwidth.
The planner emits `splitter_input_priority` and `splitter_output_priority`;
the executor must configure those native properties after paid placement.

Priorities are local. An upstream normal/surplus consumer can consume material
before a downstream critical consumer sees it. `priority_inversions` exposes
that ordering. No absolute rate reservation or global fairness is claimed.

## Compact factory migration

`BusFactory(game,builder,catalog,smelting,science,green)` provides read-only
`prepare(obs)` and resumable `step(obs)` / `proof(obs)`. Preparation reloads
current production state, traces exact saved belt corridors through their
declared underground pairs, binds present entities to observed unit identities,
and validates the complete alternate layout before any mining or construction.
Absent obsolete hardware is omitted from recovery and from new authoritative
plans; changed present hardware blocks preparation. No ownership comes from an
area selection.

The migration preserves the producer ports iron(-9.5,-29.5)E and
copper(-9.5,-17.5)E. It normally recovers both old consumer corridors and the two
owned old source splitters, then restores ordinary source belts so the full
producer stream enters the shared bus. It retains red machines, labs, coal,
source producers and all old poles. Four paid bus splitters serve red and the
compact green block. Bus handoffs are x12.5; bounded adapters reach green
iron(40.5,-77.5)E and copper(40.5,-80.5)E. The adapter avoids the existing pole
at(14.5,-77.5), which prevents a direct straight extension to x40.5.

Recovery disconnects the old raw-material corridors before the replacement is
built. Retaining the old machines protects recoverable assets; this version
does not guarantee uninterrupted science production during the cutover. It is
a normally paid, resumable migration, not a parallel hot-switch procedure.

The new 12-machine green block occupies30x22 tiles with a32x28 reservation at
[[39,-82],[71,-54]]. Output(69.5,-59.5)E reaches the retained green lab entry
(47.5,-108.5)N. Existing final underground mouths and the lab join remain.

Execution phases are recovery, build, commission, retirement and verified.
Recovery emits batches of at most eight ordinary guarded mining actions.
Completed recovery identities are removed from pending state before building
replacement entities at the same coordinates. Production plans are published
only after initial recoveries finish. Old western assemblers/inserters and
belts are retired only after new production and all eight labs are proven.
Their reservation remains until cleanup completes. Every pole is retained
initially to avoid breaking unrelated electrical connections.
Fresh source-smelting and replacement proofs are required before every
retirement batch and final success; stored commissioning evidence alone is
insufficient after later damage.

## Validation evidence and limits

The first full read-only native preparation at tick1377188 passed with596belts,
36underground mouths,4splitters,12AM1,13fast/14normal inserters and21poles. It
identified658 initial owned recoveries,459 later retirement entities and55
normally clearable trees. Seven adapters passed the unchanged route gates;
the longest was green output:77tiles,5turns for71Manhattan distance. The bus
itself used260belts,20underground mouths and4splitters. No game or runtime state
was changed by preparation. Later live destruction can change recovery counts,
so a fresh preparation is authoritative.

The static compact audit proves ordinary input/lane paths to all12 machines;
the lab audit proves green reaches all8 pickups. Live commissioning additionally
requires all12 machine recipes/products/power, native underground partners,
native splitter priorities, correctly typed occupied bus lines, red/green
production proof and green stock in every powered lab. This does not certify a
sustained science rate, full-game completion or a rocket launch.

Targeted tests cover shared geometry, native priority sides, source merge
semantics, preserved ports/entities during parallel activation, reserved-line
crossings, source/belt/lane budgets, exact corridor tracing and recovery guards,
publication freshness and commissioning gates. Per-agent token and weekly
quota usage are unavailable; the root task records the project/account meters.

## Live completion: 2026-11-12

`luna-main-bus-compact-20261112` succeeded on its original key at1660346,
proof1660440:347actions, zero consecutive failures at completion. The earlier
three tree-mining failures/full inventory block are preserved in EXP-0012/0013.
Planning456.81s/execution669.54s; recorded paused/blocked intervals549.91s were
excluded from active budgets without resetting action history or consumed time.

Both shared lines carried only their declared material (509iron/553copper in
the receipt). All four native splitters had input-left/output-right priorities.
All12compact machines and all8labs were verified; green counters were
[45,45,45,43,42,39,11]. Both lab-row lanes held128packs, green left/red right.
The preserved12iron/6copper furnaces and ore/coal feed lanes passed fresh proof.

Independent live reinspection at1663671 again passed the complete proof:
phaseverified, no pending recoveries/retirements, no old western assemblers,
inserters, belts or underground belts in its reservation. Every old pole was
retained. All8strategic posts remained healthy with100rounds each. The current
layout image is rendered from that real observation, not the prospective plan.
This remains commissioning evidence; no sustained30SPM/5min certification.
