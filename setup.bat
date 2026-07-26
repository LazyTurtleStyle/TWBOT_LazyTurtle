@echo off
REM One-time setup for Windows.
REM
REM Creates a private virtualenv in .venv\ and installs the dependencies there,
REM so nothing is installed system-wide. Safe to re-run: it upgrades the
REM dependencies of an existing .venv (do this after every bot update).
REM
REM Normally you do not need to run this yourself - start.bat calls it
REM automatically the first time.

setlocal
cd /d "%~dp0"

echo ==========================================
echo   TWB setup
echo ==========================================
echo.

REM --- Find a usable Python (3.10+) --------------------------------------------
set "PY="

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >NUL 2>&1
if not errorlevel 1 set "PY=py -3"

if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >NUL 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    echo Python 3.10 or newer was not found on this PC.
    echo.
    echo The download page will open now. During installation, MAKE SURE to tick
    echo    [x] Add Python to PATH
    echo at the bottom of the first screen, then run this again.
    echo.
    start "" "https://www.python.org/downloads/windows/"
    pause
    exit /b 1
)

for /f "delims=" %%V in ('%PY% -V') do set "PYVER=%%V"
echo Found %PYVER%
echo.

REM --- Create / reuse the virtualenv --------------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo Reusing existing virtualenv in .venv\
) else (
    echo Creating virtualenv in .venv\
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo Could not create the virtualenv. Try re-installing Python from
        echo https://www.python.org/downloads/windows/ with "Add Python to PATH" ticked.
        pause
        exit /b 1
    )
)

set "VPY=%~dp0.venv\Scripts\python.exe"

REM --- Dependencies --------------------------------------------------------------
echo.
echo Installing dependencies...
"%VPY%" -m pip install --upgrade pip >NUL
"%VPY%" -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo.
    echo Installing the dependencies failed. Check your internet connection and
    echo try again. If it keeps failing, open an issue at
    echo https://github.com/LazyTurtleStyle/TWBOT_LazyTurtle/issues
    pause
    exit /b 1
)

REM --- Verify ----------------------------------------------------------------------
echo.
echo Verifying bot integrity...
"%VPY%" twb.py -i
if errorlevel 1 (
    echo.
    echo The bot failed its integrity check - see the output above.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   Setup complete. Close this window and
echo   double-click start.bat to run the bot.
echo ==========================================
echo.
pause
exit /b 0
