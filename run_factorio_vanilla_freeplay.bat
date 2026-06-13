@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Launching Steam vanilla Factorio with only official Space Age mods enabled...
python -m factorio_ai.cli launch-vanilla-gui || exit /b 1

echo [factorio-ai] Starting a new Freeplay (Space Age) game from the main menu...
python -m factorio_ai.cli vanilla-start-freeplay || exit /b 1

echo [factorio-ai] Freeplay is open. Vanilla automation can now use normal keyboard and mouse input.
