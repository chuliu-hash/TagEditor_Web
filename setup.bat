@echo off
REM ===== TagEditor_Web dependency installer (Windows) =====
REM Usage: double-click this file, or run "setup.bat" in cmd / PowerShell.
REM
REM What it does: find conda env -> install deps -> handle the three packages
REM that plain pip cannot install correctly (CUDA torch, basicsr --no-deps,
REM onnxruntime-gpu's extra index). Safe to re-run: installed packages are skipped.
REM
REM WHY THIS FILE IS PURE ASCII (no Chinese anywhere, not even in comments):
REM cmd.exe parses batch files byte-by-byte using the system ANSI codepage
REM (GBK on Chinese Windows). A Chinese character in UTF-8 takes 3 bytes, but
REM GBK pairs bytes 2-at-a-time, so the decoder drifts out of alignment and
REM eventually swallows an ASCII letter as a trailing byte -- the rest of the
REM line then gets misparsed AS A COMMAND. That is not just garbled display;
REM it can execute unintended commands. Keeping every byte ASCII makes the
REM file encoding-independent: UTF-8, GBK and ASCII are identical for it.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set ENV_NAME=tageditor

echo ============================================================
echo  TagEditor_Web - installing dependencies
echo ============================================================
echo.

REM ---------- 1. locate Python ----------
echo [1/6] Looking for conda env "%ENV_NAME%" ...
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
    echo.
    echo   [ERROR] conda env "%ENV_NAME%" not found.
    echo   Create it first, then run this script again:
    echo.
    echo       conda create -n %ENV_NAME% python=3.11 -y
    echo.
    goto :fail
)
echo   Using: %PYEXE%
"%PYEXE%" --version
echo.

REM ---------- 2. base dependencies ----------
echo [2/6] Installing base dependencies ^(flask / numpy / pandas / opencv / openai ...^) ...
"%PYEXE%" -m pip install --upgrade pip -q
"%PYEXE%" -m pip install -r requirements.txt --upgrade-strategy only-if-needed
if errorlevel 1 (
    echo   [WARN] Some base packages failed; continuing with the special-case packages...
)
echo.

REM ---------- 3. torch (CUDA build) ----------
echo [3/6] Checking torch ...
"%PYEXE%" -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" >nul 2>nul
if !errorlevel! equ 0 (
    "%PYEXE%" -c "import torch; print('  CUDA torch already available:', torch.__version__, '| CUDA', torch.version.cuda)"
) else (
    echo   No usable CUDA torch found. Installing cu121 build ^(~2.5GB, takes a few minutes^)...
    echo   If your driver targets a different CUDA version, edit the cu121 in this line:
    echo     https://pytorch.org/get-started/locally/
    "%PYEXE%" -m pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu121
    if errorlevel 1 echo   [WARN] torch install failed; upscaling/background-removal will be unavailable ^(other features unaffected^)
)
echo.

REM ---------- 4. basicsr ----------
echo [4/6] Checking basicsr ...
"%PYEXE%" -c "import basicsr" >nul 2>nul
if !errorlevel! equ 0 (
    echo   Already installed, skipping
) else (
    echo   basicsr depends on the retired tb-nightly, so it needs --no-deps plus manual runtime deps...
    "%PYEXE%" -m pip install basicsr==1.4.2 --no-deps
    "%PYEXE%" -m pip install addict future lmdb scipy scikit-image tqdm yapf
    if errorlevel 1 echo   [WARN] basicsr install failed; upscaling/background-removal will be unavailable
)
echo.

REM ---------- 5. onnxruntime-gpu ----------
echo [5/6] Checking onnxruntime-gpu ^(used by WD14 tagging^) ...
"%PYEXE%" -c "import onnxruntime" >nul 2>nul
if !errorlevel! equ 0 (
    "%PYEXE%" -c "import onnxruntime as o; print('  Already installed:', o.__version__)"
    echo   [NOTE] requirements.txt pins 1.18.0; if you already have a newer version
    echo          that loads models fine, do NOT downgrade just to match this pin.
) else (
    echo   Installing onnxruntime-gpu ^(CUDA 12.x index^) ...
    "%PYEXE%" -m pip install onnxruntime-gpu==1.18.0 --extra-index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/
    if errorlevel 1 (
        echo   [WARN] GPU build failed; installing the CPU build instead ^(WD14 still works, just slower^)
        "%PYEXE%" -m pip install onnxruntime
    )
)
echo.

REM ---------- 6. self-check ----------
echo [6/6] Running environment self-check ...
"%PYEXE%" -c "import os,sys; sys.path.insert(0,'.'); exec(open('setup_check.py',encoding='utf-8').read())" 2>nul
if errorlevel 1 (
    echo   [NOTE] Self-check did not run; you can run it later with: python setup_check.py
)
echo.
echo ============================================================
echo  Done.
echo  Next: copy .env.example to .env, fill in your model endpoint
echo        and API keys, then run run.bat to start.
echo ============================================================
endlocal
pause
exit /b 0

:fail
endlocal
pause
exit /b 1
