# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; Part 130 - unattended no-mod Qwen 9B local LLM supervisor.
- Startup context: read this file and targeted `goal.md`; never read `note.md`/`insight.md` in full.
- No-mod helpers use `Qwen/Qwen3.5-9B`, vLLM service duration 10800s, and ordered scheduler GPU candidates `a6000ada,a6000`.
- `/tasks` GPU submissions preserve ordered `gpu_model` candidates; service-mode strategy/layout clients now attach to the running vLLM service node.
- Client tasks default to `FACTORIO_AI_SCHEDULER_VLLM_CLIENT_GPUS=1` so scheduler placement stays on the service node where `127.0.0.1` vLLM is reachable.
- Strategy metadata such as `input_item` is filtered before `_run_skill`, while mapped skill arguments still reach the selected skill.
- Live runtime: supervisor PID 76388; vLLM service task 8224 ready on allocation 40/n104; strategy task 8229 completed on allocation 40 and started `build_site_input_logistic_line`.
- Current gameplay blocker: selected site-input route failed because no executable repeated logistics route was found; layout tasks 8226 and 8233 are running on allocation 40, and 8232 completed.
- Validation: py_compile ok; PowerShell parser ok; `PYTHONPATH=src pytest tests/test_controller.py tests/test_remote_slurm.py -q` -> 109 passed.
