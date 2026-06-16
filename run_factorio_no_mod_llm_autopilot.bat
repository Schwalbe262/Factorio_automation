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
set FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL=a6000ada,a6000
set FACTORIO_AI_SLURM_SCHEDULER_PRIORITY=100
set FACTORIO_AI_VLLM_MODEL=Qwen/Qwen3.5-9B
set FACTORIO_AI_VLLM_ARGS=--max-model-len 32768 --gpu-memory-utilization 0.90 --enforce-eager
set FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0
set FACTORIO_AI_VLLM_PORT=8000
set FACTORIO_AI_VLLM_STARTUP_SECONDS=420
set FACTORIO_AI_SCHEDULER_VLLM_SERVICE_ENABLED=1
set FACTORIO_AI_SCHEDULER_VLLM_SERVICE_DURATION_SECONDS=43200
set FACTORIO_AI_SCHEDULER_VLLM_SERVICE_HEARTBEAT_SECONDS=30
set FACTORIO_AI_SCHEDULER_VLLM_SERVICE_STALE_SECONDS=120
set FACTORIO_AI_SCHEDULER_VLLM_SERVICE_QUEUE_STALE_SECONDS=180
set FACTORIO_AI_SCHEDULER_VLLM_SERVICE_CPUS=1
set FACTORIO_AI_SCHEDULER_VLLM_CLIENT_CPUS=1
set FACTORIO_AI_SCHEDULER_VLLM_CLIENT_GPUS=0
set FACTORIO_AI_SCHEDULER_VLLM_SERVICE_PRIORITY=120
set FACTORIO_AI_REQUIRE_LLM_STRATEGY=1
set FACTORIO_AI_LLM_TIMEOUT=600
set FACTORIO_AI_REMOTE_STRATEGY_TIMEOUT_SECONDS=900
set FACTORIO_AI_BACKGROUND_LAYOUT_MAX_ACTIVE_TASKS=1
set FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES=360
set FACTORIO_AI_SLURM_RENEW_CHECK_INTERVAL_SECONDS=1800

echo [factorio-ai] Checking scheduler-managed Qwen 9B local LLM path...
python -m factorio_ai.cli slurm-ensure-worker --renew-before-minutes %FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES% || exit /b 1
python -m factorio_ai.cli slurm-ensure-vllm-service --duration-seconds %FACTORIO_AI_SCHEDULER_VLLM_SERVICE_DURATION_SECONDS% || exit /b 1
echo [factorio-ai] Checking scheduler local LLM readiness...
python -m factorio_ai.cli slurm-llm-status || exit /b 1

echo [factorio-ai] Starting opportunistic layout loop in a separate window...
start "Factorio AI Idle Layout Loop" cmd /k "cd /d ""%~dp0"" && run_factorio_no_mod_idle_layout_loop.bat"

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
