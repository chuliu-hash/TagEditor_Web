@echo off
REM ===== TagEditor_Web one-click launcher (Windows) =====
REM Usage: double-click this file, or run "run.bat" in cmd / PowerShell.
REM
REM What it does: find Python -> check .env -> check port -> start -> open browser
REM
REM WHY THIS FILE IS PURE ASCII (no Chinese anywhere, not even in comments):
REM cmd.exe parses batch files byte-by-byte using the system ANSI codepage
REM (GBK on Chinese Windows). A Chinese character in UTF-8 takes 3 bytes, but
REM GBK pairs bytes 2-at-a-time, so the decoder drifts out of alignment and
REM eventually swallows an ASCII letter as a trailing byte. The rest of the
REM line then gets misparsed AS A COMMAND. That is not just garbled display --
REM it can execute unintended commands. Keeping every byte ASCII makes the
REM file encoding-independent: UTF-8, GBK and ASCII are identical for it.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set ENV_NAME=tageditor
set PORT=8001

echo ============================================================
echo  TagEditor_Web - starting
echo ============================================================
echo.

REM ---------- 1. locate Python ----------
set PYEXE=
for %%P in (
    "D:\Miniconda3\envs\%ENV_NAME%\python.exe"
    "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe"
    "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\ProgramData\Miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe"
) do (
    if exist %%P if not defined PYEXE set PYEXE=%%~P
)
if not defined PYEXE (
    where conda >nul 2>nul
    if !errorlevel! equ 0 (
        for /f "delims=" %%i in ('conda info --base 2^>nul') do set CBASE=%%i
        if defined CBASE if exist "!CBASE!\envs\%ENV_NAME%\python.exe" set PYEXE=!CBASE!\envs\%ENV_NAME%\python.exe
    )
)
if not defined PYEXE (
    echo   [ERROR] conda env "%ENV_NAME%" not found.
    echo   Run setup.bat first, or create it manually:
    echo       conda create -n %ENV_NAME% python=3.11 -y
    echo.
    pause
    exit /b 1
)

REM ---------- 2. check Flask ----------
"%PYEXE%" -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo   [ERROR] flask is not installed in this environment.
    echo   Run setup.bat first.
    echo.
    pause
    exit /b 1
)

REM ---------- 3. check .env ----------
if not exist ".env" (
    echo   [NOTE] No .env config file found.
    if exist ".env.example" (
        echo   Creating one from .env.example ...
        copy /y ".env.example" ".env" >nul
        echo   Created .env - fill in your model endpoint and API keys, then start again.
        echo   File location: %CD%\.env
        echo.
        start "" notepad ".env"
        pause
        exit /b 0
    ) else (
        echo   [ERROR] .env.example is missing too, cannot create a config.
        pause
        exit /b 1
    )
)

REM ---------- 4. check port ----------
set BUSY=
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":%PORT% " ^| findstr "LISTENING"') do set BUSY=%%p
if defined BUSY (
    echo   [ERROR] Port %PORT% is already in use ^(PID %BUSY%^).
    echo.
    echo   The app may already be running - try opening:
    echo       http://127.0.0.1:%PORT%
    echo.
    echo   To restart it, kill the process first:
    echo       taskkill /PID %BUSY% /F
    echo.
    pause
    exit /b 1
)

REM ---------- 5. start ----------
echo   Python: %PYEXE%
echo   URL:    http://127.0.0.1:%PORT%
echo   Log:    %CD%\logs\tageditor.log
echo.
echo   Press Ctrl+C to stop.
echo ============================================================
echo.

REM Give Flask a moment before opening the browser
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:%PORT%"

"%PYEXE%" app.py
set RC=%errorlevel%

echo.
if %RC% neq 0 (
    echo   [ERROR] Server exited with code %RC%. Last log lines:
    echo ------------------------------------------------------------
    if exist "logs\tageditor.log" (
        powershell -NoProfile -Command "Get-Content 'logs\tageditor.log' -Tail 20"
    ) else (
        echo   ^(no log file^)
    )
    echo ------------------------------------------------------------
    echo   Full log: %CD%\logs\tageditor.log
)
endlocal
pause
