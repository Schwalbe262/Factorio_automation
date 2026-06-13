@echo off
setlocal
cd /d "%~dp0"

set PYTHONPATH=src
set FACTORIO_AI_SLURM_ENABLED=1
set FACTORIO_AI_SLURM_REMOTE_DIR=~/factorio-ai-worker-9b
set FACTORIO_AI_SLURM_JOB_NAME=factorio-ai-worker-9b
set FACTORIO_AI_SLURM_GPUS_PER_NODE=1
set FACTORIO_AI_SLURM_GRES=gpu:a6000:1
set FACTORIO_AI_SLURM_PARTITION=gpu4
set FACTORIO_AI_VLLM_MODEL=Qwen/Qwen3.5-9B
set FACTORIO_AI_VLLM_ARGS=--max-model-len 32768 --gpu-memory-utilization 0.90 --enforce-eager
set FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0
set FACTORIO_AI_VLLM_PORT=8000
set FACTORIO_AI_VLLM_STARTUP_SECONDS=420

echo [factorio-ai] Running attached Qwen 9B benchmark inside the existing Slurm allocation...
python -m factorio_ai.cli slurm-submit-model-benchmark --attached --models Qwen/Qwen3.5-9B --timeout 300
