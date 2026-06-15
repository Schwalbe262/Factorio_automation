\# Current Handoff

\## Current status

\- Branch `chore/part129-red-science-logistics` is active for Part 129 red-science logistics and layout repair.
\- Planner now covers role-aware site-input dogleg belts, NORTH=0 direction handling, resource-safe gear/belt relocation, early coal drill+chest supply, user-output mall chests, lab spacing, regular inserter preference, and direct assembler transfer.
\- `setup_coal_supply` now places a coal burner mining drill before output chest/belt work when a drill is available.
\- Factory sites now fold dedicated intermediate assemblers into parent sites via `subitems`, such as `iron-gear-wheel` under `transport-belt`.
\- Root current Markdown files now use the escaped `md/` marker format.
\- Slurm scheduler mode defaults to `rtx3090`/`r1jae262`; layout improvement requests use `a6000ada,a6000` candidates and submit the ready single `gpu_model`.
\- Scheduler layout tasks now fail visibly if requested vLLM does not become ready, and the idle loop will not submit a second layout task while one is active.
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
\- Full `pytest -q` passed: `639 passed`.
\- Live validation: layout scheduler selected `a6000`; task `148` confirmed remote `factorio-ai` env has `vllm` command/module, but recent layout tasks still fell back with `Connection refused`.
\- Token sample recorded: `253,817,535` Factorio Codex thread tokens; delta `17,972,119`; weekly quota unknown.

\## Current blocker

\- Idle layout loop is stopped; vLLM starts too slowly or exits before `/v1/models` is ready, so restarting now would submit failed layout tasks.

\## Next steps

1\. Inspect scheduler vLLM logs for tasks `145`/`147` or run a short startup probe to find why `/v1/models` stays unavailable.
2\. Increase/fix vLLM startup on scheduler A6000, then restart `run-no-mod-idle-layout-loop`.
3\. Refresh/restart the dashboard process if it is still serving old code.
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
\- Fixed scheduler task payload size, A6000 layout routing, vLLM startup failure reporting, active layout task throttling, belt mall output direction, belt-chest consumption, and local gear-output bootstrap.

\## Risks and gotchas

\- `md/` contains user-provided format references and is not automatically a code artifact.
\- Runtime logs and planning caches are ignored local artifacts and can be stale.
\- Live no-mod skill runs mutate the current Factorio save.
