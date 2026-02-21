@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "LAUNCHER=launcher.py"

REM --- Resolve the folder this .bat lives in ---
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul

if not exist "%LAUNCHER%" (
  echo.
  echo ERROR: "%LAUNCHER%" not found in:
  echo   %SCRIPT_DIR%
  echo.
  echo Put this .bat next to your launcher script, or edit LAUNCHER at the top.
  pause
  popd >nul
  exit /b 1
)

:CHECK_PYTHON
echo.
echo Checking for Python...

REM Prefer the Python Launcher (py.exe) if present
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -c "import sys; print(sys.version)" >nul 2>nul
  if %errorlevel%==0 (
    set "PY_CMD=py -3"
    goto RUN_LAUNCHER
  )
)

REM Fallback: python in PATH
where python >nul 2>nul
if %errorlevel%==0 (
  python -c "import sys; print(sys.version)" >nul 2>nul
  if %errorlevel%==0 (
    set "PY_CMD=python"
    goto RUN_LAUNCHER
  )
)

echo Python was NOT detected on PATH (or it's not runnable).
echo.
echo Choose an install method:
echo   [1] Install via winget (recommended)
echo   [2] Open Microsoft Store Python page
echo   [3] Open python.org downloads page
echo   [4] Exit
echo.

set /p choice=Enter 1-4: 

if "%choice%"=="1" goto INSTALL_WINGET
if "%choice%"=="2" goto OPEN_STORE
if "%choice%"=="3" goto OPEN_PYORG
if "%choice%"=="4" goto EXIT_SCRIPT

echo Invalid choice.
goto CHECK_PYTHON

:INSTALL_WINGET
where winget >nul 2>nul
if not %errorlevel%==0 (
  echo.
  echo winget is not available on this system.
  echo Use option [2] or [3] instead.
  goto CHECK_PYTHON
)

echo.
echo Attempting Python install via winget...
echo (If prompted by Windows/UAC, accept the install.)
echo.

REM Try 3.12 first; if it fails, try 3.11
winget install -e --id Python.Python.3.12
if not %errorlevel%==0 (
  echo.
  echo winget install for 3.12 failed; trying Python 3.11...
  winget install -e --id Python.Python.3.11
)

echo.
echo Re-checking Python after install attempt...
goto CHECK_PYTHON

:OPEN_STORE
echo Opening Microsoft Store...
start "" "ms-windows-store://pdp/?productid=9PJPW5LDXLZ5"
echo After installing, come back here and press any key to re-check.
pause >nul
goto CHECK_PYTHON

:OPEN_PYORG
echo Opening python.org downloads...
start "" "https://www.python.org/downloads/windows/"
echo After installing, come back here and press any key to re-check.
pause >nul
goto CHECK_PYTHON

:RUN_LAUNCHER
echo.
echo Python detected. Using: %PY_CMD%
echo Running %LAUNCHER%...
echo.

%PY_CMD% "%LAUNCHER%"
set "EXITCODE=%errorlevel%"

echo.
echo Launcher exited with code %EXITCODE%.
pause

popd >nul
exit /b %EXITCODE%

:EXIT_SCRI_
