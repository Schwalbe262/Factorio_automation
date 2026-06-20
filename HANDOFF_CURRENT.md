# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; supervisor running with autopilot PID `50800`.
- Startup context: read this file and targeted `goal.md`; never read `note.md`/`insight.md` in full.
- Live 2026-06-20 14:21 KST: server UP tick `2748950`, scheduler vLLM service `12161`, skill `connect_coal_fuel_feed` step 11.
- Fixed Slurm scheduler/attached task uploads: retry once after cleaning stale task temps on `scp: write remote`/quota-style failures.
- Fixed belt-mall bootstrap self-loop: the target transport-belt assembler is excluded as a gear source.
- Fixed local gear prerequisite checks: remote/consumer-held gears no longer satisfy local `iron-gear-wheel` mall completion.
- Validation: `PYTHONPATH=src python -m unittest tests.test_remote_slurm tests.test_planner` -> 401 OK.
- Live evidence: bootstrap produced 4 belts in chest unit 289; coal feed consumed them and laid belts units 292-295, then fueled/moved to coal.
- Token checkpoint: goal tracker `3,204,576`; latest recorded delta `548,205`; weekly quota unavailable.
