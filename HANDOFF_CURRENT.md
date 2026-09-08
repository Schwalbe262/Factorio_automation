# Current Handoff
- Branch: feat/deterministic-space-age-autoplayer; implementing approved LLM-free Space Age first-rocket plan.
- Read goal.md for current rules; old Qwen/Slurm strategy is legacy, not the new runtime path.
- New deterministic_game.py uses engine character/crafting, resource-conserving actions and isolated saves.
- world_catalog.py exports live 2.1.9 data; first rocket+starter+research BOM resolves from actual prototypes.
- deterministic_state.py handles atomic checkpoints, rollback proof invalidation and exclusive run ownership.
- Token telemetry can read exact current-session JSONL when the Codex state DB lacks the thread.
- Primary visible TEST world: runtime/deterministic/adapter-smoke, UDP34210/RCON27025; bootstrap controller now being tested.
- Prior saves preserved; pre-existing dirty insight.md must not be staged.
- Next: finish live bootstrap, continuous coal-fed power, then automated production/research/defense/rocket; not yet complete.
