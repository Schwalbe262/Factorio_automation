\## Codex context budget and project memory policy

Start each new Codex thread from `HANDOFF_CURRENT.md`.

Hard input rules:
\- Never read `note.md` in full.
\- Never read `insight.md` in full.
\- Never paste full test/build logs.
\- Never paste full `git diff`.
\- Never paste full JSON/JSONL logs.
\- Never continue a long previous Codex thread when `HANDOFF_CURRENT.md` can resume the work.

Default startup context:
1\. `HANDOFF_CURRENT.md`
2\. `AGENTS.md` or equivalent repo instructions
3\. `goal.md`, only enough to understand mission/current sprint
4\. minimal project metadata needed for commands and entrypoints

Treat long journals, old handoffs, logs, JSONL traces, generated reports, `note.md`, and `insight.md` as archive/search-only history.

Use targeted search instead:
\- `rg -n "specific term" note.md`
\- `rg -n "specific term" insight.md`
\- `rg -n "^#|^##|Current|Next|Validation|Failure|Result" <long-md-file>`
\- `tail -n 80 <log>`
\- `git diff --stat`
\- `git diff -- <specific-file>`

For correctness:
\- inspect exact source ranges before editing;
\- inspect exact diff hunks before review;
\- do not rely on lossy summaries for source code, diffs, migrations, schemas, auth/security, infra, or data-loss-sensitive changes.

Project memory:
\- `goal.md` is the mission, sprint, success criteria, roadmap, and project-specific quality standard.
\- `note.md` is the chronological execution journal.
\- `insight.md` is only for confirmed reusable improvements.
\- `HANDOFF_CURRENT.md` is the short current-state handoff for new threads.

Loop journal rule:
Append to `note.md` for each meaningful execution loop:
\- timestamp;
\- part;
\- goal;
\- hypothesis;
\- actions;
\- candidates/options;
\- metrics;
\- result;
\- failure reason;
\- next action;
\- token usage if available.

Insight rule:
Append to `insight.md` only when a reusable improvement is confirmed:
\- source loop;
\- improvement;
\- before;
\- after;
\- evidence;
\- remaining risk.

Do not add routine loop completions, speculative ideas, diagnostic-only runs, or ordinary failures to `insight.md`.

At the end of each small part:
\- run the smallest relevant validation first;
\- inspect `git diff --stat`;
\- inspect only exact changed hunks needed for review;
\- update `HANDOFF_CURRENT.md` in 10 lines or fewer;
\- append one concise event to `note.md`;
\- append to `insight.md` only if there is a confirmed reusable improvement;
\- record current Codex thread token usage if the project supports it.

## Working rules for maintainers

Read first: `docs/v2/ARCHITECTURE.md`, then `docs/v2/ADDING_A_CAPABILITY.md`,
`docs/v2/DEBUGGING_A_STALL.md`, `docs/v2/PLANNERS.md` (index: `docs/v2/README.md`).

Non-negotiable:
- No granted items or research: every effect is a paid Lua body in `DeterministicGame.act`.
- Store `pending_action` before acting; never retry an unknown outcome, only `reconcile`.
- Every query is world-bound (`WorldBoundGame`); a changed world or a tick rollback blocks all jobs.
- Never build on ore except annotated mining entities (`resource_policy`, `planning.preflight`).
- One research-owning job; at most 4 active jobs, one per capability (`jobs._admission_error`).
- Fuel, stock and science move in batches (`FUEL_BATCH`, `COLLECT_BATCH`, `SCIENCE_COURIER_BATCH`); hand crafting stops above `HANDCRAFT_LIMIT` when a produce row exists.
- Sites come only from `SiteService` (`site.placement`, `site.fit`); never add fixed coordinates.
- Decisions are rules in code (roadmap, planners, auto-retry); the model only answers incidents.
- Measure progress with `python -B tools/report_run.py runtime/v2/<name>` milestones, not actions/min.
- Never hand-edit `jobs.json`, state files or the live game; fix the rule, add a test, `lifecycle new-map`.
- Run `powershell -ExecutionPolicy Bypass -File tools/check.ps1` from PowerShell before every commit.
- `git add` specific files only; keep the working tree clean (commit or discard, never leave WIP unstaged for days).
- Merge finished worktrees promptly and delete them.
