@echo off
rem ===========================================================================
rem  WheresMyGPU - console launcher (continuous monitoring)
rem
rem  For the graphical interface, double-click run_gui.bat instead.
rem
rem  Usage:  run.bat                  continuous monitoring (default)
rem          run.bat --once           one round, then exit
rem          run.bat --check          validate config + SSH + nvidia-smi
rem          run.bat --test-notify    send a test notification
rem          run.bat --gui            open the graphical interface
rem          run.bat --stop           stop running WheresMyGPU processes
rem          run.bat --help           list all options
rem
rem  NOTE: keep this file pure ASCII with CRLF line endings. cmd.exe reads .bat
rem  using the OEM code page (936/GBK here), so UTF-8 Chinese text desyncs the
rem  parser and silently corrupts the commands below it.
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PYEXE="

rem 1) the Anaconda interpreter already verified on this machine
if exist "D:\anaconda_root\python.exe" set "PYEXE=D:\anaconda_root\python.exe"

rem 2) whatever "python" resolves to on PATH
if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
    )
)

rem 3) the py launcher
if not defined PYEXE (
    where py >nul 2>nul && set "PYEXE=py"
)

if not defined PYEXE (
    echo.
    echo   [ERROR] Python interpreter not found.
    echo   Install Python 3.9+ ^(tick "Add python.exe to PATH"^), or edit PYEXE here.
    echo.
    pause
    exit /b 1
)

rem dependency self-check; installs on first run
"%PYEXE%" -c "import paramiko, yaml, requests" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Installing dependencies ^(paramiko / PyYAML / requests^) ...
    "%PYEXE%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   [ERROR] Dependency install failed. Run this manually:
        echo          "%PYEXE%" -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
)

echo   Interpreter: %PYEXE%
echo.

"%PYEXE%" -m wheresmygpu %*

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo   Exit code: %RC%
    echo   For a diagnosis, run:  run.bat --check
    echo.
    pause
)

endlocal & exit /b %RC%