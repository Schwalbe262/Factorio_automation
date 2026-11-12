@echo off
setlocal
chcp 65001 >nul
set "PYTHONPATH=%~dp0src"
pushd "%~dp0"
python -B -m factorio_agent.lifecycle --repo "%~dp0." logs %*
set "lifecycle_exit=%errorlevel%"
popd
pause
exit /b %lifecycle_exit%
