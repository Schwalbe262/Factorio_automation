# EXP-0007: additive source expansion and stable smelting ports

- Date/settings: 2026-11-11 UTC, same default Space Age2.1.20 seed101 development
  world32a34974-d7cd-4ec0-9f35-13259ea32b8e; GPT-6 Luna medium CLI evaluation.
- Goal: provide supply headroom for permanent science while preserving the
  operating mines, paid corridors and future production block interfaces.
- First option: grow a mining row upstream while keeping its output fixed.
  Pure geometry preserves all previous entities and the port. Native preflight
  rejected this world: old starter furnaces/drills occupy the extension.
- Selected option: an additional aligned one-drill bank, connected to the owned
  same-resource smelter trunk through an explicit perpendicular side-load.
  Bind the destination belt's identity, direction and item; preserve the old
  source and transport. No disconnected capacity can satisfy this expansion.
- Port issue: otherwise valid new mines could face adjacent legacy machinery.
  Reserve two clear tiles beyond the output before selecting the bank site.
- Corridor issue: iron's clean two-turn corridor needed one dead tree removed.
  Native whole-plan survey approves only neutral minable trees/ordinary rocks;
  world-bound executor uses normal mining, never destroy/remove by script.
  Native query failures cannot masquerade as approved clearance.
- Luna iron90 target succeeded: existing982/983 preserved, added1368; native
  output4 at596623. New19tile/two-turn route, detour1.1176, enters stable belt1119
  at(20.5,-61.5). New copper60 succeeded:989 preserved, added1389; native output3
  at600929. New15tile/two-turn route, detour1.1538, enters1217 at(13.5,-31.5).
  No failures. These are nominal aggregate ore capacities, not measured rates.
- Smelting: add machines inside the original six-slot rows. Fixed east boundary
  outputs are iron(-17.5,-29.5) and copper(-17.5,-17.5). A once-only initial
  port migration is allowed here because no consumers were registered; later
  connected port changes require a deliberate cutover. New consumers will
  register their source ports on the board.
- Luna iron90 target: five furnaces, success610612,17actions/zero failures.
  Copper45 target: three furnaces, success615227/zero failures. The requested
  targets require five/three18.75plates/min furnaces (93.75/56.25 nominal machine
  capacities), with90/60 nominal connected ore supply.
- Native final proof: all eight district furnaces produced; distinct input
  lanes iron ore68/coal68 and copper ore36/coal36; existing splitter and two
  underground pairs remain valid. New furnaces use automatic fuel; clean
  baseline505800 retained. Overall14furnaces/20inserters/504surfacebelts.
  Loaded generation469.45kW. Counters filling buffers are not sustained rates.
- Validation:54v2 and38legacy builder tests pass. Regressions cover preserved
  mining output ports, six-slot smelting port stability, normal natural mining
  and rejection of native preflight/world failures.
- Remaining: permanent science/lab consumption, mall, defense, oil, rail and
  rocket execution. Development-assisted results are not a full Luna-only run.

- Usage sample: parent 53002727 -> 58742039 = 5739312 tokens incl cached input; weekly account9% ->10% (+1pp); exact task share and child tokens unavailable.
