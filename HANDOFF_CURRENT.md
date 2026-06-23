# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; code/handoff changes should be staged without huge runtime `note.md`/`insight.md`.
- Slurm disk fixed: deploy archive excludes `note.md`/`insight.md` and prunes root/nested task/log dirs; remote `~/factorio-ai-worker` is ~8.5M after deploy.
- `~/factorio-ai-models` is still ~21G because it is the active `QuantTrio/Qwen3.6-27B-AWQ` cache; delete only if switching away from 27B.
- Planner fix: when started iron/site-input logistics run out of belts, `BuildItemMallSkill("transport-belt")` falls back to one-time iron-plate seed into the buffered belt assembler instead of returning action=null.
- Validation: `PYTHONPATH=src python -m unittest tests.test_remote_slurm tests.test_planner` -> 567 OK; py_compile and diff check OK.
- Live check: current `runtime/latest-observe.json` with new planner returns actionable `move near tree for pole wood`, not the old belt-shortage null failure.
- Slurm service: stale vLLM task `13326` canceled; fresh 27B service task `13327` is running but heartbeat remains `starting`/stale, so Qwen is not ready yet.
- Token recording: Codex DB malformed; fallback sample `40885851`, delta `1164388`, weekly quota unavailable.
