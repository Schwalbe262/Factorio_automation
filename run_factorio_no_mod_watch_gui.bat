@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Ensuring no-custom-mod vanilla-compatible save exists...
python -m factorio_ai.cli create-no-mod-save || exit /b 1

echo [factorio-ai] Starting no-custom-mod LAN/RCON server in a separate window...
start "Factorio AI No-Mod Server" cmd /k "cd /d ""%~dp0"" && set PYTHONPATH=src && python -m factorio_ai.cli start-no-mod-server"

echo [factorio-ai] Waiting for RCON...
timeout /t 12 /nobreak >nul

echo [factorio-ai] Opening GUI client connected to the no-custom-mod server...
python -m factorio_ai.cli launch-no-mod-gui --window-size 1600x900
if errorlevel 1 (
  echo [factorio-ai] GUI watch failed. Check the printed error above.
  pause
  exit /b 1
)

echo [factorio-ai] GUI watch launched. Close Factorio when you are done inspecting.
