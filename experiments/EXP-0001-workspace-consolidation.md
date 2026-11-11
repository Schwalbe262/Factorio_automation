# EXP-0001 — Workspace consolidation

- Date: 2026-11-11 Asia/Seoul.
- Part: 0, repository and reproducible development setup.
- Goal: retain understandable v1/v2 branches and remove obsolete worktrees.
- Hypothesis: preserve all refs and every worktree file before removing redundant
  checkouts; keep the dirty legacy main checkout unchanged.
- Initial evidence: 5 GitHub branches, 56 legacy local branches, 51 legacy
  worktrees including main; 8 auxiliary worktrees contain uncommitted/untracked
  files. New checkout starts at 22e7a49084d4dc3942b222de6a137ca28a853453.
- Actions: inventory all checkouts; create/verify an all-refs Git bundle; ZIP
  every auxiliary checkout including ignored files; verify CRC and SHA-256;
  remove only exact registered paths beneath the legacy worktrees directory.
- Backup location: ignored `runtime/housekeeping/` in the v2 checkout.
- Result: solved. All 50 auxiliary worktrees archived and removed; legacy main
  checkout retains the same four dirty files. Both clones and GitHub have only
  v1/v2 branches; GitHub default is v2. All 56 local tips preserved as local tags
  and a verified bundle; all five previously remote tips preserved on GitHub as
  archive tags. See `docs/v2/WORKSPACE.md` for locations and recovery.
- Interruption: an earlier attempt stopped during bundle creation before any
  worktree removal. No live cleanup process remained. Partial bundle retained as
  `interrupted-bundle.partial`; restarted backup with verified process state.
- Next: implement live v2 job runner and isolated default-settings world.
- Rocket status: incomplete; no v2 live gameplay yet.
- Tokens: report-before-commit sample 2,438,568 including cached input, measured
  from thread cumulative 755,121 to 3,193,689 for the initial implementation/setup
  interval. Account weekly meter 3% to 4% (+1 percentage point); this account
  delta includes concurrent activity and is not an exact per-part quota share.
