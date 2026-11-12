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
all four service sides to the outside of that reservation. Whole preflight also
checks every body, service and new pole tile against ores and mining growth.
Other production reservations, shared bus reservations, unbuilt external
hardware/tunnels and four outward cells at fixed typed ports remain protected.
Existing poles used by the connection carry exact saved identities and are not
adopted as radar-owned assets.

Placement considers at most sixteen base sites, derived from pairs of actual
canonical perimeter posts near existing base machines. Observed full health
filters canonical post pairs before ranking; ammunition remains a fresh native
check. The bounded generator checks at most thirty-two post pairs and 16,384
distinct half-tile centers. It
filters complete service reservations, future/port keepouts and both native
defense disks before applying the sixteen/eight site cap, so reserved midpoints
do not crowd out valid exterior sites. Every accepted site
requires two separate full-health gun posts, each covering the complete radar
reservation with the canonical two-tile range margin and holding at least five
full normal magazines. Any enemy unit, nest or worm within eighty tiles blocks
the site. The controller also obtains and later ensures complete canonical
perimeter coverage. It registers `radar_base` before that coverage can place
new posts, so normal defense planning protects the radar and its service space.
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
clear power routing, both healthy protectors, enemy exclusion, canonical
coverage and power margin for both radars. Its `radar_oil_future` board block
contains the planned footprint and poles with `reserved_only=True`. The base
job never builds that second radar. If its checks fail, the result explicitly
defers the slot and still permits the proven base plan. Accepted plans remain
pinned; a later outpost deployment needs its own fresh authorization and proof.

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
both defense gates, optional Oil deferral, replaced units and native working
proof. Executable flattened Lua cases reject nearby enemies, resource/mining
overlap, blocked service access, a damaged or under-armed protector, brownouts,
wrong networks and inactive scans. Tests do not establish live radar deployment.

A later read-only nest survey can expose a bounded interface such as
`observe_enemy_sites(obs, bounds, max_sites=32)`. Each receipt should include
world, surface, observed tick, native unit, prototype, position and charted
status, using `(world, surface, unit_number)` as the stable identity. Ordinary
combat would recheck that identity before selecting a target. Radar planning
does not spawn enemies, change their health, destroy nests or chart by script.
