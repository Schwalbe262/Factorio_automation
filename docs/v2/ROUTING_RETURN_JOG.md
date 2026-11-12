# Bounded source return jogs

`route_trunks` keeps the two established candidate families and their ranking.
Only when both fail does it consider a third family for parallel typed port
headings: a straight source escape of one to four tiles, a perpendicular jog of
one to four tiles, a return of one to four tiles, and complete clearance trunks
to the destination. Coordinates come from port geometry, the existing absolute
grid and obstacle edges. No factory or map coordinates are embedded.

The final trunk columns are the nearest sixteen whose complete destination
approach is clear. Indexed checks reject blocked local prefixes and complete
polylines before allocating tile paths. All three families share one expansion
counter and candidate deduplication set. The original node budget, six-turn
limit, `1.35 * Manhattan + 4` length limit, crossing compiler and final native
preflight remain in force. Existing accepted routes return before this family
is generated.

An offline replay of the read-only Engine iron survey at tick 3,118,771 found a
122-tile, six-turn route (Manhattan 106, ratio 1.15094). Its layer compiler
preserved the existing belts and underground pairs. The replay used 211,904
actual expanded nodes within the original 214,000-node budget. This is planner
evidence; whole native preflight and live production are separate gates.

Read-only whole Engine replan subsequently passed at tick 3,154,337 in 12.266
seconds. The 360-entity alternative retained steel112/4turn and iron122/6turn
feeds, exact foreign units7505 and7928..7936, and preserved crude pipes6905/6801
with their reciprocal peers. Whole/future native preflight, power, existing
coverage and the corrected core lane audit passed. Only old Engine posts7152
and7154 were ignored for their separately guarded future relocation. The
preview did not publish runtime metadata, pay for construction or recover posts.

Focused regressions cover rotations and mirroring, real underground crossing
constraints, unchanged legacy paths, turn/detour/hard-cell rejection, and the
sum of actual expansion lengths across families, including budget exhaustion.
