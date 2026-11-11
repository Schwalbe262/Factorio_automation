# Factorio Space Age Autoplayer v2

## Mission
Build a reusable execution framework that GPT-6 Luna medium can use to launch
a real Space Age rocket autonomously. Long-term: win at the solar system edge.
Main-session high-capability models develop/debug; the evaluated Luna receives
only the framework interface and its documented state, with no parent decisions.

## Rules
- Pin installed Factorio/Space Age 2.1.20 and export its actual game data.
- New isolated world, default resources/enemies/pollution/evolution/cliffs.
- RCON direct construction allowed; ordinary material costs are mandatory.
- Engine crafting, machine production, science consumption and launch are real.
- Never grant materials, unlock research by script, or edit progress counters.
- Preserve all v1 saves and uncommitted changes. One executor owns each world.
- Use PCB-style production zoning, fixed typed ports, reserved orthogonal trunks,
  controlled branches/crossings and expansion space; reject snake-like layouts.
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
now pass native consumer preparation. Engine construction stopped before its
core because two newly commissioned posts obstruct a feed and its fixed input
apron. Normally paid, armed replacements must preserve coverage before old
posts are recovered; this guarded relocation remains under development.
The independent shared gas header is commissioned with exact Plastic/Sulfur
aliases and one shared540/min nominal source. Future defense placement now
protects complete registered plans, expansion spaces and fixed port exits.
Plastic full native preparation2968151 passed13.11s; actual construction is
in progress. Next finish these consumers, mall/repair, blue, rail and rocket.
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
