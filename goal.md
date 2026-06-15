# Factorio Autoplayer Goal

## Mission

- Launch the first rocket through the no-custom-mod Factorio automation path.
- Keep the LLM at the strategic layer: choose goals, diagnose bottlenecks, critique layouts, and propose safe candidates.
- Keep deterministic skills responsible for movement, mining, crafting, building, recipes, power, belts, inserters, validation, and rollback decisions.
- Use the web dashboard as the operator surface for monitoring, manual correction, selected site improvement targets, token usage, and LLM/layout evidence.
- Use Codex-like implementation assistance only as the bootstrap engineering layer while deterministic skills and UI controls are still missing.
- Converge toward an autoplayer that can run the game with the local Qwen LLM as the only strategic model, using saved traces, tuned prompts, and Factorio-specific fine-tuning.

## Current Sprint

- Continue the red-science path until `logistics` research is complete.
- Stabilize automation science production and lab feeding before expanding the green-circuit line.
- Keep idle GPU cycles busy with simulation-only factory-site optimization while the player or deterministic executor is busy.
- Record every strategy/autopilot/layout loop in `note.md` and append only meaningful improvements to `insight.md`.
- Preserve layout, strategy, LLM decision, validation, and operator-intervention traces for GEPA prompt tuning and future Qwen LoRA fine-tuning.
- Preserve no-custom-mod compatibility for the primary development path.
- When Codex adds missing functions, keep the new capability deterministic, logged, test-covered, and exposed to the local Qwen strategy layer instead of leaving it as an operator-only shortcut.
- Keep the no-mod LLM autopilot running under local/remote Qwen whenever possible; Codex should diagnose and add missing functions, not manually drive each gameplay loop.
- Re-rank site layout candidates whenever newly researched, stocked, or automated items change factory geometry or ratios, including long-handed inserters, modules, beacons, better machines, rail logistics, and quality tiers.

## Autonomy Roadmap

- Bootstrap phase: Codex may add missing deterministic skills, planner checks, validation gates, trace exporters, and web UI controls because the local agent does not yet have enough tools to complete the rocket path alone.
- Transition phase: each Codex-added capability must become an ordinary tool the local Qwen strategy layer can select, evaluate, and retry through structured payloads.
- Target phase: the local Qwen model chooses the next objective, diagnoses blockers, proposes layout improvements, and keeps the game progressing without Codex deciding gameplay actions.
- Fine-tuning path: use preserved strategy traces, layout validation records, human intervention comparisons, notes, and confirmed insights to build Factorio-specific Qwen LoRA training data.
- Safety boundary: even after prompt tuning or LoRA fine-tuning, direct world mutation stays inside deterministic skills with validation and rollback evidence.

## Human Intervention Learning

- A real human player may add new factory blocks, move entities, repair production, or modify an agent-built site during a run.
- Human changes are not automatically assumed to be better; the agent should compare the previous agent design and the human-modified design using the same factory quality criteria below.
- If the human intervention improves measurable quality such as throughput, footprint, power use, pollution, bottleneck removal, site adjacency, expansion clearance, or robustness, record it as an accepted insight.
- If the intervention degrades the layout or only works because of manual carrying, record it in `note.md` as evidence but do not promote it to `insight.md`.
- Preserve before/after layout traces from human interventions because they are high-value examples for GEPA prompt optimization and Qwen fine-tuning.

## Current Status

- 2026-06-15 00:12 KST: the previous live map was backed up because the factory sites were too scattered for the automation-first logistics goal.
- A fresh no-mod world is now running with Nauvis cliffs disabled (`cliff_settings.richness = 0`), starter inventory only, and initial strategy `produce_iron_plate`.
- Next live objective: bootstrap iron, coal, copper, and steam power as compact starter-local sites. Do not place remote starter steam power unless the dependent factory site is co-located there or a reachable power/logistics corridor already exists; an isolated remote pump cannot power the starter base. After the first bootstrap phase, repeated inputs must move through site-to-site logistics lines rather than player inventory shuttle loops.
- Preserve the crashed spaceship/wreckage near the starting point by default. It is technically mineable, but the autoplayer should treat it as a protected landmark unless the operator explicitly overrides that rule.
- Iron and copper bootstrap must use direct burner mining drill -> stone furnace smelting cells, not repeated pickaxe mining of ore. Hand mining is reserved for unavoidable starter materials or one-off bootstrap fuel, not ongoing ore supply.
- Starter stone should use a burner mining drill outputting into a chest once a drill/chest can be built. Direct hand stone mining is only a fallback to bootstrap that stone supply.

## Factory Quality Criteria

- Compact footprint: prefer high output per tile and avoid scattered starter-era blocks.
- Throughput: no unresolved input, output, belt, inserter, or later train loading bottlenecks.
- Automation-first logistics: after the first bootstrap phase, repeated production must use belts, inserters, chests, pipes, trains, or later bots instead of player inventory shuttle loops.
- Ore extraction: copper and iron production should move to mining drills as soon as the relevant drill can be crafted or placed; do not continue pickaxe mining ore for normal plate production.
- Site adjacency: related producer/consumer sites should be close enough for short local belts until a main bus, trunk line, or rail network exists.
- Site graph and traffic: treat factory placement as a coupled producer/consumer graph, not isolated blocks. Co-locate high-coupling inputs and outputs unless a trunk belt, pipe corridor, rail station, or later bot network has enough capacity and expansion room; otherwise the factory will develop traffic congestion similar to badly separated city districts.
- Placement cost model: compare the build and operating cost of belts, pipes, poles, rails, stations, inserters, buffers, and future throughput risk before deciding whether to extend a line, relocate a factory site, or reserve a corridor.
- Unlock-aware layout optimization: candidate generation and LLM ranking must account for currently available buildings, inserters, modules, beacons, rails, quality tiers, and logistics tools. A design that was optimal before a new item unlock may become obsolete after that unlock.
- Power and pollution: prefer lower power draw and pollution for equivalent throughput, especially before defense is mature.
- Power expansion clearance: do not hard-ban factories near power blocks, but charge an explicit layout risk/cost when a factory consumes boiler, steam-engine, pole, water, fuel, or later power-upgrade expansion room.
- Expansion: leave clear lanes for belts, power, rails, modules, beacons, and replacement with higher-tier machines.
- Site safety: avoid starter resource patches, enemy pressure, disconnected power grids, preserved starting wreckage, and unvalidated remote logistics before rail.
- New world setup: for future Nauvis starts, disable cliffs because they make compact factory placement and corridor planning unnecessarily brittle.

## Learning Roadmap

- Collect structured traces from LLM decisions, strategy outcomes, layout candidates, sandbox validation, notes, insights, and operator interventions.
- Compare human/operator factory edits against the agent's previous site layout; accept them as reusable insights only when objective metrics improve.
- Use GEPA prompt optimization offline against saved traces before adding any live-mutating optimizer.
- Convert successful and failed layout/strategy traces into fine-tuning examples for a Factorio-specialized local Qwen LoRA.
- Keep exact gameplay execution deterministic even after prompt tuning or fine-tuning.
- Treat accepted human interventions as supervised improvement examples only when before/after evidence shows an actual factory-quality gain.
- Do not build repeated site-to-site belt paths or belt-fed smelting expansions before `transport-belt` production is automated by an assembler mall. Use direct miner-to-furnace/chest bootstrap cells first, then switch to belt smelting once belts are produced by assemblers.

## Later Milestones

- Green science, stronger mall automation, electric mining, and upgraded smelting blocks.
- Rail-based outposts, oil processing, blue science, modules, bots, and robust defense.
- Rocket silo and first rocket launch.
- Space platform, other planets, quality-aware rebuilds, and megabase-scale expansion.
