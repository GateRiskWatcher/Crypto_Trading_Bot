@echo off
REM GateRiskWatcher launcher (foreground; close window to stop)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual env not found. Run first:
  echo   uv venv .venv --python 3.11
  echo   uv pip install -r requirements.txt
  echo   .venv\Scripts\python.exe src\setup_keys.py
  pause
  exit /b 1
)
.venv\Scripts\python.exe src\watch.py
pause
