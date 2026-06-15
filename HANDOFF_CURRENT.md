\# Current Handoff

\## Current status

\- Branch `chore/part129-red-science-logistics` is active for Part 129 red-science logistics and layout repair.
\- Planner now covers role-aware site-input dogleg belts, NORTH=0 direction handling, resource-safe gear/belt relocation, early coal drill+chest supply, user-output mall chests, lab spacing, regular inserter preference, and direct assembler transfer.
\- `setup_coal_supply` now places a coal burner mining drill before output chest/belt work when a drill is available.
\- Factory sites now fold dedicated intermediate assemblers into parent sites via `subitems`, such as `iron-gear-wheel` under `transport-belt`.
\- Root current Markdown files now use the escaped `md/` marker format.
\- Slurm idle layout work now requests confirmed `learned_skills` and records only evidence-backed reusable layout skills.
\- Live observe found `coal_miners=1` at `{x:106,y:24}`.

\## Current objective

\- Part 129 coal-drill/site-hierarchy/Markdown-format changes are validated and ready to push from this branch.
\- Next gameplay work: continue coal fuel feed and red-science logistics after the pushed part.

\## Active branch / part

\- Branch: `chore/part129-red-science-logistics`
\- Part: Part 129 - red science planning-site cache recovery and markdown format adoption.

\## Important files

\- `goal.md`: mission, current sprint, and project quality criteria.
\- `src/factorio_ai/controller.py`: no-mod planning-site cache and retry behavior.
\- `src/factorio_ai/planner.py`: gear/belt logistics, site-input routes, mall outputs, coal supply, lab placement, power-sensitive execution.
\- `src/factorio_ai/strategy.py`: power-first guardrails before belt/site logistics.
\- `src/factorio_ai/remote_slurm.py`, `src/factorio_ai/slurm_worker.py`, `src/factorio_ai/run_journal.py`: idle layout skill-learning request and confirmed insight recording.
\- `note.md`: concise loop journal; archive/search-only for old evidence.
\- `insight.md`: confirmed reusable improvements only.

\## Last validation

\- `py_compile`, focused planner/monitor/web/slurm tests (`335`), full `pytest -q` (`612`) passed.
\- Full `pytest -q` passed: `617 passed`.
\- Live coal supply output corrected to `{x:107.5,y:24.5}` for burner drill `{x:106,y:24}`; coal feed belt route is extending but still short of boiler.
\- Token sample recorded: `162,147,720` Factorio Codex thread tokens; delta `8,652,602`; weekly quota unknown.

\## Current blocker

\- No code/test blocker. Live gameplay still needs more belt batches to finish `connect_coal_fuel_feed` to the boiler.

\## Next steps

1\. Push Part 129 branch if the current session has not already pushed it.
2\. Refresh/restart the dashboard process if it is still serving old code.
3\. Refill belt mall as needed, then continue `connect_coal_fuel_feed` until boiler receives belt-fed coal.
4\. Continue red science/logistics research once fuel, belt output, and site input routes are stable.

\## Token/context policy

\- Start from this file.
\- Do not read `note.md` or `insight.md` in full.
\- Search archive docs only with targeted `rg`.
\- Do not paste full logs, JSON/JSONL, test output, or git diff.
\- Update this file in 10 lines or fewer at closeout.

\## Archive/search policy

\- `note.md`: chronological loop archive.
\- `insight.md`: confirmed reusable improvements.
\- Old handoffs/logs/traces: search-only.

\## Recent changes

\- Prioritized coal drill placement before coal output chest/belt work.
\- Added parent-site `subitems` for dedicated intermediate assemblers and exposed them in compact payloads/UI.
\- Applied escaped `md/` marker format to current root Markdown files.
\- Added confirmed idle Slurm `learned_skills` flow with journal promotion only for confirmed reusable lessons.
\- Added route corner belt direction, endpoint inserter role checks, and source endpoint protection for site-input logistics.
\- Fixed site-input/iron-line inserter pickup/drop semantics, corrected integer-center coal drill output tiles, and stopped boiler feed from treating the next route belt as the coal source.
\- Added role-aware dogleg site-input routes and fixed `NORTH=0` direction comparisons so north-facing belts/inserters are not treated as missing direction.
\- Full validation now passes `612 passed`.

\## Risks and gotchas

\- `md/` contains user-provided format references and is not automatically a code artifact.
\- Runtime logs and planning caches are ignored local artifacts and can be stale.
\- Live no-mod skill runs mutate the current Factorio save.
