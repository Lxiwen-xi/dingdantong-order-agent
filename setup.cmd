@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Setup complete. Run start.cmd.
pause
exit /b 0
:failed
echo Setup failed. Check that Python 3.11 or newer is installed.
pause
exit /b 1
