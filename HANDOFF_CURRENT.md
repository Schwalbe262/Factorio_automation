Claude owns development since 2026-11-13. Read AGENTS.md, then this file. CLAUDE_HANDOFF.md is the older Codex handoff (history only).
Approved restructure plan, in order: P0 baseline (DONE) -> P1 legacy absorbed into factorio_agent/core, v1 deleted, ruff format (DONE) -> P2 long-lived kernel (one RCON session, cached world, batched actions, no CLI child per step, metrics) -> P3 lane scheduler + capability registry -> P4 Luna interface v2 (goals, event-driven calls, autopilot default) -> P5 global site plan (sitemap, allocator, ratio-sized blocks, offline layout harness) -> P6 fresh-map 60 min runs -> P7 docs for mid-tier models.
P1 pushed through 2a8a8b7. Clean checkout 1585 run/10 skips PASS. Goal: rocket launch first, reproducible on other seeds, built stages replay fast after restart.
User decisions: validate on a NEW map (ask before first creation); keep runtime/v2/eval-3083327785 untouched, do not resume its smelting job; Luna prompt/schema may change, model/CLI pinning may not.
Baseline to beat (old map, 84.6 min): 823 actions, 43% of time in >30 s gaps, planning/execution 5.5, 84 Luna calls at 26k input tokens.
Targets for a 60 min run: gaps <=10%, planning/execution <=1, >=40 actions/min, <=30 Luna calls at <=8k tokens, red+green science automated.
Uncommitted and frozen: advanced_petroleum, fluid_products, fluid_distribution, defense_ammo_policy, post_ammo_transfer, post_raid_recovery, registered_asset_repair (+tests/docs), oil_gathering/water_gathering source_key edits. Do not stage broadly, reset or clean.
Before every commit, from PowerShell: powershell -ExecutionPolicy Bypass -File tools/check.ps1 (ruff check, format check, all test_v2*). Pass module names to run a subset.
Never grant items/research or claim game progress from unit tests.
NEXT: Part 2 kernel (src/factorio_agent/kernel).
