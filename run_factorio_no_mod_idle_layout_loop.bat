@echo off
setlocal
cd /d "%~dp0"

set PYTHONPATH=src
set FACTORIO_AI_SLURM_ENABLED=1
set FACTORIO_AI_SLURM_MODE=scheduler
set FACTORIO_AI_SLURM_SCHEDULER_URL=http://100.112.168.31:8000
set FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT=r1jae262
set FACTORIO_AI_SLURM_REMOTE_DIR=~/factorio-ai-worker
set FACTORIO_AI_SLURM_TASK_TIMEOUT_SECONDS=900
set FACTORIO_AI_SLURM_SCHEDULER_CPUS=3
set FACTORIO_AI_SLURM_SCHEDULER_GPUS=1
set FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL=a6000ada,a6000,rtx3090
set FACTORIO_AI_SLURM_SCHEDULER_PRIORITY=100
set FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS=a6000ada,a6000
set FACTORIO_AI_SLURM_LAYOUT_CPUS=3
set FACTORIO_AI_SLURM_LAYOUT_PRIORITY=80
set FACTORIO_AI_VLLM_MODEL=Qwen/Qwen3.5-9B
set FACTORIO_AI_VLLM_ARGS=--max-model-len 32768 --gpu-memory-utilization 0.90 --enforce-eager
set FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0
set FACTORIO_AI_VLLM_PORT=8000
set FACTORIO_AI_VLLM_STARTUP_SECONDS=420
set FACTORIO_AI_LLM_GUIDED_JSON=1
set FACTORIO_AI_LLM_TIMEOUT=600
set FACTORIO_AI_REMOTE_STRATEGY_TIMEOUT_SECONDS=900
set FACTORIO_AI_BACKGROUND_LAYOUT_ENABLED=1
set FACTORIO_AI_BACKGROUND_LAYOUT_MODE=scheduler
set FACTORIO_AI_BACKGROUND_LAYOUT_MAX_ACTIVE_TASKS=1
set FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS=0
set FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES=360
set FACTORIO_AI_SLURM_RENEW_CHECK_INTERVAL_SECONDS=1800

echo [factorio-ai] Running opportunistic no-mod layout loop.
echo [factorio-ai] It submits simulation-only site layout work whenever autopilot is idle, stopped, or stale.
echo [factorio-ai] Checking scheduler-managed Qwen local LLM path before idle layout work.
python -m factorio_ai.cli slurm-ensure-worker --renew-before-minutes %FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES% || exit /b 1
python -m factorio_ai.cli run-no-mod-idle-layout-loop --objective launch_rocket_program --cycles 0 --sleep-seconds 5 --stale-seconds 180 --min-submit-interval-seconds 0
