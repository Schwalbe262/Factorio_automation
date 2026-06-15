@echo off
setlocal
cd /d "%~dp0"

if not "%FACTORIO_AI_ALLOW_LEGACY_DIRECT_SLURM%"=="1" (
  echo [factorio-ai] Legacy direct Slurm attached benchmark is disabled. Use scheduler mode at http://100.112.168.31:8000.
  exit /b 1
)

set PYTHONPATH=src
set FACTORIO_AI_SLURM_ENABLED=1
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

echo [factorio-ai] Running attached Qwen 4B benchmark inside the existing Slurm allocation...
python -m factorio_ai.cli slurm-submit-model-benchmark --attached --models Qwen/Qwen3.5-4B --timeout 240
