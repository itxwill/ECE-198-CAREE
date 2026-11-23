@echo off
setlocal
set "CAREE_PROJECT=%~dp0"
set "CAREE_LAUNCHER=%~f0"

rem Optional overrides: CAREE_PYTHON and CAREE_PIO can be executable paths.
if not defined CAREE_PYTHON set "CAREE_PYTHON=python"
if not defined CAREE_PIO set "CAREE_PIO=%USERPROFILE%\.platformio\penv\Scripts\platformio.exe"

if /i "%~1"=="--web" goto web
set "CAREE_UPLOAD=0"
set "CAREE_CHECK=0"
set "CAREE_PORT="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="upload" (
    set "CAREE_UPLOAD=1"
    shift
    goto parse
)
if /i "%~1"=="--upload" (
    set "CAREE_UPLOAD=1"
    shift
    goto parse
)
if /i "%~1"=="--check" (
    set "CAREE_CHECK=1"
    shift
    goto parse
)
if defined CAREE_PORT (
    echo Usage: start-caree.bat [upload] [COM13] [--check]
    exit /b 1
)
set "CAREE_PORT=%~1"
shift
goto parse

:parsed

if not exist "%CAREE_PIO%" (
    echo PlatformIO was not found at "%CAREE_PIO%".
    echo Set CAREE_PIO to the full path of platformio.exe and try again.
    exit /b 1
)
if not exist "%CAREE_PROJECT%caree-esp\platformio.ini" (
    echo Missing caree-esp\platformio.ini beside this launcher.
    exit /b 1
)
if not exist "%CAREE_PROJECT%web\webapp.py" (
    echo Missing web\webapp.py beside this launcher.
    exit /b 1
)
"%CAREE_PYTHON%" -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo Python or Flask is unavailable. Activate your Python environment first,
    echo or set CAREE_PYTHON to its python.exe path.
    exit /b 1
)

if "%CAREE_CHECK%"=="1" (
    "%CAREE_PIO%" --version
    echo Upload enabled: %CAREE_UPLOAD% ^(0=no, 1=yes^)
    echo Serial port: %CAREE_PORT% ^(blank=automatic^)
    echo Project paths and Python/Flask checks passed. Nothing was launched.
    exit /b 0
)

echo Starting the dashboard in a separate window...
start "CAREE Flask dashboard" "%ComSpec%" /k ""%CAREE_LAUNCHER%" --web"
echo Dashboard: http://localhost:5000
if "%CAREE_UPLOAD%"=="0" (
    echo Skipping firmware upload.
    goto monitor
)
echo Uploading firmware in this window. Flask runs independently.
echo Close any serial monitor before uploading.
echo If Connecting stalls, hold BOOT and tap EN until writing starts.
echo.

if not defined CAREE_PORT (
    rem PlatformIO automatically selects the upload port when none is supplied.
    "%CAREE_PIO%" run --project-dir "%CAREE_PROJECT%caree-esp" --target upload
) else (
    "%CAREE_PIO%" run --project-dir "%CAREE_PROJECT%caree-esp" --target upload --upload-port "%CAREE_PORT%"
)
set "CAREE_UPLOAD_RESULT=%ERRORLEVEL%"
echo.
if "%CAREE_UPLOAD_RESULT%"=="0" (
    echo Upload complete.
    goto monitor
) else (
    echo Upload failed. The dashboard window stays running.
    echo Retry with an explicit port, for example: start-caree.bat upload COM13
)
pause
exit /b %CAREE_UPLOAD_RESULT%

:monitor
echo Opening serial monitor at 115200 baud...
echo Flask continues running in its own window.
echo Press Ctrl+C to stop monitoring before the next upload.
echo Press EN on the ESP32 if you want to see its startup messages again.
pushd "%CAREE_PROJECT%caree-esp"
if not defined CAREE_PORT (
    "%CAREE_PIO%" device monitor --baud 115200
) else (
    "%CAREE_PIO%" device monitor --baud 115200 --port "%CAREE_PORT%"
)
set "CAREE_MONITOR_RESULT=%ERRORLEVEL%"
popd
if not "%CAREE_MONITOR_RESULT%"=="0" (
    echo Serial monitor exited with an error. Check the port and close other monitors.
    pause
)
exit /b %CAREE_MONITOR_RESULT%

:web
cd /d "%CAREE_PROJECT%web"
echo CAREE dashboard: http://localhost:5000
echo Press Ctrl+C in this window to stop Flask.
echo If port 5000 is already in use, stop the previous Flask instance first.
"%CAREE_PYTHON%" "%CAREE_PROJECT%web\webapp.py"
exit /b %ERRORLEVEL%
