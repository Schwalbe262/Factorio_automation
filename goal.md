# Factorio Space Age Autoplayer v2

## Mission
Build a reusable execution framework that GPT-6 Luna medium can use to launch
a real Space Age rocket autonomously. Long-term: win at the solar system edge.
Main-session high-capability models develop/debug; the evaluated Luna receives
only the framework interface and its documented state, with no parent decisions.

## Rules
- Pin installed Factorio/Space Age 2.1.20 and export its actual game data.
- New isolated worlds keep default resources/enemies/pollution/evolution.
  Future Nauvis maps disable cliffs by user request; dev-101 retains its original
  generation settings. Do not alter an existing world's terrain to apply this.
- RCON direct construction allowed; ordinary material costs are mandatory.
- Engine crafting, machine production, science consumption and launch are real.
- Never grant materials, unlock research by script, or edit progress counters.
- Preserve all v1 saves and uncommitted changes. One executor owns each world.
- Use PCB-style production zoning, fixed typed ports, reserved orthogonal trunks,
  controlled branches/crossings and expansion space; reject snake-like layouts.
- Extend the shared main bus beyond iron/copper with dedicated expandable bulk
  steel and electronic-circuit producers, then other frequently shared materials.
  Budget actual source production, per-lane throughput and parallel belt capacity;
  priority taps must preserve upstream consumers. Keep expanding-volume cable local.
- Maintain radar coverage, normally repaired wall-backed mutually covering turrets,
  and ordinary damage/fire-rate/productivity upgrades. Repeated losses require
  reinforcement. Remove dangerously close nests using paid native projectile combat.
- Nearby eastern cluster priority was completed at native4156939: six spawners
  and four worms destroyed by one normally funded ten-turret projectile wave.
  Continue monitoring future nearby clusters: spawners replenish local units
  independently of pollution. Keep the paid wall backstop armed, then use
  bounded prepared native combat when another cluster requires removal.
- Latest combat steering: keep an armed rear cover, then accept ordinary worm
  hits while rapidly placing enough guns where they can actually hit spawners.
  Commit a prepared concentrated push; never sustain nest-adjacent harassment or
  incrementally rebuild offensive posts. A group's complete stock, fixed target
  coverage and paid deployment must be ready before the attack begins. Actual
  native projectile deaths remain the success evidence. In default rules, time,
  pollution and destroyed spawners affect evolution; ordinary unit kills do not.
- Radar is an active installation priority: normally paid, powered, defended
  coverage must expose the growing factory and support threat observations.
- User instructions are persistent automatic policies, not one-time interventions.
- Radar placement should cover every owned production, mining, utility, transport
  and defensive footprint with actual continuous nearby-sector visibility. Expand
  coverage as the factory grows; charted history alone is not current visibility.
- With genuinely spare resources, add normally paid solar generation on broad
  off-ore outer sites that preserve factory growth. Retain night supply and
  include radar load, storage requirements and native power headroom in decisions.
- Automate recurring belts, underground belts, splitters, assemblers and inserters
  through normally supplied capped Mall cells, automatically replenishing stocks
  consumed by construction. Small bootstrap handcrafts must not become bulk stock.
- Normally recover exhausted owned electric drills only after proving no mineable
  resources remain in their full native mining area; retire their source claims
  so upkeep cannot rebuild them. Track patch reserves against consumption and
  prepare external mines and paid railway transport before starter patches empty.
- Completed nest sweeps include safe post-combat repair and recovery of raid guns
  and their remaining ammunition for reuse, with exact item/round conservation.
- Fresh-run performance target: reach the current Sulfur/Engine/Steel-extension
  milestone in60 real minutes, at ordinary game speed. This is not yet validated;
  measure construction/material waits/research/repairs separately on a clean run.
  Declare material-flow, producer-row and expansion directions before placing a
  district; preserve fixed ports and reserve growth, service, power and defense
  corridors. Existing accepted layouts require an ordinary migration to rotate.
- Mining banks use a shared patch grid, straight collectors, opposing drill rows
  feeding both physical belt lanes and bounded off-ore exits. Judge expansion by
  active ore, power and per-lane production, not installed machines or splitters.
- Normally supplied construction malls make recurring building materials and
  ammunition, with native chest limits and circuit stock stops. Prefer piercing
  ammunition after its native unlock; gather stock and rapidly deploy a prepared
  turret group for assaults. Refeed recovered surplus to verified consuming
  factories while preserving construction/combat reserves and stock caps.
- Bulk ammunition demand such as200 magazines requires an actual normally
  supplied assembler factory. Do not repeatedly handcraft red or yellow raid
  stock. Honor existing paid stock/queues; new stock comes from machine output.
  Reserve precursor capacity for the red line so existing base upkeep cannot
  consume its only yellow supply. Report finite batch logistics separately from
  sustained automatic belt supply and enforce real inventory stock limits.
- Early factories and transport trunks avoid resource patches. Permit only
  explicit local mining footprints and minimal bounded surface exits; prebuild safe
  replacement corridors before moving existing supplies off ore. Do not silently
  relax this policy for later expansion.
- Never claim a full-game or rocket success from unit tests or fixture saves.

## Parts and gates
0. Consolidate v1/v2 branches/worktrees; document architecture and experiments.
1. Isolated runtime, observation, catalog, typed resumable jobs, Luna interface.
2. Continuous mining/smelting/coal and automatic steam power; real live proof.
3. Construction mall, red/green science each 30/min for five game minutes,
   ammunition production, replenishment and repair.
4. Expandable districts, underground/splitter routing and supply fault recovery.
5. Oil/blue science, coproduct handling, rail mining outpost and real transport.
6. Silo, normally produced rocket parts and platform starter pack, real launch
   counter increase and natural platform arrival. Luna-only end-to-end runs.

## Evaluation
Current sprint (2026-11-12): independent paired producers retain fixed ports
(EXP-0009); permanent red and green production reaches all eight labs through
ordinary belts (EXP-0010/0011). Loaded second steam pair, installed iron ore360
and plates225/min, and eight normally armed strategic posts are commissioned.
The user manually repelled a destructive enemy attack; this is human assistance.
Shared iron/copper main bus and compact32x28 green migration are commissioned,
and the old western hardware retired (EXP-0012). Construction
stock collection now pools live sources instead of repeatedly taking empty
moving belts. Green raw branches now avoid the iron patch without moving the
compact production core; new dedicated coal supplies independent normal inputs.
Shared resource keepouts reject new non-mining construction on ore. Remaining
green/steam-coal and foreign-copper-crossing iron corridors have been migrated
with native production proofs (EXP-0014). One iron drill exhausted its patch;
remaining source capacity dropped to330/min, then a defended additional pair
restored390active/420installed (EXP-0016). Stone60nominal and automatic bricks
are commissioned, with new paid coverage posts. A surplus iron bus outlet is
ready; normally paid shared brick-coal header and steel production are commissioned.
These are assisted development outcomes, not a Luna-only score.
Eight paid steel-furnace replacements now provide375iron/min catalog capacity;
native production and both lanes are verified. Automatic magazines, basic
petroleum, independent chemistry coal and water are commissioned (EXP0017).
Forty actual full factory magazines were transferred with conserved round totals.
Full-rated red36+green35 requires264.5iron/min before additional industrial demand;
ammo's bounded critical fill and surplus industrial buffers can temporarily reduce
science supply. Neither nameplate budgets nor full buffers certify sustained rates.
Normal underground-belt pipe crossings and whole producer-reservation keepouts
now pass native consumer preparation. Paid, armed replacements preserved cover
while obstructing old posts were normally recovered. An append-only continuation
preserves the first historical mine's unknown baseline; the second retirement
conserved ten magazines with97 actual rounds, including equipped ammunition.
The independent shared gas header is commissioned with exact Plastic/Sulfur
aliases and one shared540/min nominal source. Future defense placement now
protects complete registered plans, expansion spaces and fixed port exits.
Plastic is commissioned2991431. Advanced circuits are commissioned3100796
after a paid one-belt mixer repair and conserved recovery of83copper plates.
The shared analyzer now distinguishes corners from true side loading; Engine
and Blue designs include the same required empty approach. Blue recipe research
completed3104834; no Blue factory is commissioned yet. Engine is commissioned
4326379 on its original key with148 actions. All360 bound assets match and two
engine machines have23 actual finished cycles each, with four units at the fixed
outlet. Its Steel source proof pins the original commissioned furnace units and
permits normally consumed empty output buffers; first Steel commissioning still
requires both real output lanes. WaterDistribution is commissioned4584058 with
31 normal actions: outlet16713 at89.5,-57.5 has real water and28 reciprocal
connections, while original outlet6989 remains fixed. A same-consumer planned
fluid underground crossing preserves foreign/resource/future hard cells. Sulfur
is commissioned4780144 on its original key with134 actions and zero failures.
Watch normally restored six lost crude/gas pipes at4766812, spending six real
pipe items and recording new units16915–16920,364 reciprocal edges and owner
publication; original ports6963/7228/7231 stayed fixed. Native4777771 found real
gas at all those ports and45 sulfur cycles. Blue's initial job is blocked with
zero actions by an AdvancedCircuit belt fault at182.5,-40.5; its complete owned
layout is under inspection. No fabricated chemistry progress.
Watch maintains
the remaining perimeter: it restocks/rebuilds known posts and
ordinary defense research has unlocked walls, radar and repair packs. Three-post
wall bastions are commissioned with24 paid walls and three armed posts. Native
repair receipts prove real health gain and tool wear. The exact pinned player's
successor10915 is rebound after actor11's death;4103 corpse items were recovered
with conserved rounds/durability, one spare repair tool remains in the corpse.
The transport-belt mall is commissioned4561808 with67 actions. Native4600230
confirmed100 normal belt craft cycles and200 actual chest belts, with zero
internal product WIP and the native200 cap. Guarded stock collection supplied
eight actual belts to Steel expansion at4799204: requested8/moved8, with no
handcraft fallback. Steel expansion4810835 retained285 old units and output6213,
added two normally paid furnaces and proved each produced steel. Its15/min
catalog rating is not a sustained measurement. Engine's fresh4818782 source proof
accepted the additive receipt chain without rewriting its original source pins.
Original coal drill956 exhausted its patch. Automatic recovery shared one fresh
66/min-potential bank between both boilers (54/min max), normally recovered two
exact belts and preserved279 units. Four-engine generation and automatic fuel
were sampled30 game seconds; offered power returned to3.6MW at4556084. The old
source is retired history, and source/route publication has durable five-file CAS.
Sustained production rates remain unproved.
One normally paid, defended radar is working at105.5,-27.5, native3941213 with
positive scan progress. The installation gate uses fresh local coverage,
walk access and actual hostile ranges, without waiting for unrelated global
refills. User-rejected staged assault is retired; one concentrated prepaid wave
replaces it. Native4156939 accepted all six nearby spawners and four worms killed
by the single paid ten-gun wave, with no turret losses. The capped2Yellow/4Red
factory is commissioned4137210 and independently held200 output at4174070;
raw delivery remains assisted finite couriers rather than continuous belt supply.
Finish construction malls, Sulfur/chemical consumers, bulk bus growth, blue,
rail and rocket. No enemy deletion, granted health or retroactive conservation claim.
Do not close the continuous-rate gate from filling output buffers. No rocket yet.

Freeze code, model/effort, seed/save and settings per attempt. Classify failures
as model/observation/executor/missing capability/environment. Compare target-only,
diagnosis and candidate-choice interfaces. Repeat fixed development seeds and
unseen seeds. Separate game time, wall time, model waiting, planning and execution.
A parent intervention invalidates Luna-only acceptance for that attempt.

## Delivery and memory
Commit/push each completed part to v2; retain v1 as historical development.
HANDOFF_CURRENT.md stays at most ten lines. note.md is append-only; never read
it or insight.md in full. experiments/ holds concise per-attempt Markdown with
hypothesis, changes, metrics, outcome (including unresolved), evidence, next step
and tokens. insight.md contains only confirmed reusable improvements.
Report measured per-part tokens and account weekly meter changes separately;
never invent a token-to-weekly-quota conversion.
