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
