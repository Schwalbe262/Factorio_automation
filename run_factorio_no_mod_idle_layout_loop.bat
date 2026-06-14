@echo off
setlocal
cd /d "%~dp0"

set PYTHONPATH=src
set FACTORIO_AI_SLURM_ENABLED=1
set FACTORIO_AI_SLURM_MODE=attach
set FACTORIO_AI_SLURM_REMOTE_DIR=~/factorio-ai-worker
set FACTORIO_AI_SLURM_JOB_NAME=factorio-ai-worker
set FACTORIO_AI_SLURM_GPUS_PER_NODE=1
set FACTORIO_AI_SLURM_GRES=gpu:1
set FACTORIO_AI_SLURM_PARTITION=gpu4,gpu2,gpu1
set FACTORIO_AI_VLLM_MODEL=Qwen/Qwen3.5-4B
set FACTORIO_AI_VLLM_ARGS=--max-model-len 32768 --gpu-memory-utilization 0.85 --enforce-eager
set FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0
set FACTORIO_AI_VLLM_PORT=8000
set FACTORIO_AI_VLLM_STARTUP_SECONDS=240
set FACTORIO_AI_BACKGROUND_LAYOUT_ENABLED=1
set FACTORIO_AI_BACKGROUND_LAYOUT_MODE=attach
set FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS=0

echo [factorio-ai] Running opportunistic no-mod layout loop.
echo [factorio-ai] It submits simulation-only site layout work whenever autopilot is idle, stopped, or stale.
python -m factorio_ai.cli run-no-mod-idle-layout-loop --objective launch_rocket_program --cycles 0 --sleep-seconds 5 --stale-seconds 15 --min-submit-interval-seconds 0
