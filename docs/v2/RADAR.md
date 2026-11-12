# Normally supplied radar coverage

`RadarNetwork(game, builder, catalog, perimeter)` exposes read-only
`prepare(obs, parameters=None)`, paid resumable `step(obs, parameters=None)` and
fresh `proof(obs, parameters=None)`. Parameters accept only `None` or `{}`.
Its checkpoint is
`radar-network.json`. Only the existing executor under the world writer lock
calls `step`; preparation never writes checkpoints or the board.

The first deployment builds exactly one base radar and its necessary ordinary
small electric poles. The pinned native catalog supplies the recipe and power
cost. In the installed 2.1.20 catalog, a radar costs ten iron plates, five gears
and five electronic circuits, and consumes 5,000 joules per tick, or 300 kW.
Native radar research and the enabled radar/pole recipes are required; the
controller never changes research or grants ingredients.

The radar has an explicit three by three footprint, including the builder's
`_width`/`_height` metadata. Its seven by seven reservation keeps four service
sides and space for a local power pole. Native local walkability must connect
all four service sides to the outside of that reservation. Native site checks
keep the service reservation free of ores and mining growth. Whole preflight
checks the actual radar and new poles; service cells are walking reservations,
not fake belts to place.
Other production reservations, shared bus reservations, unbuilt external
hardware/tunnels and four outward cells at fixed typed ports remain protected.
Existing poles used by the connection carry exact saved identities and are not
adopted as radar-owned assets.

Placement considers at most sixteen base sites, derived from pairs of actual
canonical perimeter posts near existing base machines. Observed full health
and at least five observed full magazines filter canonical posts before ranking;
fresh native guards recheck both. Candidate ranking shares the finite site cap
across eligible post pairs. The bounded generator checks at most thirty-two post pairs and 16,384
distinct half-tile centers. It
filters complete service reservations, future/port keepouts and both native
defense disks before applying the sixteen/eight site cap, so reserved midpoints
do not crowd out valid exterior sites. Every accepted site
requires two separate full-health gun posts, each covering the complete radar
reservation with the canonical two-tile range margin and holding at least five
full normal magazines. Enemy units or spawners within32 tiles block the site.
Hostile turrets are checked separately against their actual native attack range
plus a two-tile margin to the complete service reservation; the bounded survey
uses the maximum installed turret range. A distant moving enemy outside weapons
range no longer blocks a protected rear radar. The controller uses a private proof of these existing registered posts
at preparation, immediately before construction, and during final proof. It
does not require unrelated global Perimeter refill or repair to complete and
does not build new gun posts. `radar_base` still reserves its exact owned plan.
An expensive radar is never used to establish an exposed eastern foothold.

Power evidence belongs to the exact existing source pole/network selected by
the ordinary pole planner. The native
`electric_network.parent_network.flow_last_tick` supplies currently offered
generation, required load and consumption satisfaction. The guard deducts the
larger of current demand and the measured five-second network flow, all known
unbuilt electrical loads, and the additional radar load. At least the larger
of 300 kW or twenty percent of offered power must remain. Accumulator discharge
is excluded from offered generation. A brownout or replaced source pole blocks
construction and proof. Electricity statistics are normalized per tick in the
[official flow-statistics API](https://lua-api.factorio.com/latest/classes/LuaFlowStatistics.html).

Read-only tick 3289894 verified those APIs on the actual 2.1.20 runtime. The main
network then offered 3.6 MW, with approximately 340.5 kW instantaneous demand
and 404.2 kW five-second flow. Two other sampled networks offered zero. These
were snapshot measurements; four installed engines do not promise unlimited
future capacity or sustained loaded generation.

Initial preparation may also reserve an Oil radar slot from at most eight
nearby defended candidates. It independently needs resource-free geometry,
clear power routing, both healthy protectors, enemy exclusion and a snapshot
power margin for both radars. Its `radar_oil_future` board block
contains the planned footprint and poles with `reserved_only=True`. The base
job never builds that second radar. If its checks fail, the result explicitly
defers the slot and still permits the proven base plan. Accepted plans remain
pinned. That optional reservation is a historical preview, not current defense
or reserved power evidence, and does not add a live prerequisite to base radar
installation. A later deployment needs its own fresh authorization and proof.

Commissioning requires exact saved world, player force, native units, names,
positions and directions for the installed radar and poles, connected power,
positive energy, native working status and positive `radar_scan_progress`.
Proof reports that progress and the actual charted count in a fixed nine-chunk
sample. The sample can contain previously explored chunks. It does not claim a
completed sector, a full-map scan, or that this radar charted every sampled
chunk. The runtime progress field is documented in the
[official entity API](https://lua-api.factorio.com/latest/classes/LuaEntity.html#radar_scan_progress).

Focused tests exercise ordinary paid planning, missing research, full body and
service geometry, future transport/port protection, planned power accounting,
local existing-post defense, unrelated global refill/missing posts, optional Oil deferral, replaced units and native working
proof. Executable flattened Lua cases reject nearby enemies, resource/mining
overlap, blocked service access, a damaged or under-armed protector, brownouts,
wrong networks and inactive scans. Tests do not establish live radar deployment.

Development preparation3936147 passed the native site guard at105.5,-27.5,
including exact registered covering guns11310 and6171. Earlier blanket80-tile
exclusion rejected distant fighting near the eastern rear line. Local32-tile
exclusion and actual worm-range checks retain protection while permitting this
rear installation. Actual powered scan proof succeeded at3941213: unit15071
at105.5,-27.5 was connected to network2, working, with scan progress0.284 and9/9
charted sample chunks. The same job retained84 actions and0 failures. Local
protectors11310 and6171 held5 and10 full magazines. Power3941197 offered3.6MW,
with1.099MW maximum consumption,2.041MW headroom and full satisfaction.
The radar's5333.33 energy field is stored joules, not kilowatts. Its installed
prototype rating remains300kW. This confirms one actual base radar, not complete
map coverage or a sustained scan-rate measurement.

Read-only validation at tick3,921,358 rechecked the accepted site at
`(105.5,-27.5)`: exact full-health posts11310 and6171 held five and ten full
normal magazines, covered the whole reservation, and had105 native walkable
service cells. Whole preflight passed; the native power guard retained about
2.244 MW headroom with a720 kW reserve. No radar was observed installed and this
validation performed no gameplay or checkpoint writes.

A later read-only nest survey can expose a bounded interface such as
`observe_enemy_sites(obs, bounds, max_sites=32)`. Each receipt should include
world, surface, observed tick, native unit, prototype, position and charted
status, using `(world, surface, unit_number)` as the stable identity. Ordinary
combat would recheck that identity before selecting a target. Radar planning
does not spawn enemies, change their health, destroy nests or chart by script.
