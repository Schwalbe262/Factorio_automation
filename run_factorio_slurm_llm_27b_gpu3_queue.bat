@echo off
setlocal
cd /d "%~dp0"

set PYTHONPATH=src
set FACTORIO_AI_SLURM_ENABLED=1
set FACTORIO_AI_SLURM_REMOTE_DIR=~/factorio-ai-worker-27b
set FACTORIO_AI_SLURM_JOB_NAME=factorio-ai-worker-27b
set FACTORIO_AI_SLURM_GPUS_PER_NODE=3
set FACTORIO_AI_SLURM_GRES=gpu:a6000ada:3
set FACTORIO_AI_SLURM_PARTITION=gpu3
set FACTORIO_AI_VLLM_MODEL=Qwen/Qwen3.6-27B-FP8
set FACTORIO_AI_VLLM_ARGS=--tensor-parallel-size 3 --max-model-len 32768 --gpu-memory-utilization 0.85 --enforce-eager
set FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0
set FACTORIO_AI_VLLM_PORT=8000
set FACTORIO_AI_VLLM_STARTUP_SECONDS=480

echo [factorio-ai] Starting or reusing 3-GPU Qwen 27B Slurm queue worker...
python -m factorio_ai.cli slurm-start-worker || exit /b 1
python -m factorio_ai.cli slurm-status
python -m factorio_ai.cli slurm-llm-status
