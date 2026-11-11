# EXP-0002 — Luna selects and executes initial capabilities

- Date: 2026-11-11 Asia/Seoul.
- Part: 1/2, live model interface and starter integration.
- Goal: have a fresh GPT-6 Luna medium select and run starter and power jobs
  using the documented CLI, without raw game commands or code edits.
- Runtime: `runtime/v2/dev-101`, seed 101, server UDP 34260, RCON loopback 27060,
  world `32a34974-d7cd-4ec0-9f35-13259ea32b8e`, Factorio/official mods 2.1.20.
- Rules observed: game speed 1; starting area 1; enemy/resource controls 1;
  peaceful false; cliffs interval 40/richness 1; pollution/expansion enabled;
  evolution time/destroy/pollution factors 0.000004/0.002/0.0000009.
- Model: fresh `gpt-6-luna`, reasoning `medium`, no inherited conversation.
- Frozen execution sources: `runtime/v2/dev-101/evaluated-code.json` SHA-256
  manifest. New unused planning/verification modules developed separately.
- Startup failures: a 0.3s connection timeout on an unused port was correctly
  treated as inconclusive; changed startup availability check to exclusive bind.
  Server then started. Nested Lua setting userdata serialized to null; observed
  scalar rules individually, without changing game settings.
- Interface issue: a shell's 10s yield was initially confused with command
  completion. Parent explained session_id polling, without supplying strategy.
  This is a developmental interface test, not final end-to-end acceptance.
- Starter result: succeeded at tick 20537. Natural steam-power/electronics/
  automation-science-pack unlocks; lab count 1; machine output iron 63 (63
  consumed), copper 48 (15 consumed). Fresh observe tick 20878 confirmed live
  further production: iron 64, copper 49, coal 57. No science packs yet.
- Power baseline result: unfinished/paused. 204 actions, no repeated executor
  failures, but material funding delayed a 140-belt coal plan. Planning time
  343.8568s, action execution 73.3605s. Final baseline tick 93559: 102 belts,
  iron 316 produced/310 consumed, 8 held. Parent safely stopped/paused after
  user requested regular PCB-style routing; no baseline power success claimed.
- Model reasoning issue: interpreted the immediate 45/48-iron subcomponent
  deficit as nearly finished, without a whole-project remaining material budget.
  The framework must expose that distinction and measured time-window rates.
- Improved result after EXP-0004 migration: same paused power job resumed and
  succeeded at tick 171961, verifying 1816 ticks of coal/steam flow, 8 coal on
  belts, 15 coal/min, water 200, steam 400, two connected energized engines.
  Native receipt still flags insufficient fuel for full engine capacity; idle
  generation was 0. This is not proof of loaded sustained power or science.
- Research trial 1: explicit no_powered_lab_site, no actions. Actual poles have
  network ID 1 but consumer connectivity predicate false. Fixed selection to
  associate pole network ID with energized generators; legal lab site verified.
- Research trial 2: lab built and Automation started; prematurely blocked when
  ten normally queued science crafts had reserved the finite allowance. Fixed
  queue waiting; regression test covers queued/delivered/exhausted states.
- Research trial 3: false allowance-exhaustion when the last pack was already
  consumed but its lab unit was still processing. Wait for native lab status
  working; finite budget remains unchanged. Regression covers this state too.
- Research final result: solved for the two startup technologies. Automation
  naturally completed, then electric-mining-drill succeeded at tick 215293.
  Total red science 35 normally crafted/35 normally consumed; no research flags,
  statistic edits or material grants. Native network 1 generated 60kW under lab
  load (e.g. tick 209556), cumulative energy 15.499MJ then 21.002MJ after finish.
  Idle output returns to zero normally. Current observer exposes these metrics.
- Verification: job safety/recovery and sustained-flow unit tests; live starter
  effects as above. Fixture tests do not constitute rocket evidence.
- Remaining: power, scaling, science/mall/defense, oil, rail and rocket execution.
- Next: automatic science/mall/defense and scalable extraction. A new Luna
  starter-capacity development test may be active; inspect its current handle.
  Rocket execution remains missing.
- Tokens: sample parent and evaluator counters separately at part completion.

- Delivery usage sample: parent thread 3193689 -> 30059441 = 26865752 tokens including cached input; weekly account meter 4% -> 6% (+2pp). Concurrent child token counters unavailable; not an exact per-part quota percentage.
