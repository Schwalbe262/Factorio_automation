@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Restoring the latest backed-up Steam Factorio mod-list...
python -m factorio_ai.cli restore-steam-mod-list || exit /b 1
echo [factorio-ai] Steam Factorio mod-list restored.
