# Current Handoff

## Current status

- Branch `chore/part129-red-science-logistics` is active for red-science logistics, markdown memory format adoption, and current layout repair.
- Markdown format, stale planning-site cache recovery, first-assembler gear bootstrap, and no-mod logistics/build-item commands are implemented.
- Current live save still has the old compact gear/belt assemblers on iron ore and unpowered; new planner recognizes them as relocation sources.
- Strategy currently selects `bootstrap_power_pole_mall` because relocation needs more small-electric-pole stock and coal supply is still missing.

## Current objective

- Finish Part 129 by recording current Codex token usage, committing, and pushing.
- Next gameplay work: produce poles, relocate gear/belt mall to a resource-safe 4-tile layout, then build early coal supply/logistics.

## Active branch / part

- Branch: `chore/part129-red-science-logistics`
- Part: Part 129 - red science planning-site cache recovery and markdown format adoption.

## Important files

- `goal.md`: mission, current sprint, and project quality criteria.
- `src/factorio_ai/controller.py`: no-mod planning-site cache and retry behavior.
- `src/factorio_ai/planner.py`: planning cache consumers, item malls, gear/belt relocation, power corridor, coal supply behavior.
- `src/factorio_ai/strategy.py`: coal supply readiness and gear/belt relocation guardrails.
- `note.md`: concise loop journal; archive/search-only for old evidence.
- `insight.md`: confirmed reusable improvements only.

## Last validation

- `pytest -q` passed: 569 tests.
- Live no-mod strategy selects `bootstrap_power_pole_mall`; evidence includes `small_electric_pole_deficit=18` and `coal_supply_ready=false`.
- Live planner relocation layout for old units 73/74 targets gear `{x:82.5,y:-58.0}` and belt `{x:86.5,y:-58.0}`, spacing 4.0, not on resources.

## Current blocker

- Live world has not been repaired yet; it needs power-pole stock before gear/belt relocation can mine/rebuild the old compact mall.
- One live `run-no-mod-strategy-step --max-steps 80` attempt timed out after 240s and was stopped; no observed world change.

## Next steps

1. Record current Codex thread token usage once.
2. Commit and push Part 129 branch.
3. Re-run `run-no-mod-strategy-step` with a longer timeout or direct pole-mall skill until poles are stocked.
4. Run relocation, then verify old units 73/74 are gone and new gear/belt assemblers are powered, 4 tiles apart, and off resources.
5. Run/setup coal supply early so a coal drill outputs to a chest/belt instead of repeated hand mining.

## Token/context policy

- Start from this file.
- Do not read `note.md` or `insight.md` in full.
- Search archive docs only with targeted `rg`.
- Do not paste full logs, JSON/JSONL, test output, or git diff.
- Update this file in 10 lines or fewer at closeout.

## Archive/search policy

- `note.md`: chronological loop archive.
- `insight.md`: confirmed reusable improvements.
- Old handoffs/logs/traces: search-only.

## Recent changes

- Added current handoff and root AGENTS instructions in the new markdown memory format.
- Fixed stale no-mod planning-site cache recovery; automation research live run succeeded after the fix.
- Added no-mod logistics/build-item mall commands and first-assembler gear bootstrap safeguards.
- Added resource-safe, 4-tile gear/belt relocation, stricter connected-power anchors, and coal-output readiness.

## Risks and gotchas

- `md/` contains user-provided format references and is not automatically a code artifact.
- Runtime logs and planning caches are ignored local artifacts and can be stale.
- Live no-mod skill runs mutate the current Factorio save.
