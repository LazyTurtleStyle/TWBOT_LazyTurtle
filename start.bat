@echo off
REM Starts the bot and the web dashboard on Windows.
REM
REM Just double-click this file. The first run installs everything it needs.
REM
REM Usage from a command prompt (optional):
REM   start.bat            single world (config.json in this folder)
REM   start.bat nl99      a named world, data under worlds\nl99\
REM
REM The dashboard opens on http://localhost:5000/

setlocal
cd /d "%~dp0"

set "WORLD=%~1"
set "PORT=5000"
set "VPY=%~dp0.venv\Scripts\python.exe"

REM --- First run? Install everything first ---------------------------------------
if not exist "%VPY%" (
    echo First run detected - setting up. This only happens once.
    echo.
    call "%~dp0setup.bat"
    if errorlevel 1 exit /b 1
)

REM --- Sanity check --------------------------------------------------------------
echo Verifying bot integrity
if defined WORLD (
    "%VPY%" twb.py -i --world %WORLD%
) else (
    "%VPY%" twb.py -i
)
if errorlevel 1 goto VERIFY_FAIL

REM --- Dashboard in its own (minimised) window -----------------------------------
REM server.py does sys.path.insert(0, "../"), so it must run from webmanager\.
echo Starting the dashboard on http://localhost:%PORT%/
start "TWB dashboard" /D "%~dp0webmanager" /min "%VPY%" server.py %PORT% 0.0.0.0

REM Give it a moment to bind the port before the browser opens.
timeout /t 3 /nobreak >NUL
start "" "http://localhost:%PORT%/"

REM --- Bot in this window --------------------------------------------------------
echo.
echo Starting the bot. Close this window (or press Ctrl-C) to stop it.
echo.
if defined WORLD (
    "%VPY%" twb.py --world %WORLD%
) else (
    "%VPY%" twb.py
)
goto :EOF

:VERIFY_FAIL
echo.
echo It looks like the bot failed to start.
echo Try running setup.bat again to repair the installation.
echo If that does not fix it, please open an issue at
echo https://github.com/LazyTurtleStyle/TWBOT_LazyTurtle/issues
pause
goto :EOF
