@echo off
rem ===========================================================================
rem  WheresMyGPU - GUI launcher
rem
rem  NOTE: keep this file pure ASCII. cmd.exe reads .bat with the OEM code page
rem  (936/GBK on Chinese Windows), so UTF-8 Chinese text desyncs the parser and
rem  silently corrupts the commands. All Chinese UI text lives in Python.
rem
rem  Double-click this file to open the interface.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
set "PYWEXE="

rem Prefer the interpreter already verified on this machine.
if exist "D:\anaconda_root\pythonw.exe" set "PYWEXE=D:\anaconda_root\pythonw.exe"
if exist "D:\anaconda_root\python.exe"  set "PYEXE=D:\anaconda_root\python.exe"

rem Fall back to whatever is on PATH.
if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
    )
)
if not defined PYWEXE (
    for /f "delims=" %%P in ('where pythonw 2^>nul') do (
        if not defined PYWEXE set "PYWEXE=%%P"
    )
)

if not defined PYEXE (
    echo.
    echo   [ERROR] Python interpreter not found.
    echo   Install Python 3.9+ ^(tick "Add python.exe to PATH"^), or edit PYEXE in this file.
    echo.
    pause
    exit /b 1
)

rem Dependency self-check; installs on first run.
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

rem Launch with pythonw.exe so no console window appears.
if defined PYWEXE (
    start "" "%PYWEXE%" -m wheresmygpu --gui
    exit /b 0
)

rem No pythonw available: run in this console so errors stay visible.
"%PYEXE%" -m wheresmygpu --gui
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo   Exit code: %RC%
    echo   See logs\wheresmygpu-*.log for details.
    echo.
    pause
)
endlocal & exit /b %RC%