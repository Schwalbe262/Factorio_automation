@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=src"
python -B -m factorio_agent --runtime runtime/v2/dev-101 watch
if errorlevel 1 pause
