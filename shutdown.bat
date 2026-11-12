@echo off
setlocal
chcp 65001 >nul
set "PYTHONPATH=%~dp0src"
pushd "%~dp0"
python -B -m factorio_agent.lifecycle --repo "%~dp0." shutdown %*
set "lifecycle_exit=%errorlevel%"
popd
if not "%lifecycle_exit%"=="0" pause
exit /b %lifecycle_exit%
