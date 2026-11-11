# Compact green science block

`factorio_agent.compact_green.compact_green_core(x, y)` compiles a complete
production block with **12 assembling-machine-1s, 155 ordinary belts, six
underground belts, 13 fast inserters, 14 ordinary inserters and 16 poles**.
There are 216 entities, no splitters, no individual raw-material delivery
snakes, and no output splitting. The occupied envelope is 30 by 22 tiles;
the 32 by 28 reservation includes four empty 3 by 3 future-machine slots.
These slots are space reservations, without claimed production or connected
ingredients. Seven science assemblers provide a nominal 35 green/minute for
the 30/minute target. Neither capacity math nor preflight proves a sustained
live rate.

The previous `manufacturing.green_core` reserved 54 by 53 tiles and needed
477 internal entities; its complete installed plan had 956 entities including
external connections. Compare 216 with the internal core; the new block's
external main-bus adapters, lab connection and electrical bridge still need
to be included in any complete-installation comparison.

## Fixed interfaces

All coordinates below use the recommended anchor **(43.5, -78.5)**. Inputs and
output face east. An origin change translates every entity and interface.

| Interface | Position | Rated flow/minute |
| --- | --- | ---: |
| Shared iron plate input | (40.5, -77.5) | 192.5 |
| Shared copper plate input | (40.5, -80.5) | 52.5 |
| Green science output | (69.5, -59.5) | 35 |
| Existing-pole electrical connection | (43.5, -76.5) | native electric grid |
| Reserved area | [[39, -82], [71, -54]] | 32 by 28 tiles |

The shared main bus can supply east-facing tap endpoints (12.5, -80.5) for
copper and (12.5, -77.5) for iron, corresponding to splitter station rows
-79.5 and -76.5. Route two ordinary `transport.plan_connection` adapters to
the block ports. A direct extension of the iron tap meets an existing pole
at (14.5, -77.5), so let the bounded native router choose the short detour.
Do not move either raw port to fit a guessed straight corridor.

## Material flow

Coordinates in this diagram are offsets from the anchor. Every assembler is
3 by 3. `>` and `<` between adjacent machines represent a real ordinary
inserter in the one-tile gap.

```text
y=-2  copper --------------------------------------------------> x25
                                                                  |
y= 1  iron ------+-----+-----+---+--------------------->           UG
                |     |     |   |                                 |
y= 4         [BELT]  [GEAR] [INS] < [CIRCUIT] < [CABLE] <----------+
              x2      x8   x14      x18         x22
              ^       |     ^
y= 7          +--gear-+     +--gear--+   (two real gear extractions)

      BELT output -> north side of mixer
      INS output  -> south side of mixer, upstream of FIRST pickup
y=13  ======= belt on north/left lane; inserter on south/right lane =====>
               |   |   |   |   |   |   |
y=16          [G] [G] [G] [G] [G] [G] [G]     centers x1,5,...,25
               |   |   |   |   |   |   |
y=19           +---+---+---+---+---+---+------------------> output x26
y=23          [reserved] [reserved] [reserved] [reserved]
```

One iron belt passes all four iron consumers. Cable inserts directly into
circuits, and circuits directly into inserters. The gear machine uses two
normal output inserters for the actual belt and inserter consumers. The
science ingredient mixer uses perpendicular side loading before every
science pickup; it places belts and inserters on opposite lanes of one
eastbound belt. Science output is one eastbound collector.

The three southbound underground pairs are local crossings, offsets:

| Input | Output | Crosses |
| --- | --- | --- |
| (25, 0) | (25, 2) | Copper under shared iron at y=1 |
| (11, 6) | (11, 8) | Inserter product under gear at y=7 |
| (-1, 9) | (-1, 11) | Belt product under inserter product at y=10 |

Each has two-tile mouth separation, straight approach and exit, and a
distinct x coordinate. No tunnel contains a turn. No foreign surface belt is
replaced or joined at a crossing. These are normal yellow underground belts
with the installed prototype's five-tile maximum endpoint separation.

At 35/minute, total demand is iron 192.5, copper 52.5, gears 52.5, cable 105,
circuits 35, inserters 35 and transport belts 35. The one gear machine and
one cable machine each run at 87.5% of nominal capacity. Multi-product belt
crafting produces two belts/craft. Direct cable transfer and the gear's
105 iron/minute input use fast inserters. Science machines need only ten
ingredient items/minute and five output items/minute each, so their two
ordinary inserters have ample static headroom.

## Verification and migration

`audit_compact_green(plan, catalog)` checks full prospective tile footprints,
reservation containment, empty future slots, all internal pole coverage and
wire connectivity, declared native underground geometry, raw-rooted recipe
reachability, every science pickup's actual two lanes, every machine's output
connection, catalog machine capacity and conservative inserter/lane budgets.
The budgets are 120 items/minute for fast inserters, 30 for ordinary inserters
and 450 for one yellow-belt lane. These are static limits, not a simulation of
pickup timing. `preflight_compact_green(game, builder, obs, plan, catalog)`
then surveys the entire reservation and every entity using read-only native
queries, returning the ordinary tree-clearance list.

The read-only survey on world `32a34974-d7cd-4ec0-9f35-13259ea32b8e`,
Factorio 2.1.20, tick 1,344,015 found **zero water tiles, zero resource
entities and 40 normally clearable trees** in this reservation. Whole-block
native preflight passed for all 216 proposed entities. The installed catalog
audit passed with all 12 machines receiving every ingredient. Native prototype
inspection separately confirmed yellow belt speed .03125 tiles/tick,
underground span 5, fast inserter rotation .04 turns/tick and extension
.1 tiles/tick; ordinary inserter values are .014 and .035. No entity, item,
recipe, progress counter or runtime file was changed by these checks.

Retain the western green production hardware until the replacement has ordinary
paid construction, grid power, all twelve recipes producing, verified native
underground neighbours and observed output. The current migration disconnects
old raw corridors first, so production can pause during construction. Retained
hardware does not imply an uninterrupted operating fallback. The lab interface
is **(47.5, -108.5), north**, entering the westbound row at (47.5, -114.5).
It is upstream of the last lab at x45.5. Preserve this entry when connecting
the new output; injecting at x43.5 omits the last lab.
`audit_lab_injection(lab_plan, entry)` verifies actual directed belt reachability
to every lab pickup and rejects that earlier seven-of-eight regression.

The local block's internal poles form one connected network but need an
external electrical bridge. Main-bus adapters, output-to-lab routing and
power are integration work. Re-run native preflight immediately before
construction because the live worker may have changed nearby infrastructure.
The ordinary plate supply must cover red plus green demand before claiming
a sustained 30/minute rate. Do not tear down the western factory merely
because this static blueprint passed.

Focused validation: `python -m unittest discover -s tests -p test_v2_compact_green.py`
with `PYTHONPATH=src`. Fourteen tests cover all seven individual ingredient
pickups and outputs, both raw inputs, both gear consumers, direct wire
transfer, the required fast transfer, lane mixing, native mouth roles/exits,
full footprints, reserved slots, catalog speed, power, and all eight lab paths.

This subtask's exact Codex token count and weekly quota change are unavailable
to the delegated worker; the parent should report its measured thread/account
usage rather than estimating either value.

## Native commissioning completed

The original migration key succeeded at1660346, proof1660440. All five
intermediate machines and seven green machines produced; green counters were
[45,45,45,43,42,39,11]. Every lab held both pack types and each row lane held128.
The last green station needed an extended normal buffer-fill interval before
its first product; waiting was not treated as completion or bypassed by items.
Independent reinspection1663671 again passed with green counters
[50,49,49,48,47,44,15]. The old western hardware was normally recovered while
its poles were retained. The image [pcb-layout-current.png](images/pcb-layout-current.png)
shows the actual compact block and shared bus after retirement. Rated35/minute
remains a design rating; measured sustained output/consumption is still pending.
