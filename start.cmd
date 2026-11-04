@echo off
setlocal
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Python environment not found. Run setup.cmd first.
    pause
    exit /b 1
)
"%PYTHON%" "%~dp0run.py"
pause
