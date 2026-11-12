@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0..\..") do set "FACTORIO_REPO=%%~fI"
set "PYTHONPATH=%FACTORIO_REPO%\src"
pushd "%FACTORIO_REPO%" || exit /b 1
python -B -m factorio_agent.lifecycle --repo "%FACTORIO_REPO%" status %*
set "lifecycle_exit=%errorlevel%"
popd
if not defined FACTORIO_MENU_ACTIVE pause
exit /b %lifecycle_exit%
