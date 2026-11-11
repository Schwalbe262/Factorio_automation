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
