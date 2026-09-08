@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
rem Current development server. Arguments can select another completed run.
if "%~1"=="" (
  python -m factorio_ai.deterministic_cli watch-deterministic --runtime runtime/deterministic/adapter-smoke --server-port 34210 --rcon-port 27025
) else (
  python -m factorio_ai.deterministic_cli watch-deterministic %*
)
if errorlevel 1 pause
