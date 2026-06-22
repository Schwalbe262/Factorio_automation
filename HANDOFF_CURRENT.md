# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; Qwen Slurm worker has remote availability issues, heuristic fallback may still appear.
- Fixed route-scale belt bootstrap loop and long gear-mall iron-line hand-carry recovery; validated planner/strategy/controller earlier.
- Current live autopilot PID `66372`; `run-health --no-observe` shows supervisor running, live skill `produce_iron_plate`, recovery root `iron_plate_source_missing`.
- Remote disk cleanup on Slurm: `factorio-ai-worker` 37M, `slurm_scheduler` 2.5G, `factorio-ai-models` 21G, `~/.cache` 7.8G after removing stale scheduler task clones and duplicate default HF Qwen caches.
- Slurm vLLM scripts now default `HF_HOME`/`HUGGINGFACE_HUB_CACHE` to `$HOME/factorio-ai-models` to avoid duplicate model downloads.
- Validated `PYTHONPATH=src python -m unittest tests.test_remote_slurm` (75 OK); deployed updated worker code with `cleanup_and_deploy.ps1`.
- Remaining structural live blocker from before cleanup: `produce_iron_plate` may oscillate between direct iron support and expanded smelting repair; inspect planner around `IronPlateSkill` next.
- Dirty runtime-heavy `note.md`/`insight.md` exist; stage only intentional code/tests/handoff unless explicitly cleaning history.
