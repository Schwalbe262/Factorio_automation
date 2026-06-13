@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Ensuring no-custom-mod vanilla-compatible save exists...
python -m factorio_ai.cli create-no-mod-save || exit /b 1

echo [factorio-ai] Starting no-custom-mod LAN/RCON server...
echo [factorio-ai] Other clients do not need the Factorio AI mod. They only need matching official Factorio/Space Age content.
python -m factorio_ai.cli start-no-mod-server
