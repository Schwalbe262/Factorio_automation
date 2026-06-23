# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; keep huge runtime `note.md`/`insight.md` unstaged unless intentionally journaling.
- Slurm disk: `~/factorio-ai-worker` is 25M after deploy cleanup; `~/factorio-ai-models` is 21G active `QuantTrio/Qwen3.6-27B-AWQ` cache.
- vLLM fix: service and direct fallback now default `VLLM_USE_FLASHINFER_SAMPLER=0` because cluster compute nodes lack nvcc for FlashInfer JIT.
- `reset_serving.ps1` now launches one 27B AWQ service with `--max-model-len 16384 --gpu-memory-utilization 0.90 --quantization awq --enforce-eager`.
- Validation: `PYTHONPATH=src python -m unittest tests.test_remote_slurm` -> 77 OK; py_compile and diff check OK.
- Deploy/reset done: vLLM service task `13329` is running on a6000, heartbeat `ready`, `slurm-llm-status` reports `llm_ready=true`.
- Live health: Factorio server UP, supervisor gate `ready`, autopilot pid `[16936]`, live skill `bootstrap_build_item_mall` advanced from step 4 to step 10.
- Remaining watch items: foundry still shows stale implemented-skill queue; operator layout learning has pending human review trace.
- Token recording: latest Codex sample `41389093`; project DB may still make weekly quota unavailable.
