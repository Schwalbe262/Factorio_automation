# Factorio Autoplayer Goal

## Mission

- Launch the first rocket through the no-custom-mod Factorio automation path.
- Keep the LLM at the strategic layer: choose goals, diagnose bottlenecks, critique layouts, and propose safe candidates.
- Keep deterministic skills responsible for movement, mining, crafting, building, recipes, power, belts, inserters, validation, and rollback decisions.
- Use the web dashboard as the operator surface for monitoring, manual correction, selected site improvement targets, token usage, and LLM/layout evidence.

## Current Sprint

- Continue the red-science path until `logistics` research is complete.
- Stabilize automation science production and lab feeding before expanding the green-circuit line.
- Keep idle GPU cycles busy with simulation-only factory-site optimization while the player or deterministic executor is busy.
- Record every strategy/autopilot/layout loop in `note.md` and append only meaningful improvements to `insight.md`.
- Preserve no-custom-mod compatibility for the primary development path.

## Factory Quality Criteria

- Compact footprint: prefer high output per tile and avoid scattered starter-era blocks.
- Throughput: no unresolved input, output, belt, inserter, or later train loading bottlenecks.
- Power and pollution: prefer lower power draw and pollution for equivalent throughput, especially before defense is mature.
- Expansion: leave clear lanes for belts, power, rails, modules, beacons, and replacement with higher-tier machines.
- Site safety: avoid starter resource patches, enemy pressure, disconnected power grids, and unvalidated remote logistics before rail.

## Learning Roadmap

- Collect structured traces from LLM decisions, strategy outcomes, layout candidates, sandbox validation, notes, insights, and operator interventions.
- Use GEPA prompt optimization offline against saved traces before adding any live-mutating optimizer.
- Convert successful and failed layout/strategy traces into fine-tuning examples for a Factorio-specialized local Qwen LoRA.
- Keep exact gameplay execution deterministic even after prompt tuning or fine-tuning.

## Later Milestones

- Green science, stronger mall automation, electric mining, and upgraded smelting blocks.
- Rail-based outposts, oil processing, blue science, modules, bots, and robust defense.
- Rocket silo and first rocket launch.
- Space platform, other planets, quality-aware rebuilds, and megabase-scale expansion.
