@echo off
rem ===========================================================================
rem  WheresMyGPU - stop the GUI and/or the background monitor
rem
rem  Delegates to "python -m wheresmygpu --stop", which matches only python
rem  processes whose command line mentions wheresmygpu, so unrelated Python
rem  programs keep running. (Doing that matching in batch is unreliable:
rem  cmd.exe cannot carry a PowerShell pipeline through "for /f".)
rem
rem  NOTE: keep this file pure ASCII with CRLF line endings. cmd.exe reads .bat
rem  using the OEM code page (936/GBK here), so UTF-8 Chinese text desyncs the
rem  parser and silently corrupts the commands below it.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
if exist "D:\anaconda_root\python.exe" set "PYEXE=D:\anaconda_root\python.exe"
if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
    )
)

if not defined PYEXE (
    echo.
    echo   [ERROR] Python interpreter not found.
    echo.
    pause
    exit /b 1
)

echo.
echo   Stopping WheresMyGPU ...
echo.

"%PYEXE%" -m wheresmygpu --stop

echo.
timeout /t 3 >nul
endlocal