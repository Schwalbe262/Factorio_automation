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
set FACTORIO_AI_REQUIRE_LLM_STRATEGY=1

echo [factorio-ai] Checking active Qwen 4B Slurm LLM worker...
python -m factorio_ai.cli slurm-llm-status || exit /b 1

echo [factorio-ai] Checking no-custom-mod RCON server...
python -m factorio_ai.cli no-mod-observe >nul 2>nul
if errorlevel 1 (
  echo [factorio-ai] No live RCON server detected. Ensuring save exists...
  python -m factorio_ai.cli create-no-mod-save || exit /b 1
  echo [factorio-ai] Starting no-custom-mod LAN/RCON server in a separate window...
  start "Factorio AI No-Mod Server" cmd /k "cd /d ""%~dp0"" && set PYTHONPATH=src && python -m factorio_ai.cli start-no-mod-server"
  echo [factorio-ai] Waiting for RCON...
  timeout /t 12 /nobreak >nul
)

echo [factorio-ai] Starting continuous no-custom-mod autopilot with required Slurm LLM strategy.
echo [factorio-ai] Use Ctrl+C in this window to stop the autopilot loop.
python -m factorio_ai.cli run-no-mod-autopilot --objective launch_rocket_program --require-llm --cycles 0 --sleep-seconds 5
