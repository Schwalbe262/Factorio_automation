@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Creating a fresh no-custom-mod save...
python -m factorio_ai.cli create-no-mod-save --overwrite || exit /b 1

echo [factorio-ai] Starting no-custom-mod LAN/RCON server in a separate window...
start "Factorio AI No-Mod Server" cmd /k "cd /d ""%~dp0"" && set PYTHONPATH=src && python -m factorio_ai.cli start-no-mod-server"

echo [factorio-ai] Waiting for RCON...
timeout /t 12 /nobreak >nul

echo [factorio-ai] Running no-custom-mod iron plate MVP...
python -m factorio_ai.cli run-no-mod-iron-mvp --target 10 --max-steps 120
if errorlevel 1 (
  echo [factorio-ai] Iron MVP failed. Check logs\iron-mvp-*.jsonl and logs\no-mod-server-live.log.
  pause
  exit /b 1
)

echo [factorio-ai] Iron MVP completed. The no-custom-mod server window remains open for web monitoring or GUI watch.
