# Current Handoff

## Current status

- Branch `chore/part129-red-science-logistics` is active for Part 129 red-science logistics and layout repair.
- Planner now covers role-aware site-input dogleg belts, NORTH=0 direction handling, resource-safe gear/belt relocation, early coal drill+chest supply, user-output mall chests, lab spacing, regular inserter preference, and direct assembler transfer.
- Slurm idle layout work now requests confirmed `learned_skills` and records only evidence-backed reusable layout skills.
- Live site-input iron-plate belt route now has `missing_count=0` and `misoriented=[]`; current blocker is missing target input inserter.

## Current objective

- Finish Part 129 by recording current Codex token usage, committing, and pushing.
- Next gameplay work: produce or recover a regular inserter for the site consumer input, then resume gear/belt and red-science logistics.

## Active branch / part

- Branch: `chore/part129-red-science-logistics`
- Part: Part 129 - red science planning-site cache recovery and markdown format adoption.

## Important files

- `goal.md`: mission, current sprint, and project quality criteria.
- `src/factorio_ai/controller.py`: no-mod planning-site cache and retry behavior.
- `src/factorio_ai/planner.py`: gear/belt logistics, site-input routes, mall outputs, coal supply, lab placement, power-sensitive execution.
- `src/factorio_ai/strategy.py`: power-first guardrails before belt/site logistics.
- `src/factorio_ai/remote_slurm.py`, `src/factorio_ai/slurm_worker.py`, `src/factorio_ai/run_journal.py`: idle layout skill-learning request and confirmed insight recording.
- `note.md`: concise loop journal; archive/search-only for old evidence.
- `insight.md`: confirmed reusable improvements only.

## Last validation

- `pytest -q` passed: 602 tests.
- Live site-input retry repaired role-aware dogleg belts; latest blocker is missing inserter for `site consumer input inserter`.

## Current blocker

- Inventory and belt assembler output currently have no transport belts or inserters; site-input route is built but target inserter is absent.

## Next steps

1. Record current Codex thread token usage once.
2. Commit and push Part 129 branch.
3. Build or recover an `inserter` for the site consumer input without hand-crafted gear workaround.
4. Re-run `build_site_input_logistic_line` and verify target inserter direction SOUTH into the gear assembler.
5. Re-run `build_gear_belt_mall_logistics` to refill transport-belt output.
6. Continue red science/logistics research once iron input and belt output recover.

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

- Added confirmed idle Slurm `learned_skills` flow with journal promotion only for confirmed reusable lessons.
- Added route corner belt direction, endpoint inserter role checks, and source endpoint protection for site-input logistics.
- Added role-aware dogleg site-input routes and fixed `NORTH=0` direction comparisons so north-facing belts/inserters are not treated as missing direction.
- Added strategy guardrail so unpowered gear/belt mall forces `setup_power` before iron/site logistics.
- Full validation now passes `602 passed`.

## Risks and gotchas

- `md/` contains user-provided format references and is not automatically a code artifact.
- Runtime logs and planning caches are ignored local artifacts and can be stale.
- Live no-mod skill runs mutate the current Factorio save.
