@echo off
setlocal
cd /d "%~dp0"

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

echo [factorio-ai] Starting or reusing 1-GPU Qwen 4B active Slurm worker...
python -m factorio_ai.cli slurm-start-worker || exit /b 1
python -m factorio_ai.cli slurm-status
python -m factorio_ai.cli slurm-llm-status
