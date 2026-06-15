# Current Handoff

## Current status

- Branch `chore/part129-red-science-logistics` is active for Part 129 red-science logistics and layout repair.
- Planner now covers resource-safe gear/belt relocation, early coal drill+chest supply, user-output mall chests, science/lab spacing, regular inserter preference, direct assembler transfer, and site-input belts.
- Slurm idle layout work now requests confirmed `learned_skills` and records only evidence-backed reusable layout skills.
- Live strategy now selects `setup_power` when the gear/belt mall is unpowered; current live power run hit max steps while waiting on boiler fuel burn.

## Current objective

- Finish Part 129 by recording current Codex token usage, committing, and pushing.
- Next gameplay work: continue `setup_power`, then resume belt mall output and site-input logistics once power is stable.

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

- `pytest -q` passed: 600 tests.
- Live site-input retry no longer fails on mixed integer/half-tile belt coordinates; latest blocker is powered belt automation.
- Live strategy check selects `setup_power` with `gear_belt_mall_status=no_power`.

## Current blocker

- Live gear/belt assemblers are placed off resources and 4 tiles apart, but currently report `no_power`.
- `setup_power` run inserted boiler fuel then reached max steps waiting for fuel burn/steam recovery.

## Next steps

1. Record current Codex thread token usage once.
2. Commit and push Part 129 branch.
3. Continue `run-no-mod-strategy-step --max-steps 60` or direct `setup_power` until steam/electric recovery is observed.
4. Re-run `build_gear_belt_mall_logistics` to refill belt output.
5. Re-run `build_site_input_logistic_line` for iron plates; verify corner belt direction and endpoint inserter roles.
6. Then continue early coal supply/fuel logistics so hand-mined coal does not recur.

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
- Added strategy guardrail so unpowered gear/belt mall forces `setup_power` before iron/site logistics.
- Full validation now passes `600 passed`.

## Risks and gotchas

- `md/` contains user-provided format references and is not automatically a code artifact.
- Runtime logs and planning caches are ignored local artifacts and can be stale.
- Live no-mod skill runs mutate the current Factorio save.
