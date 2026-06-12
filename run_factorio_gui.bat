@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Installing development mod...
python -m factorio_ai.cli install-mod || exit /b 1

echo [factorio-ai] Launching GUI demo from the configured save...
python -m factorio_ai.cli launch-save-gui --window-size 1600x900 || exit /b 1

echo [factorio-ai] If Steam asks to continue with custom arguments, trying to click Continue...
python -m factorio_ai.cli confirm-steam-launch --timeout 20

echo [factorio-ai] GUI launch command finished.
