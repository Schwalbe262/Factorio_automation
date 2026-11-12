@echo off
setlocal
chcp 65001 >nul
if /I "%~1"=="--help" goto help
if /I "%~1"=="-h" goto help
if /I "%~1"=="/?" goto help
if not "%~1"=="" goto usage_error
set "FACTORIO_MENU_ACTIVE=1"

:menu
cls
call :print_menu
choice /C 123456780 /N /M "선택: "
if errorlevel 255 goto menu_exit
if errorlevel 9 goto menu_exit
if errorlevel 8 goto shutdown
if errorlevel 7 goto config
if errorlevel 6 goto logs
if errorlevel 5 goto status
if errorlevel 4 goto gui
if errorlevel 3 goto new_map
if errorlevel 2 goto stop
if errorlevel 1 goto start
goto menu_exit

:start
call "%~dp0tools\windows\start.bat"
set "menu_exit_code=%errorlevel%"
goto result

:stop
call "%~dp0tools\windows\stop.bat"
set "menu_exit_code=%errorlevel%"
goto result

:new_map
call "%~dp0tools\windows\new_map.bat"
set "menu_exit_code=%errorlevel%"
goto result

:gui
call "%~dp0tools\windows\gui.bat"
set "menu_exit_code=%errorlevel%"
goto result

:status
call "%~dp0tools\windows\status.bat"
set "menu_exit_code=%errorlevel%"
goto result

:logs
call "%~dp0tools\windows\logs.bat"
set "menu_exit_code=%errorlevel%"
goto result

:config
start "" /wait notepad.exe "%~dp0automation_config.json"
set "menu_exit_code=%errorlevel%"
goto result

:shutdown
call "%~dp0tools\windows\shutdown.bat"
set "menu_exit_code=%errorlevel%"
goto result

:result
echo.
echo 완료 코드: %menu_exit_code%
pause
goto menu

:print_menu
echo Factorio 자동화
echo.
echo [1] 자동 시작 / 현재 월드 재개
echo [2] 게임 + AI 일시정지 / 저장 / GUI 유지
echo [3] 새 맵 생성 / 현재 월드 저장 + 백업 후 전환
echo [4] GUI 열기
echo [5] 상태 보기
echo [6] 로그 보기
echo [7] automation_config.json 편집 / 메모장
echo [8] 저장 + 전체 종료
echo [0] 메뉴만 닫기 / 게임 실행 상태 유지
echo.
exit /b 0

:help
echo Usage: Factorio.bat [--help]
echo 메뉴를 열어 번호를 선택하세요. 도움말은 게임을 시작하지 않습니다.
echo.
call :print_menu
exit /b 0

:usage_error
echo Usage: Factorio.bat [--help]
echo 추가 실행 인수는 tools\windows의 개별 BAT에서 사용하세요.
exit /b 2

:menu_exit
exit /b 0
