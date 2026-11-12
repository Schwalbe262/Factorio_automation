@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0..\..") do set "FACTORIO_REPO=%%~fI"
set "PYTHONPATH=%FACTORIO_REPO%\src"
pushd "%FACTORIO_REPO%" || exit /b 1
python -B -m factorio_agent.lifecycle --repo "%FACTORIO_REPO%" start %*
set "lifecycle_exit=%errorlevel%"
popd
if not defined FACTORIO_MENU_ACTIVE if not "%lifecycle_exit%"=="0" pause
exit /b %lifecycle_exit%
