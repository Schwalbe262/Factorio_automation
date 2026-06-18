# Reference blueprints (user-provided, in-game proven)

`smelting_mining_reference_book.txt` — a Factorio blueprint BOOK (3 blueprints) the user gave as a
layout reference for the cell-layout v2 redesign:
- BP[0]: electric-mining-drill ore mining + belts.
- BP[1]: **stone-furnace smelting** (24 furnaces) — the proven 2x2 smelting geometry.
- BP[2]: larger stone-furnace smelting (48 furnaces) -> steel-plate.

Key geometry learned from BP[1] (decode with `factorio_ai.blueprints.decode_blueprint_string`):
- 2x2 furnaces are placed at HALF-INTEGER centers (e.g. x=-0.5, pitch 2.0) so their tiles align to
  the integer grid; inserters/belts are at integer positions. (Placing a 2x2 at an integer center
  mis-aligns its tiles -> inserters miss it; that was the iron-plate cell bug, P2.)
- DOUBLE ROW of furnaces (y=-2.5 and y=2.5) sharing a MIDDLE input belt (y=0) that carries ore +
  coal; each furnace's input inserter pulls from the middle belt, output inserter pushes to the
  OUTER belt (y=-5 / y=+5). Splitters + underground-belts implement the half-lane ore+coal feed.

## vanilla_production_blocks.txt (user reference #2, factorioprints -LOAVA6Unf_1BxVkbbSk)
"Vanilla Production Blocks" (v0.17, no beacons/mods) — 35 basic production cells. The GOOD basic
layout reference (the user's earlier "Production Book" -N8_DKC5AVE1L5tfVX4l is late-game/beaconed —
do NOT use it). Techniques to teach the layout engine:
- **Row sandwich (producer-outer / consumer-inner):** EC Compact-2R = rows [cable | EC | EC | cable]
  (36 cable : 24 EC = 3:2). Co-located intermediate producers on the OUTER rows feed the INNER
  consumer rows via inserters between adjacent rows — compact, direct, no long belts. Generalizes the
  direct_insertion archetype to multi-machine cells.
- **Fast inserters for throughput:** the cell uses fast-inserters (132 of them), not basic, to carry
  the high intermediate rate (e.g. 180 cable/min) that a base inserter (~57/min) can't. THIS is the
  fix for the single-machine rate limit — pick inserter tier by required rate + availability.
- **Underground belts + splitters for lane management:** copper-plate input bus distribution + EC
  output collection cross lanes via fast-underground-belt; splitters balance/merge.
- Tightly packed machine rows (3x3 touching) — no wasted rows (P3).
