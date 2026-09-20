@echo off
setlocal
rem ============================================================
rem  Multi-Agent E-Commerce System - Python launcher
rem  Usage: double-click this file, or run:  start-python.bat [port]
rem  Notes: always calls python\.venv\Scripts\python.exe by ABSOLUTE
rem         path, so it can never pick up another project's venv.
rem ============================================================

cd /d "%~dp0python"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual env not found: python\.venv\Scripts\python.exe
    echo         Create:  python -m venv .venv
    echo         Install: .venv\Scripts\python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple -r requirements.txt
    goto :end
)

if not exist ".env" (
    echo [ERROR] .env is missing in python\
    echo         Create it:  copy .env.example .env
    echo         Then fill ECOM_LLM_API_KEY with your real key.
    goto :end
)

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

echo ============================================================
echo  Multi-Agent E-Commerce System - Python
echo  Interpreter : python\.venv\Scripts\python.exe
echo  Port        : %PORT%
echo  Swagger UI  : http://localhost:%PORT%/docs
echo  Health      : http://localhost:%PORT%/health
echo  Stop        : Ctrl+C
echo ============================================================
echo.

".venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port %PORT% --reload

:end
echo.
pause
