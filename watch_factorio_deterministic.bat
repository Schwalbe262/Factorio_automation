@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
rem Current development server. Later arguments override defaults or hide its window.
python -m factorio_ai.deterministic_cli watch-deterministic --runtime runtime/deterministic/adapter-smoke --server-port 34210 --rcon-port 27025 %*
if errorlevel 1 pause
