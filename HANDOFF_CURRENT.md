\# Current Handoff

\## Current status

\- Branch `chore/part129-red-science-logistics` is active for Part 129 red-science logistics and layout repair.
\- Planner now covers role-aware site-input dogleg belts, NORTH=0 direction handling, resource-safe gear/belt relocation, early coal drill+chest supply, user-output mall chests, lab spacing, regular inserter preference, and direct assembler transfer.
\- `setup_coal_supply` now places a coal burner mining drill before output chest/belt work when a drill is available.
\- Factory sites now fold dedicated intermediate assemblers into parent sites via `subitems`, such as `iron-gear-wheel` under `transport-belt`.
\- Root current Markdown files now use the escaped `md/` marker format.
\- Slurm scheduler mode defaults to `rtx3090`/`r1jae262`; client task payloads are uploaded to remote files so `/tasks` commands stay small.
\- Live map: belt mall output inserter now points assembler-to-chest and produced belts into chest; boiler coal route reached about `x=0.5` but still needs more belts to reach boiler at `x=-43.5`.

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
\- Full `pytest -q` passed: `634 passed`.
\- Live validation: task command length now about `2KB`; output inserter `708` has pickup at assembler and drop at chest.
\- Token sample recorded: `225,528,048` Factorio Codex thread tokens; delta `63,380,328`; weekly quota unknown.

\## Current blocker

\- Idle layout loop stopped because scheduler task 50 failed before user command: `srun` invoked `~/slurm_scheduler/.../task.sh` as argv and the shell did not expand `~`.

\## Next steps

1\. Push Part 129 branch if the current session has not already pushed it.
2\. Refresh/restart the dashboard process if it is still serving old code.
3\. Ask scheduler side to use absolute task.sh paths inside `srun`/wrapper, then restart `run-no-mod-idle-layout-loop`.
4\. Fuel iron/coal burner drills as needed, refill belt mall, then continue `connect_coal_fuel_feed` until boiler receives belt-fed coal.
5\. Continue red science/logistics research once fuel, belt output, and site input routes are stable.

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
\- Fixed scheduler task payload size, belt mall output inserter direction, belt-chest consumption for boiler feed, and local gear-output bootstrap for belt/inserter infrastructure.

\## Risks and gotchas

\- `md/` contains user-provided format references and is not automatically a code artifact.
\- Runtime logs and planning caches are ignored local artifacts and can be stale.
\- Live no-mod skill runs mutate the current Factorio save.
