@echo off
setlocal
rem ============================================================
rem  Multi-Agent E-Commerce System - Python launcher
rem  Usage: double-click this file, or run:  start-python.bat [port] [reload]
rem  Notes: always calls backend\.venv\Scripts\python.exe by ABSOLUTE
rem         path, so it can never pick up another project's venv.
rem
rem  CHANGED 2026-09-22:
rem    - directory was renamed python\ -> backend\; this script still
rem      said "python", so it failed at the very first line with
rem      "The system cannot find the path specified".
rem    - --reload is now OPT-IN (pass "reload" as the 2nd argument).
rem      Reason: uvicorn's reloader kills the whole child process tree on
rem      restart, and the MCP stdio subprocess dies first -- the symptom is
rem      a silent hang or BrokenPipe with no traceback pointing at the cause.
rem      For a demo run, plain serving is the safe default.
rem ============================================================

cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual env not found: backend\.venv\Scripts\python.exe
    echo         Create:  python -m venv .venv
    echo         Install: .venv\Scripts\python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple -r requirements.txt
    goto :end
)

if not exist ".env" (
    echo [ERROR] .env is missing in backend\
    echo         Create it:  copy .env.example .env
    echo         Then fill ECOM_LLM_API_KEY with your real key.
    goto :end
)

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

set "RELOAD_FLAG="
if /I "%~2"=="reload" set "RELOAD_FLAG=--reload"

echo ============================================================
echo  Multi-Agent E-Commerce System - Python
echo  Interpreter : backend\.venv\Scripts\python.exe
echo  Port        : %PORT%
echo  Swagger UI  : http://localhost:%PORT%/docs
echo  Health      : http://localhost:%PORT%/health
echo  Reload      : %RELOAD_FLAG%
echo  Stop        : Ctrl+C
echo ============================================================
echo.
if not defined RELOAD_FLAG echo  Tip: pass "reload" as 2nd arg for hot reload, e.g.
if not defined RELOAD_FLAG echo       start-python.bat 8000 reload
if not defined RELOAD_FLAG echo.
echo  NOTE: do NOT add --reload while MCP is enabled. The reloader kills
echo        the MCP stdio subprocess on every restart (silent hang).
echo.

".venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port %PORT% %RELOAD_FLAG%

:end
echo.
pause
