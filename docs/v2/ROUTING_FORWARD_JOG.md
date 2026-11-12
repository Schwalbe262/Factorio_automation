# Fourth routing family and required straight source prefixes

`route_trunks(..., minimum_start_run=None)` preserves the first three routing
families and their existing preference/ranking. The new fourth family is
generated only when all three fail to produce an accepted whole corridor.
No existing accepted route changes when the new option is omitted or `None`.

For parallel cardinal port headings, the fourth family considers:

1. A straight source escape of one to five tiles.
2. A perpendicular local jog of one to four tiles in either direction.
3. A forward middle column from the existing absolute grid or native obstacle
   clearance coordinates, followed by a complete perpendicular trunk.
4. A fully clear crossbar, end column and final destination approach.

The complete source prefix and middle trunk are checked against indexed hard
cells before selecting end columns. Up to sixteen nearest end columns are kept
**after** crossbar/end-tail clearance and the ordinary quality checks. Middle
columns are not prematurely truncated to sixteen candidates. No map coordinates
or source-specific waypoints define this family. Rotation/reflection cases use
the same code. Candidate rejection is explicit; no tile-search maze fallback is
introduced.

When supplied, `minimum_start_run` must be an actual integer in `1..8` with a
valid cardinal start heading. Boolean/float/string values, missing headings and
coincident endpoints fail validation. Every family's canonical first straight
segment is checked **before path expansion**, so a known invalid native source
prefix cannot consume expanded-node budget. This filter grants no collision,
resource, reservation, underground, heading or endpoint exception.

The fourth family permits a six-tile escape only for an explicit six-tile source
prefix requirement. Values seven/eight may constrain the older families' longer
straight segments; they do not create new seven/eight-tile fourth-family escapes.
The caller derives the required run from its verified native source/apron and
underground mouth geometry, rather than hardcoding a water-specific exception.

All candidates retain the existing bounds, maximum six turns and
`length <= 1.35 * Manhattan + 4` gate. The same crossing-layer/native validator
must accept the whole candidate. All four families share **one actual expanded
node budget**; it is never reset or doubled. Indexed skeleton rejection does not
walk/materialize path cells, and every materialized node counts toward `visited`.

Offline captured-map benchmark (native snapshot tick3117605, using current
authoritative unbuilt-plan keepouts and a prospective core shifted upward28):

| Result | Value |
| --- | ---: |
| Route length / Manhattan | 202 / 150 tiles |
| Turns / ratio | 6 / 1.346667 |
| First straight run | 5 tiles |
| Actual expanded nodes, independently counted | 22,212 of 200,000 |
| Fourth-family candidates | 108 |
| Fourth-family generation | 0.0378 seconds |
| Complete offline route selection | 0.8596 seconds |

The selected middle column and crossbar came from generic geometry. The pipe
compiler accepted the underground crossings, the exact captured source-apron
prefix, and normal source/first-neighbor pipes. Reusing the original core on the
same captured native map still failed (zero expanded nodes, zero fourth-family
candidates; 0.9101 seconds). The detour gate was unchanged.

This benchmark proves captured-map planning only. New core terrain, gas feeds,
power, coverage, prospective foreign assets and native whole-pipe connectivity
still require a fresh whole Chemical preparation; snapshot coverage does not
certify terrain outside the captured survey.

Nine portable focused tests cover the fourth-only fixture, unchanged older
accepted routes, all rotations/reflections, turn/length/bounds/layer/node-budget
failures, strict prefix validation, no expansion of invalid prefixes, explicit
six-cell opt-in and clearance before the sixteen-end-column limit. The optional
manual `captured_water_case` helper reads local ignored snapshot/catalog/state
files; automated tests do not depend on those files or mutate the game.
