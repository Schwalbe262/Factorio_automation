# EXP-0006: PCB district and normal belt-fed smelting

- World/version/settings/model: same seed101/default Space Age2.1.20 world as
  EXP-0005; main develops, GPT-6 Luna medium requests/runs documented CLI jobs.
  This is an assisted development test, not an unseen autonomous scored run.
- Goal: fixed-port iron/copper furnace rows supplied entirely by the new ore
  banks and a separate coal bank. Preserve the existing operating power feed.
- Options: whole32x32 slots are surveyed for resources, existing structures,
  land and future expansion; reserve the complete accepted district before
  payment. The accepted area is [[-48,-40],[-16,-8]].
- Geometry:4iron/2copper furnaces from live recipe speed/3.2sec recipe duration;
  12ordinary inserters; straight input/output rows, separate ore/coal side-load
  lanes. An empty upstream stub keeps the coal junction straight, preventing
  a corner from placing coal on both lanes. Fixed east plate outputs.
- Logistics: one paid balanced splitter supplies both furnace banks. Whole
  corridors have hard port headings, at most6turns and length<=1.35*direct+4.
  Fixed2--4tile port escapes and bounded parallel-trunk/crossbar candidates
  solved a pole-blocked source escape without a maze fallback or wider detour.
- Crossings: preserve existing foreign surface belts; install actual straight
  underground pairs only at perpendicular crossings. Check span, roles,
  orientation, nearby pairing interference and accidental surface merges.
  Final proof checks native reciprocal partners, not endpoint placement alone.
- First preflight failed on the long coal connection. Short one-tile escapes
  were trapped by existing poles. Bounded complete-corridor candidates resolved
  it; no world mutation during failed preflights.
- Attempt1: Luna blocked smelting_reservation_changed after5actions, tick474554.
  Same tuple coordinates became lists on JSON reload. Saved plans were equal;
  normalize pair endpoints to JSON lists. Roundtrip regression added.
- Attempt2: ordinary construction progressed, but review found old global
  bootstrap upkeep had inserted8coal in each of6new furnaces. Safely stopped
  worker2456 at action boundary tick495960,67actions, zero executor failures.
  This attempt cannot count as independent automatic-fuel proof.
- Ownership fix: exclude the new district's registered furnaces from every
  bootstrap path, including an explicit starter request. Regression preserves
  ordinary starter upkeep while excluding owned automatic furnaces.
- Recovery: all6new furnaces had0products and0burning fuel before recovery.
  Normally take48coal back from fuel inventories; do not delete items or edit
  energy. Record a clean empty-fuel/zero-products baseline before continuing.
  If prior production/burning exists, block for explicit review instead.
- Validation gate: all planned entities/current powered inserters; every native
  furnace produces; plate belts contain outputs; distinct actual ore/coal lane
  contents; actual underground partners; no new manual fuel after clean baseline.
- Initial design inputs are nominal60iron ore/30copper ore per minute. Native
  output does not prove sustained rates while downstream output buffers fill.
- Final native outcome: same job succeeded,110actions/zero executor failures.
  The six normal takes recovered48coal before clean baseline tick505800. No
  further actor inserts into district furnaces occurred after that baseline.
  Proof tick517019: iron furnace products[4,3,3,2]=12; copper[3,3]=6; actual
  output belts held11iron/5copper plates. Actual lane contents were separated,
  two native underground pairs and one real splitter verified.
- Independent fresh proof tick520099 (including current powered-inserter checks)
  still passed: iron products[20,19,19,18]=76, copper[19,19]=38; output belts
  held46iron/24copper. Iron lanes contained52ore vs2coal, copper20ore vs14coal.
  Thus automated extraction/transport/furnace production continued after the
  clean baseline; do not confuse produced counts with belt inventory counts.
- Final area: [[-48,-40],[-16,-8]]. Final whole-world installed
  inventory:430surface belts,4underground endpoints,1splitter,58poles,12furnaces
  (six older starter furnaces),16ordinary inserters. Loaded steam generation
  was434.93kW at Luna observation,390.67kW at fresh proof.
- Limits: development interventions invalidate a wholly Luna-only history.
  Output buffers fill with no permanent consumer yet; no sustained60/30 output
  or30SPM proof. Old starter upkeep still funds construction and must eventually
  retire after permanent production/mall supply is proven.
- Next: load the fixed plate ports with permanent automatic red/green science,
  then mall/defense and broader processing. Rocket execution remains incomplete.
- Engineering visualization: tools/render_v2_board.ps1 and
  docs/v2/images/pcb-layout-current.png; diagram, not a game screenshot.

- Delivery usage sample: parent 45679186 -> 51829715 = 6150529 tokens incl cached input; weekly account 8.0% -> 9.0% (+1.0pp); child tokens and exact per-task weekly share unavailable.
