@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

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

echo [factorio-ai] Starting continuous no-custom-mod autopilot.
echo [factorio-ai] Use Ctrl+C in this window to stop the autopilot loop.
python -m factorio_ai.cli run-no-mod-autopilot --objective launch_rocket_program --cycles 0 --sleep-seconds 5
