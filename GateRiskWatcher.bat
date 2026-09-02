@echo off
REM GateRiskWatcher risk board (one-shot print; press any key to close)
chcp 65001 >nul
title GateRiskWatcher Risk Board
cd /d "%~dp0"
".venv\Scripts\python.exe" src\risk_board.py
echo.
echo ==============================
echo Done. Press any key to close...
pause >nul
