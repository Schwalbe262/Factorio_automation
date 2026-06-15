# Current Handoff

## Current status
- Part 128 selected-row token SQLite query is patched and validated on `chore/codex-context-budget`.
- No previous root `HANDOFF_CURRENT.md` existed; old handoffs remain archive/search-only history.
- `AGENTS.md` contains the active Codex context policy and should be tracked with this part.

## Current objective
- Reduce Codex input and cached_input growth without reducing correctness.
- Keep future startup context short and require exact source ranges, exact diff hunks, and filtered evidence.

## Active branch/part
- Branch: `chore/codex-context-budget`
- Part 127: current handoff and startup instruction cleanup, pushed as `2c1b78e`.
- Part 128: selected-row Codex token SQLite query tightening, pending commit/push.

## Important files
- Startup context: `HANDOFF_CURRENT.md`, `AGENTS.md`, `pyproject.toml`.
- Startup docs to keep aligned: `README.md`, `continue_factorio_cli.bat`.
- Token accounting: `src/factorio_ai/token_usage.py`, `src/factorio_ai/cli.py`, `tests/test_token_usage.py`.

## Last validation
- Part 128: `tests/test_token_usage.py` 9 passed; targeted suite 34 passed; full `pytest -q` 555 passed; token delta `1,365,554`, weekly percent unknown.

## Current blocker
- `FACTORIO_AI_WEEKLY_TOKEN_QUOTA` is unset in the current shell, so weekly token percent reports `unknown`.

## Next 3-7 concrete steps
- Review exact Part 128 diff hunks.
- Commit and push Part 128.
- Open or update the PR for `chore/codex-context-budget`.
- Keep later feature work on a separate branch/part.
- Keep future `HANDOFF_CURRENT.md` closeouts to 10 changed lines or fewer.
- Continue recording one current Codex thread token sample per completed part.

## Token/context policy
- New Codex sessions start with `HANDOFF_CURRENT.md`, `AGENTS.md`, and minimal project metadata only.
- Use `python -m factorio_ai.cli record-current-codex-thread-usage --label "partXXX short label"` once at closeout.
- Report absolute token delta and weekly percent when `FACTORIO_AI_WEEKLY_TOKEN_QUOTA` is configured; otherwise report percent as `unknown`.

## Archive/search policy
- Treat `note.md`, `insight.md`, `AGENT_HANDOFF.md`, `LLM_CONTINUATION.md`, `docs/CLI_HANDOFF.md`, logs, JSONL traces, generated reports, screenshots, and trace archives as search-only.
- Do not read archive files in full; use `rg`, exact ranges, `Select-Object -First/-Last`, `tail`, `head`, or deterministic parsers.
- Do not print full logs, full JSON/JSONL, full generated reports, or full `git diff`.

## Recent changes
- Added this root current handoff as the short startup source.
- Aligned README and the CLI launcher to start from `HANDOFF_CURRENT.md`.
- Marked old handoffs as archive/search-only.

## Risks and gotchas
- Old docs may still contain historical instructions that conflict with current startup policy; prefer this file and targeted searches.
- Correctness still requires exact source reads before edits and exact diff hunk review before commit.
- `logs/` and `runtime/` are ignored local artifacts and can be very large.
