# Two versions, two folders

| Version | Folder | Branch | Purpose |
| --- | --- | --- | --- |
| v1 | `C:/Users/NEC/Documents/Factorio` | `v1` | Preserve old saves and four existing uncommitted changes |
| v2 | `C:/Users/NEC/Documents/git/factorio_automation` | `v2` | Active Luna-driven framework development |

GitHub default branch is `v2`. These are separate clones; there are no auxiliary
worktrees. Do not create more worktrees for routine development or evaluation.
An evaluation run needs an isolated ignored runtime directory, not a checkout.

## Preserved history

2026-11-11 cleanup removed 50 auxiliary worktrees, reduced 56 old local branches
and 5 old GitHub branches to v1/v2, and preserved all original branch tips.

- Five previously remote tips: GitHub tags `archive/2026-11-11/<old-branch>`.
- All 56 legacy local tips: same tag prefix in the legacy clone.
- Complete pre-cleanup refs/objects: v2 `runtime/housekeeping/legacy-all-refs.bundle`.
- All files from each removed worktree, including untracked and ignored files:
  v2 `runtime/housekeeping/worktrees/<worktree-name>.zip`.
- Every ZIP has an `ARCHIVE_MANIFEST.json` with file SHA-256 hashes and original
  branch/HEAD/dirty paths. Archives passed CRC and file hash checks before removal.
- Exact inventory, cleanup results and branch results: JSON under that same
  ignored `runtime/housekeeping/` directory. Do not delete these backups.

The old main folder's four modified files and all old runtime/save data remain
in place. Backups are local; no game profile, save or untracked proof file was
published to GitHub.

Inspect an old committed file with `git show archive/2026-11-11/<branch>:<file>`
in the legacy clone. To recover uncommitted work, extract its ZIP to a separate
ordinary folder and compare/copy the needed files; this does not require a new
worktree. `git bundle verify <bundle>` verifies the complete Git backup.
