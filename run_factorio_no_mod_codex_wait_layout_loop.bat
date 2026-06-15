@echo off
setlocal
cd /d "%~dp0"

set PYTHONPATH=src
set FACTORIO_AI_SLURM_ENABLED=1
set FACTORIO_AI_SLURM_MODE=scheduler
set FACTORIO_AI_SLURM_SCHEDULER_URL=http://100.112.168.31:8000
set FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT=r1jae262
set FACTORIO_AI_SLURM_REMOTE_DIR=~/factorio-ai-worker
set FACTORIO_AI_SLURM_SCHEDULER_GPUS=1
set FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL=rtx3090
set FACTORIO_AI_VLLM_MODEL=Qwen/Qwen3.5-4B
set FACTORIO_AI_VLLM_ARGS=--max-model-len 32768 --gpu-memory-utilization 0.85 --enforce-eager
set FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0
set FACTORIO_AI_VLLM_PORT=8000
set FACTORIO_AI_VLLM_STARTUP_SECONDS=240
set FACTORIO_AI_BACKGROUND_LAYOUT_ENABLED=1
set FACTORIO_AI_BACKGROUND_LAYOUT_MODE=scheduler
set FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS=20

echo [factorio-ai] Running no-custom-mod Codex wait layout loop.
echo [factorio-ai] This submits simulation-only layout improvement work until runtime\codex-wait.json clears.
python -m factorio_ai.cli run-no-mod-codex-wait-layout-loop --objective launch_rocket_program --cycles 0 --sleep-seconds 20
