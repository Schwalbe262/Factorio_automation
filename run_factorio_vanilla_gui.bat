@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Launching achievement-compatible vanilla Factorio through Steam...
python -m factorio_ai.cli launch-vanilla-gui || exit /b 1

echo [factorio-ai] This path uses only official Space Age mods, no RCON, and no Lua commands.
echo [factorio-ai] Vanilla automation must use screen/window capture and normal keyboard/mouse input only.
echo [factorio-ai] Keep Factorio open. Do not minimize it until run_factorio_vanilla_probe.bat proves minimized capture/input.
