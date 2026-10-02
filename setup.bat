@echo off
REM ===== TagEditor_Web dependency installer (Windows) =====
REM Usage: double-click this file, or run "setup.bat" in cmd / PowerShell.
REM
REM What it does: find conda env -> install deps -> handle the three packages
REM that plain pip cannot install correctly (CUDA torch, basicsr --no-deps,
REM onnxruntime-gpu). Safe to re-run: installed packages are skipped.
REM
REM WHY THIS FILE IS PURE ASCII (no Chinese anywhere, not even in comments):
REM cmd.exe parses batch files byte-by-byte using the system ANSI codepage
REM (GBK on Chinese Windows). A Chinese character in UTF-8 takes 3 bytes, but
REM GBK pairs bytes 2-at-a-time, so the decoder drifts out of alignment and
REM eventually swallows an ASCII letter as a trailing byte -- the rest of the
REM line then gets misparsed AS A COMMAND. That is not just garbled display;
REM it can execute unintended commands. Keeping every byte ASCII makes the
REM file encoding-independent: UTF-8, GBK and ASCII are identical for it.
REM verify.bat lives under the same rule (and both files must stay CRLF).
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
    "%PYEXE%" -m pip install addict future lmdb pyyaml scipy scikit-image tqdm yapf
    if errorlevel 1 echo   [WARN] basicsr install failed; upscaling/background-removal will be unavailable
)
echo.

REM ---------- 5. onnxruntime-gpu ----------
REM Two DIFFERENT questions, asked in this order:
REM   (a) is it installed at all?  -> plain "import onnxruntime"
REM   (b) does CUDA really work?   -> build a real CUDA InferenceSession and
REM                                   read back session.get_providers()[0]
REM (b) is the one that matters. onnxruntime-gpu 1.26.0 (the version pinned in
REM requirements.txt) needs cuDNN 9.x, i.e. the nvidia-cudnn-cu12 package. When
REM those DLLs are missing, the import still succeeds and
REM get_available_providers() still advertises CUDAExecutionProvider, but the
REM session silently settles on CPUExecutionProvider -- WD14 tagging then runs
REM on the CPU (about 10x slower) with no error and no warning anywhere.
echo [5/6] Probing onnxruntime-gpu CUDA support ^(used by WD14 tagging^) ...
"%PYEXE%" -c "import onnxruntime" >nul 2>nul
if !errorlevel! neq 0 goto :ort_install

"%PYEXE%" -c "import os,sys;os.environ['ORT_LOG_SEVERITY_LEVEL']='4';import onnxruntime as ort;av='CUDAExecutionProvider' in ort.get_available_providers();mp=os.path.join('models','wd-eva02-large-tagger-v3','model.onnx');have=os.path.isfile(mp);so=ort.SessionOptions();so.log_severity_level=4;s=ort.InferenceSession(mp,so,providers=['CUDAExecutionProvider','CPUExecutionProvider']) if have else None;p=s.get_providers()[0] if s is not None else '';print('  onnxruntime-gpu',ort.__version__,'| settled provider:',p if have else 'not probed, model file missing');sys.exit(0 if p=='CUDAExecutionProvider' else (3 if not have else (1 if av else 2)))" 2>nul
set ORT_RC=!errorlevel!
if !ORT_RC! equ 0 goto :ort_ok
if !ORT_RC! equ 1 goto :ort_cudnn
if !ORT_RC! equ 2 goto :ort_cpu
goto :ort_nomodel

:ort_ok
echo   [OK] CUDA provider is live: WD14 tagging runs on the GPU.
goto :ort_done

:ort_cudnn
echo   [WARN] onnxruntime-gpu is installed but CUDA is NOT usable ^(see the settled
echo          provider above^): WD14 tagging runs on the CPU, about 10x slower.
echo          Results are still correct. Cause: onnxruntime-gpu 1.26.0 needs
echo          cuDNN 9.x, and no nvidia-cudnn-cu12 / nvidia-cublas-cu12 is present.
echo          Fix ^(this script will NOT do it for you -- an installer must not
echo          silently change an environment that already works^):
echo              pip install nvidia-cudnn-cu12 nvidia-cublas-cu12
echo          Then confirm with: python setup_check.py
goto :ort_done

:ort_cpu
echo   [WARN] This is the CPU-only onnxruntime build: WD14 tagging works but is
echo          about 10x slower. requirements.txt pins the GPU build instead:
echo              pip install onnxruntime-gpu==1.26.0
echo          ^(that build needs cuDNN 9.x: pip install nvidia-cudnn-cu12 nvidia-cublas-cu12^)
goto :ort_done

:ort_nomodel
echo   [WARN] onnxruntime-gpu is installed, but models\wd-eva02-large-tagger-v3\model.onnx
echo          is missing, so CUDA could not be probed. WD14 tagging needs that file.
goto :ort_done

:ort_install
echo   onnxruntime is not importable (missing, or broken). Installing onnxruntime-gpu 1.26.0 ...
"%PYEXE%" -m pip install onnxruntime-gpu==1.26.0
if errorlevel 1 (
    echo   [WARN] GPU build failed; installing the CPU build instead ^(WD14 still works, just slower^)
    "%PYEXE%" -m pip install onnxruntime
)

:ort_done
echo.

REM ---------- 6. self-check ----------
REM setup_check.py exits 1 when it listed at least one issue, 0 when clean.
REM Any other exit code means the script itself did not run (broken python,
REM syntax error...), which must NOT be reported as an environment issue.
REM stderr is dropped on purpose: when the CUDA provider cannot load, onnxruntime
REM prints a long C++ level error that no Python-side severity option silences.
REM The report itself (stdout) is always shown; if the script dies instead, the
REM note below points the user at a direct run, where the traceback is visible.
echo [6/6] Running environment self-check ...
"%PYEXE%" -c "import os,sys; sys.path.insert(0,'.'); exec(open('setup_check.py',encoding='utf-8').read())" 2>nul
set CHK_RC=!errorlevel!
if !CHK_RC! equ 0 (
    echo   Self-check passed: no missing pieces, no known degradation.
) else (
    if !CHK_RC! equ 1 (
        echo   [WARN] The self-check listed environment issues ^(report above^).
        echo          Installation is complete; only the listed features are
        echo          missing or run slower. Re-check anytime with:
        echo              python setup_check.py
    ) else (
        echo   [NOTE] The self-check did not run ^(exit code !CHK_RC!^).
        echo          Run it directly to see why: python setup_check.py
    )
)
echo.
echo ============================================================
echo  Done.
echo  Next: copy .env.example to .env, fill in your model endpoint
echo        and API keys, then run run.bat to start.
echo  Verify: verify.bat runs the local test scripts.
echo ============================================================
endlocal
pause
exit /b 0

:fail
endlocal
pause
exit /b 1
