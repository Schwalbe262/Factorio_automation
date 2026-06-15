## Codex context budget policy

Start new Codex threads from `HANDOFF_CURRENT.md`.
Do not resume long old threads by default.

Read only:
1. `HANDOFF_CURRENT.md`
2. `AGENTS.md` or equivalent instructions
3. minimal project metadata needed for commands

Treat long journals, old handoffs, logs, JSONL traces, generated reports, `note.md`, and `insight.md` as archive/search-only history.

Do not read archive files in full.
Search them only for specific facts with `rg` or equivalent.

Do not print huge command output.
For large logs/tests/searches, save raw output and show only filtered evidence.

For correctness:
- inspect exact source ranges before editing;
- inspect exact diff hunks before review;
- do not rely on lossy summaries for source, diff, schema, migration, auth/security, infra, or data-loss-sensitive decisions.

At the end of each small part:
- update `HANDOFF_CURRENT.md` in 10 lines or fewer;
- append only concise key events to long journals;
- add only confirmed reusable lessons to insight docs;
- record current Codex thread token usage when supported.

Hard input rules:
- Never read `note.md` in full.
- Never read `insight.md` in full.
- Never paste full test/build logs.
- Never paste full `git diff`.
- Never continue a long previous Codex thread when `HANDOFF_CURRENT.md` can resume the work.
