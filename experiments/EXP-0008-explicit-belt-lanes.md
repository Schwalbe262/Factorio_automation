# EXP-0008: observe lane identity and distribute smelter output

> Correction, EXP-0009: this successfully redistributed existing output but
> added no independent production bandwidth. The user rejected it as the
> production solution. Both output modules have since been normally recovered
> and replaced with paired furnace producer rows. Keep the results below as
> historical lane-observation evidence, not the current layout or acceptance.

- Date: 2026-11-11 UTC; same default2.1.20 development world as EXP-0007.
- User finding: iron/copper collection belts used only half the belt.
- Hypothesis: same-side furnace output inserters naturally fill only the far
  lane; lane identity must be explicit in layout contracts and acceptance.
- Native before: both old output ports at x=-17.5 had left0/right4 items.
  Inserter1029 direction0/drop(-39.5,-29.30078125) confirms the far-side offset
  on the eastbound iron collection belt. Aggregate belt counts hid this fact.
- Sources: official Inserters/Belt_transport_system wiki and FFF442/runtime
  entity docs, linked from docs/v2/BELT_LANES.md. Do not assume a splitter alone
  switches lane identity; do not assume flipping reaches the near side across
  a perpendicular belt.
- Implementation: deterministic straight-belt drop-lane helper, explicit lane
  metadata, native typed-port left/right observation. Reject generic underground
  mouths whose approach/exit turns would side-load or alter the intended path.
  Audited all4existing mouths: their approach/exit belts are straight and valid.
- Layout: preserve existing furnace rows and boundary ports. Add one small
  reserved output module per product: a normal balanced splitter and14belts,
  splitting flow then side-loading opposite sides of a straight output with an
  upstream stub. Register source/destination ownership on the board.
- Native whole-plan preflight:28belts/2splitters, no removals or natural clearing.
  Pure geometry checked in all4orientations. No custom vectors, grants or speedup.
- Luna first trial succeeded tick665299,16actions/zero failures. Iron output
  (-9.5,-29.5) left4/right4; copper(-9.5,-17.5) left4/right4. Source ports remain
  right-only by their declared collection policy. No intervention required.
- Independent fresh proof670056: both outputs still4/4;30entity directions
  verified. Splitters1550/1565 direction4, input_priority/output_priority none.
  Those native details are now included in the proof receipt rather than hidden
  behind a generic succeeded flag.
- Overall world after installation:532yellow belts,3splitters,4underground
  endpoints. Luna measured347.4kW network generation during its observation.
- Validation:58v2+38builder tests pass; native both-lane postcondition works.
- Result: the reported output-lane problem is addressed. This proves output
  distribution and accurate observation, not equal input-lane draw or sustained
  saturated throughput. Permanent science/labs and rocket automation remain next.

- Usage sample: parent 60016947 -> 63236564 = 3219617 tokens incl cached input; weekly account10% ->10% (display unchanged); exact task share and child tokens unavailable.
