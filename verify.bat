@echo off
REM ===== TagEditor_Web local verification (Windows) =====
REM Usage: double-click this file, or run "verify.bat" in cmd / PowerShell.
REM
REM Runs the two stdlib-only test scripts with the tageditor env python and
REM aggregates their exit codes: exit 0 only when BOTH pass. No pytest, no
REM network, no third-party test framework.
REM
REM WHY THIS FILE IS PURE ASCII (no Chinese anywhere, not even in comments):
REM cmd.exe parses batch files byte-by-byte using the system ANSI codepage
REM (GBK on Chinese Windows). A Chinese character in UTF-8 takes 3 bytes, but
REM GBK pairs bytes 2-at-a-time, so the decoder drifts out of alignment and
REM eventually swallows an ASCII letter as a trailing byte -- the rest of the
REM line then gets misparsed AS A COMMAND. Keeping every byte ASCII makes the
REM file encoding-independent (UTF-8 / GBK / ASCII are identical for it).
REM This file must also stay CRLF: a stray LF-only line can break label parsing.
setlocal
cd /d "%~dp0"

set ENV_NAME=tageditor
set PYEXE=
for %%P in (
    "D:\Miniconda3\envs\%ENV_NAME%\python.exe"
    "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe"
    "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\ProgramData\Miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe"
) do if exist %%P if not defined PYEXE set PYEXE=%%~P
if not defined PYEXE set PYEXE=python
echo Using: %PYEXE%
echo.

set RC=0

echo [1/2] tests\test_invariants.py  (the "do not regress" assertions)
"%PYEXE%" tests\test_invariants.py
if errorlevel 1 set RC=1
echo.

echo [2/2] tests\test_mutations.py   (each assertion vs. a real injected defect)
"%PYEXE%" tests\test_mutations.py
if errorlevel 1 set RC=1
echo.

if %RC% equ 0 (
    echo verify: PASS - both scripts green
) else (
    echo verify: FAIL - see the output above
)

endlocal & exit /b %RC%
