# EXP-0005: machine science and paid electric mining

- Date: 2026-11-11. Development world seed101, Space Age2.1.20, default
  enemies/cliffs/resources, speed1, world32a34974-d7cd-4ec0-9f35-13259ea32b8e.
- Goal: naturally unlock underground belts/splitters, remove the dedicated
  coal source's burner upkeep, and fund later PCB production with aligned mines.
- Hypothesis: bounded normally funded machine production and fixed mining
  templates let GPT-6 Luna medium request useful jobs without individual actions.
- Machine layout: reserve16x16 at [[24,-8],[40,8]], two AM1 machines, four
  ordinary inserters, fixed iron/copper/output buffers, normally connected poles.
  Whole prospective geometry/native placement is checked before construction.
- Funding: finite40 iron plates +20 copper plates from existing ordinary stock.
  Machines produced20 red packs; native labs consumed20 and unlocked logistics
  at tick324072. Total red statistics55 produced/55 consumed include the earlier
 35 normally handcrafted starter packs. No additional handcrafted science.
- Result: this bootstrap bridge works. It does NOT provide automatic raw supply,
  30SPM, or permission for unbounded actor-fed science. Actual loaded generation
  measured66.6--141.6kW; idle machines6.6kW.
- Coal upgrade: prebuild19 paid pole hops; conserve boiler/belt reserves; recover
  the exact owned burner drill; normally craft/install electric drill956 at
  (-48.5,14.5), facing east into the preserved belt. Recover the obsolete fuel
  inserter only after native output. No grants, time acceleration or new world.
- Failed attempt: construction worked but independent proof reported
  postcondition_not_verified when a mined item moved off the sampled belt tile
  between checks. This was a temporal evidence bug, not failed mining.
- Improvement: retain tick/world/entity-bound native output for at most600ticks;
  independently require the same currently powered drill and receiver. A unit
  regression checks visible output, normal movement, and evidence expiry.
- Luna fresh-key recheck succeeded tick344894 without rebuilding. Coal observed
  at344787, powered drill956, retained belt,19wire hops. Generation96.6kW under
  drill load. This does not establish full-load coal sufficiency.
- Mining extension: additive aligned drill rows, shared straight collector,
  typed east output, catalog-derived nominal rate, pure-resource survey and full
  native placement/power preflight. Existing banks remain during expansion.
- Luna sequential development tests all succeeded: iron2drills982/983,
  nominal60/min, native5ore tick411413; copper1drill989, nominal30/min, native3ore
  tick415807; separate coal1drill1006, nominal30/min, native3coal tick419489.
  No failed actions. Final five electric drills include the preserved power
  source. These receipts prove output and design capacity, not sustained rates.
- Diagnostics: timed engine-counter rates replace lifetime-count rate claims;
  saved-plan material budget is distinct from the immediate component deficit.
- Remaining: sustained ore/smelter flow, permanent automated red/green science,
  construction mall, defense, oil, rail and rocket execution are incomplete.
- Tokens: measured parent interval is recorded at delivery. Cache-inclusive
  tokens differ from uncached reasoning tokens; child counters are unavailable.

- Delivery usage sample: parent 32144956 -> 45438772 = 13293816 tokens incl cached input; weekly account 6.0% -> 8.0% (+2.0pp); exact task quota and child tokens unavailable.
