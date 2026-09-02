@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual env missing. Run uv venv .venv --python 3.11 first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe src\setup_keys.py
pause
