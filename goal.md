# Factorio Deterministic Autoplayer

## Mission
- Build a standalone algorithmic player; no local or cloud LLM calls during gameplay.
- First milestone: launch a real Space Age rocket from a fresh Nauvis world, delivering a normally manufactured platform starter pack.
- Long-term direction: extend the same surface-aware production and logistics system to other Space Age planets.
- Codex develops and tests reusable deterministic capabilities and parameterized factory designs.

## Agreed Run Rules
- Preserve all old saves. Each new run uses an isolated runtime directory and world identity.
- Use the installed official Space Age mod set and current live recipe/technology data.
- Keep default enemies, pollution, evolution, resources and starting area. Disable cliffs.
- Main backend: assisted RCON; simplified movement and initial gathering are permitted, with normal resource costs.
- Machine production, engine handcrafting, prerequisite research, science consumption and rocket construction must occur normally.
- Never grant production materials, set researched/enabled flags, manufacture rocket parts by script, or edit launch/production counters.
- Initial inventory matches normal freeplay and is granted once for the dedicated character only.
- Secondary character backend obeys movement, mining, crafting time and ordinary reach; validate through initial automated power/science in this milestone.
- Preserve starting wreckage. Do not consume it as an automatic material source.

## Architecture
- Live world catalog: version/mod fingerprint, recipes, technologies, surfaces, machine geometry and typed item/fluid ports.
- Deterministic supervisor: observe, resolve dependencies, choose a reviewed executor, execute, observe and validate.
- Parameterized library: mining, smelting, steam, assembly, chemistry, labs, defense and silo blocks.
- Grid placement and separate item/fluid routing account for terrain, throughput, footprint and expansion corridors.
- Explicit running/waiting/blocked/failed/succeeded results; placement or a command acknowledgement alone is not completion.
- World-scoped checkpoints reconcile partial builds and invalidate proofs after save rollback or game-data changes.
- Single executor owns a run. No model, Slurm, foundry or generated-code hooks in the new execution path.

## Implementation Parts
1. Resource-conserving adapter, isolated startup, live catalog, truthful results, checkpoint and usage telemetry.
2. Initial direct-feed iron/copper cells, coal/stone supply and sustained coal-fed steam power.
3. Construction mall, red/green science, automatic lab feeding, turret/ammunition/wall supply and repair.
4. Oil and fluid production, byproduct handling, blue science and advanced intermediates.
5. Silo, rocket parts, platform starter pack, real launch and arrival proof.
6. Launcher, observer GUI, dashboard and lower-priority character execution validation.

## Quality and Acceptance
- Iron/copper production uses mining drills and furnaces, not recurring manual ore collection.
- Bootstrap hand supply is bounded by construction/startup needs and stops when automatic supply is verified.
- Related sites stay compact; longer routes require validated material, throughput, power and expansion corridors.
- Templates must include all required item/fluid/power connections. Fail closed on unsupported geometry.
- Science starts at 30 packs/minute; calculate actual machine counts and supply needs from live recipes.
- Test initial fuel/power/production for at least five game minutes, and recover broken supplies and attack damage.
- Use identical frozen code for autonomous RCON rocket runs on seeds 20260908, 101 and 202.
- Main-run completion requires a real launch count increase and natural platform hub creation after starter-pack delivery.
- Fixture resources belong only in clearly separate test saves; they must not enter acceptance runs.
- Keep exact action/result logs and meaningful progress evidence; do not hide stalls behind unrelated inventory changes.

## Delivery and Project Memory
- Validate and commit/push each completed part to GitHub on feat/deterministic-space-age-autoplayer.
- Read HANDOFF_CURRENT.md first in a new thread. Keep it at ten lines or fewer.
- Append concise meaningful execution events to note.md; never read the whole archive.
- Append to insight.md only for confirmed reusable improvements; preserve its pre-existing local changes.
- Report measured token totals (including cached input), part deltas when available, and observed account weekly usage separately.
