@echo off
REM Starts the bot and the web dashboard on Windows.
REM
REM Just double-click this file. The first run installs everything it needs.
REM
REM Usage from a command prompt (optional):
REM   start.bat            the world that is set up (config.json here, or the
REM                        single world under worlds\)
REM   start.bat nl99      a named world, data under worlds\nl99\
REM
REM The dashboard opens on http://localhost:5000/

setlocal
cd /d "%~dp0"

set "WORLD=%~1"
set "PORT=5000"
set "VPY=%~dp0.venv\Scripts\python.exe"

REM --- No world name given? Use the one that is already set up --------------------
set "ABORT="
if not defined WORLD call :PICK_WORLD
if defined ABORT exit /b 1

REM --- A named world has to be set up already -------------------------------------
REM Without this a typo (nl99 -> n199) is indistinguishable from a new world: the
REM bot creates worlds\<typo>\, finds no config there and waits for a world nobody
REM is setting up, while this window shows nothing.
if defined WORLD if not exist "%~dp0worlds\%WORLD%\config.json" goto BAD_WORLD

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

:PICK_WORLD
REM A top-level config.json means this is a single-world install - leave it be.
REM Otherwise, if exactly one world under worlds\ is set up, start that instead
REM of the default bot: double-clicking this file would otherwise walk into the
REM first-run setup wizard and configure the same account a second time, and two
REM bots on one account log each other out.
if exist "%~dp0config.json" goto :EOF
set "COUNT=0"
set "FOUND="
for /d %%W in ("%~dp0worlds\*") do if exist "%%~fW\config.json" call :COUNT_WORLD "%%~nxW"
if "%COUNT%"=="0" goto :EOF
if not "%COUNT%"=="1" goto :MANY_WORLDS
set "WORLD=%FOUND%"
echo Using the only world that is set up: %FOUND%
echo.
goto :EOF

:MANY_WORLDS
echo Several worlds are set up under worlds\:
for /d %%W in ("%~dp0worlds\*") do if exist "%%~fW\config.json" echo    %%~nxW
echo.
echo Say which one to start, for example:  start.bat %FOUND%
echo.
pause
set "ABORT=1"
goto :EOF

:COUNT_WORLD
set /a COUNT+=1
set "FOUND=%~1"
goto :EOF

:BAD_WORLD
echo.
echo There is no world called "%WORLD%": worlds\%WORLD%\config.json does not exist.
set "ANY="
for /d %%W in ("%~dp0worlds\*") do if exist "%%~fW\config.json" set "ANY=1"
if defined ANY (
    echo Worlds set up here:
    for /d %%W in ("%~dp0worlds\*") do if exist "%%~fW\config.json" echo    %%~nxW
    echo.
    echo Check the spelling and start one of those, for example: start.bat nl99
) else (
    echo Open the dashboard and use "Add world" to set one up first.
)
echo.
pause
exit /b 1

:VERIFY_FAIL
echo.
echo It looks like the bot failed to start.
echo Try running setup.bat again to repair the installation.
echo If that does not fix it, please open an issue at
echo https://github.com/LazyTurtleStyle/TWBOT_LazyTurtle/issues
pause
goto :EOF
