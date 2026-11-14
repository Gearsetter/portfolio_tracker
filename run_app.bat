@echo off
rem IBKR Portfolio Tracker - start the app (Windows).
rem First run: creates a private virtual environment in .venv and installs requirements.txt.
rem Optional: set IBKR_TRACKER_PORT=8600  /  set IBKR_TRACKER_HOME=D:\some\folder  before starting.
setlocal
cd /d "%~dp0"
if "%IBKR_TRACKER_PORT%"=="" set "IBKR_TRACKER_PORT=8501"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python 3.10+ was not found. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^)
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating the virtual environment ^(.venv^) - first start only ...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 echo Warning: could not install/verify the requirements ^(offline?^) - trying to start anyway.

echo Starting IBKR Portfolio Tracker on http://localhost:%IBKR_TRACKER_PORT%  ^(close this window or press Ctrl+C to stop^)
start "" /b ".venv\Scripts\pythonw.exe" scripts\open_when_ready.py %IBKR_TRACKER_PORT%
".venv\Scripts\python.exe" -m streamlit run app.py --server.port %IBKR_TRACKER_PORT% %*
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Something went wrong - see the messages above.
pause
exit /b 1
