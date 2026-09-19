@echo off
REM ===== TagEditor_Web 一键安装依赖（Windows）=====
REM 用法：双击本文件，或在 cmd / PowerShell 里执行 setup.bat
REM
REM 做的事：检查 conda 环境 → 装依赖 → 处理 pip 搞不定的三个包
REM         （torch 的 CUDA 版、basicsr 的 --no-deps、onnxruntime-gpu 的源）
REM 可重复执行：已装好的会跳过，不会重复下载。
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set ENV_NAME=tageditor

echo ============================================================
echo  TagEditor_Web 依赖安装
echo ============================================================
echo.

REM ---------- 1. 找 Python ----------
echo [1/6] 查找 conda 环境 "%ENV_NAME%" ...
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
    echo   [错误] 找不到 conda 环境 "%ENV_NAME%"。
    echo   请先创建它，再重新运行本脚本：
    echo.
    echo       conda create -n %ENV_NAME% python=3.11 -y
    echo.
    goto :fail
)
echo   使用: %PYEXE%
"%PYEXE%" --version
echo.

REM ---------- 2. 基础依赖 ----------
echo [2/6] 安装基础依赖（flask / numpy / pandas / opencv / openai 等）...
"%PYEXE%" -m pip install --upgrade pip -q
"%PYEXE%" -m pip install -r requirements.txt --upgrade-strategy only-if-needed
if errorlevel 1 (
    echo   [警告] 基础依赖有失败项，继续处理需要特殊步骤的包...
)
echo.

REM ---------- 3. torch（CUDA 版）----------
echo [3/6] 检查 torch ...
"%PYEXE%" -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" >nul 2>nul
if !errorlevel! equ 0 (
    "%PYEXE%" -c "import torch; print('  已有可用的 CUDA torch:', torch.__version__, '| CUDA', torch.version.cuda)"
) else (
    echo   未检测到可用的 CUDA torch，安装 cu121 版本（约 2.5GB，需要几分钟）...
    echo   如果显卡驱动对应别的 CUDA 版本，请改下面这行的 cu121：
    echo     https://pytorch.org/get-started/locally/
    "%PYEXE%" -m pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu121
    if errorlevel 1 echo   [警告] torch 安装失败，超清放大/背景移除将不可用（其余功能不受影响）
)
echo.

REM ---------- 4. basicsr ----------
echo [4/6] 检查 basicsr ...
"%PYEXE%" -c "import basicsr" >nul 2>nul
if !errorlevel! equ 0 (
    echo   已安装，跳过
) else (
    echo   basicsr 依赖已下架的 tb-nightly，必须用 --no-deps 装再补运行时依赖...
    "%PYEXE%" -m pip install basicsr==1.4.2 --no-deps
    "%PYEXE%" -m pip install addict future lmdb scipy scikit-image tqdm yapf
    if errorlevel 1 echo   [警告] basicsr 安装失败，超清放大/背景移除将不可用
)
echo.

REM ---------- 5. onnxruntime-gpu ----------
echo [5/6] 检查 onnxruntime-gpu（WD14 打标用）...
"%PYEXE%" -c "import onnxruntime" >nul 2>nul
if !errorlevel! equ 0 (
    "%PYEXE%" -c "import onnxruntime as o; print('  已安装:', o.__version__)"
    echo   [提示] requirements.txt 里钉的是 1.18.0；若你已装更高版本且能加载模型，
    echo          不要为了对齐版本而降级（本脚本不做降级）。
) else (
    echo   安装 onnxruntime-gpu（CUDA 12.x 专用源）...
    "%PYEXE%" -m pip install onnxruntime-gpu==1.18.0 --extra-index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/
    if errorlevel 1 (
        echo   [警告] GPU 版安装失败，改装 CPU 版（WD14 仍可用，只是慢些）
        "%PYEXE%" -m pip install onnxruntime
    )
)
echo.

REM ---------- 6. 自检 ----------
echo [6/6] 自检 ...
"%PYEXE%" -c "import os,sys; sys.path.insert(0,'.'); exec(open('setup_check.py',encoding='utf-8').read())" 2>nul
if errorlevel 1 (
    echo   [提示] 自检脚本未跑起来，可稍后手动执行: python setup_check.py
)
echo.
echo ============================================================
echo  安装完成。
echo  下一步：复制 .env.example 为 .env 并填写模型地址与密钥，
echo          然后运行 run.bat 启动。
echo ============================================================
endlocal
pause
exit /b 0

:fail
endlocal
pause
exit /b 1
