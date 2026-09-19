@echo off
REM ===== TagEditor_Web 一键启动（Windows）=====
REM 用法：双击本文件，或在 cmd / PowerShell 里执行 run.bat
REM
REM 做的事：找 Python → 查 .env → 查端口 → 启动 → 自动开浏览器
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set ENV_NAME=tageditor
set PORT=8001

echo ============================================================
echo  TagEditor_Web 启动
echo ============================================================
echo.

REM ---------- 1. 找 Python ----------
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
    echo   [错误] 找不到 conda 环境 "%ENV_NAME%"。
    echo   请先运行 setup.bat 安装，或手动创建：
    echo       conda create -n %ENV_NAME% python=3.11 -y
    echo.
    pause
    exit /b 1
)

REM ---------- 2. 检查 Flask ----------
"%PYEXE%" -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo   [错误] 该环境没装 flask，依赖似乎未安装。
    echo   请先运行 setup.bat。
    echo.
    pause
    exit /b 1
)

REM ---------- 3. 检查 .env ----------
if not exist ".env" (
    echo   [注意] 没找到 .env 配置文件。
    if exist ".env.example" (
        echo   正在从 .env.example 复制一份...
        copy /y ".env.example" ".env" >nul
        echo   已创建 .env —— 请填入模型地址和 API Key 后再启动。
        echo   文件位置: %CD%\.env
        echo.
        REM 用默认程序打开，方便直接编辑
        start "" notepad ".env"
        pause
        exit /b 0
    ) else (
        echo   [错误] .env.example 也不存在，无法自动创建配置。
        pause
        exit /b 1
    )
)

REM ---------- 4. 检查端口 ----------
set BUSY=
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":%PORT% " ^| findstr "LISTENING"') do set BUSY=%%p
if defined BUSY (
    echo   [错误] 端口 %PORT% 已被占用（PID %BUSY%）。
    echo.
    echo   可能程序已经在运行了 —— 先打开浏览器看看：
    echo       http://127.0.0.1:%PORT%
    echo.
    echo   若确认要重启，先结束占用进程：
    echo       taskkill /PID %BUSY% /F
    echo.
    pause
    exit /b 1
)

REM ---------- 5. 启动 ----------
echo   环境: %PYEXE%
echo   地址: http://127.0.0.1:%PORT%
echo   日志: %CD%\logs\tageditor.log
echo.
echo   按 Ctrl+C 停止服务。
echo ============================================================
echo.

REM 稍等再开浏览器（给 Flask 起来的時間）
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:%PORT%"

"%PYEXE%" app.py
set RC=%errorlevel%

echo.
if %RC% neq 0 (
    echo   [错误] 服务异常退出（代码 %RC%）。日志末尾：
    echo ------------------------------------------------------------
    if exist "logs\tageditor.log" (
        powershell -NoProfile -Command "Get-Content 'logs\tageditor.log' -Tail 20"
    ) else (
        echo   （没有日志文件）
    )
    echo ------------------------------------------------------------
    echo   完整日志: %CD%\logs\tageditor.log
)
endlocal
pause
